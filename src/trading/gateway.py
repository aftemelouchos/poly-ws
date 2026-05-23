"""Trading gateway: buy, sell, balance (paper + live)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod

from py_clob_client_v2 import (
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    OrderArgsV2,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from src.trading.config import TradingConfig
from src.trading.types import (
    AccountBalances,
    AssetBalance,
    OrderRequest,
    OrderResult,
    TradingMode,
)

logger = logging.getLogger(__name__)

USDC_DECIMALS = 1_000_000
# CLOB outcome token bakiyeleri genelde 6 ondalik (mikro birim)
CONDITIONAL_DECIMALS = 1_000_000


def _parse_amount(value: str | int | float | None) -> float:
    """USDC / collateral."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / USDC_DECIMALS if v > 1_000_000 else v
    s = str(value).strip()
    if not s:
        return 0.0
    v = float(s)
    return v / USDC_DECIMALS if v > 1_000_000 else v


def _parse_conditional_amount(value: str | int | float | None) -> float:
    """
    YES/NO token bakiyesi — API cogunlukla mikro birim (orn. 6375000 = 6.375).
    5000 mikro = 0.005 share; eski parser bunu 5000 share saniyordu.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if not s:
            return 0.0
        v = float(s)
    if v >= 100 and abs(v - round(v)) < 1e-9:
        return v / CONDITIONAL_DECIMALS
    if v > 1_000_000:
        return v / CONDITIONAL_DECIMALS
    return v


class TradingGateway(ABC):
    @property
    @abstractmethod
    def mode(self) -> TradingMode:
        pass

    @abstractmethod
    async def buy(self, req: OrderRequest) -> OrderResult:
        pass

    @abstractmethod
    async def sell(self, req: OrderRequest) -> OrderResult:
        pass

    @abstractmethod
    async def get_balances(
        self, token_ids: list[str] | None = None
    ) -> AccountBalances:
        pass

    async def cancel_all(self) -> None:
        pass


class PaperTradingGateway(TradingGateway):
    """Dry-run: sanal bakiye, emirler bellekte; fill yok (user WS veya motor doldurur)."""

    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg
        self._usdc = cfg.paper_initial_usdc
        self._tokens: dict[str, float] = {}
        self._orders: dict[str, dict] = {}
        self._counter = 0

    @property
    def mode(self) -> TradingMode:
        return TradingMode.PAPER

    def _next_id(self) -> str:
        self._counter += 1
        return f"paper-{self._counter:06d}"

    async def buy(self, req: OrderRequest) -> OrderResult:
        cost = req.price * req.size
        if cost > self._usdc + 1e-9:
            return OrderResult(
                success=False,
                error=f"Yetersiz USDC (need {cost:.4f}, have {self._usdc:.4f})",
                dry_run=True,
                side="BUY",
                token_id=req.token_id,
                price=req.price,
                size=req.size,
            )
        oid = self._next_id()
        self._usdc -= cost
        self._tokens[req.token_id] = self._tokens.get(req.token_id, 0.0) + req.size
        self._orders[oid] = {"side": "BUY", **req.__dict__}
        logger.info("[PAPER] BUY %s @ %.4f x %.2f -> %s", req.token_id[:12], req.price, req.size, oid)
        return OrderResult(
            success=True,
            order_id=oid,
            side="BUY",
            token_id=req.token_id,
            price=req.price,
            size=req.size,
            dry_run=True,
        )

    async def sell(self, req: OrderRequest) -> OrderResult:
        held = self._tokens.get(req.token_id, 0.0)
        if req.size > held + 1e-9:
            return OrderResult(
                success=False,
                error=f"Yetersiz token (need {req.size}, have {held})",
                dry_run=True,
                side="SELL",
                token_id=req.token_id,
                price=req.price,
                size=req.size,
            )
        oid = self._next_id()
        self._tokens[req.token_id] = held - req.size
        self._usdc += req.price * req.size
        self._orders[oid] = {"side": "SELL", **req.__dict__}
        logger.info("[PAPER] SELL %s @ %.4f x %.2f -> %s", req.token_id[:12], req.price, req.size, oid)
        return OrderResult(
            success=True,
            order_id=oid,
            side="SELL",
            token_id=req.token_id,
            price=req.price,
            size=req.size,
            dry_run=True,
        )

    async def get_balances(
        self, token_ids: list[str] | None = None
    ) -> AccountBalances:
        usdc = AssetBalance(
            asset_type=AssetType.COLLATERAL,
            token_id=None,
            balance=self._usdc,
            allowance=self._usdc,
        )
        tokens = {}
        for tid in token_ids or list(self._tokens.keys()):
            bal = self._tokens.get(tid, 0.0)
            tokens[tid] = AssetBalance(
                asset_type=AssetType.CONDITIONAL,
                token_id=tid,
                balance=bal,
                allowance=bal,
            )
        return AccountBalances(
            mode=TradingMode.PAPER,
            funder="paper",
            signer="paper",
            usdc=usdc,
            tokens=tokens,
        )

    async def cancel_all(self) -> None:
        self._orders.clear()
        logger.info("[PAPER] cancel_all (open orders cleared)")


class LiveTradingGateway(TradingGateway):
    """CLOB v2 live — gercek emirler."""

    def __init__(self, cfg: TradingConfig, client: ClobClient):
        self.cfg = cfg
        self.client = client
        self._dry_run = cfg.dry_run

    @property
    def mode(self) -> TradingMode:
        return TradingMode.LIVE

    async def _submit(self, req: OrderRequest, side: Side) -> OrderResult:
        side_name = "BUY" if side == Side.BUY else "SELL"
        if self._dry_run:
            logger.info(
                "[LIVE DRY-RUN] %s %s @ %.4f x %.2f",
                side_name,
                req.token_id[:16],
                req.price,
                req.size,
            )
            return OrderResult(
                success=True,
                order_id=f"dry-{uuid.uuid4().hex[:12]}",
                side=side_name,
                token_id=req.token_id,
                price=req.price,
                size=req.size,
                dry_run=True,
            )

        order_type = getattr(OrderType, req.order_type, OrderType.GTC)
        opts = PartialCreateOrderOptions(
            tick_size=str(req.tick_size),
            neg_risk=req.neg_risk,
        )
        args = OrderArgsV2(
            token_id=req.token_id,
            price=req.price,
            size=req.size,
            side=side,
        )
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.create_and_post_order(
                    args,
                    options=opts,
                    order_type=order_type,
                    post_only=req.post_only,
                ),
            )
        except Exception as e:
            logger.debug("[LIVE] %s failed: %s", side_name, e)
            return OrderResult(
                success=False,
                error=str(e),
                side=side_name,
                token_id=req.token_id,
                price=req.price,
                size=req.size,
            )

        oid = ""
        if isinstance(resp, dict):
            oid = str(resp.get("orderID") or resp.get("order_id") or "")
        logger.debug("[LIVE] %s %s -> %s", side_name, req.token_id[:12], oid)
        return OrderResult(
            success=bool(oid),
            order_id=oid,
            side=side_name,
            token_id=req.token_id,
            price=req.price,
            size=req.size,
            raw=resp if isinstance(resp, dict) else {"response": resp},
        )

    async def buy(self, req: OrderRequest) -> OrderResult:
        return await self._submit(req, Side.BUY)

    async def sell(self, req: OrderRequest) -> OrderResult:
        return await self._submit(req, Side.SELL)

    async def get_balances(
        self, token_ids: list[str] | None = None
    ) -> AccountBalances:
        loop = asyncio.get_running_loop()

        def _fetch_collateral():
            return self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )

        raw_usdc = await loop.run_in_executor(None, _fetch_collateral)
        usdc = AssetBalance(
            asset_type=AssetType.COLLATERAL,
            token_id=None,
            balance=_parse_amount(raw_usdc.get("balance")),
            allowance=_parse_amount(raw_usdc.get("allowance")),
            raw=raw_usdc if isinstance(raw_usdc, dict) else {},
        )

        tokens: dict[str, AssetBalance] = {}
        for tid in token_ids or []:
            def _fetch_token(token_id=tid):
                return self.client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                    )
                )

            raw = await loop.run_in_executor(None, _fetch_token)
            tokens[tid] = AssetBalance(
                asset_type=AssetType.CONDITIONAL,
                token_id=tid,
                balance=_parse_conditional_amount(raw.get("balance")),
                allowance=_parse_conditional_amount(raw.get("allowance")),
                raw=raw if isinstance(raw, dict) else {},
            )

        return AccountBalances(
            mode=TradingMode.LIVE,
            funder=self.cfg.funder_address,
            signer=self.client.get_address(),
            usdc=usdc,
            tokens=tokens,
        )

    async def cancel_all(self) -> None:
        if self._dry_run:
            logger.info("[LIVE DRY-RUN] cancel_all")
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.client.cancel_all)
        logger.debug("[LIVE] cancel_all")
