"""HFT runtime: market WS + user WS + trading gateway (dusuk gecikme)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from src.config import WebSocketConfig
from src.models import BookState, MarketInfo, OrderBook, TokenLabel
from src.trading.clob_client import api_creds_from_client, create_clob_client
from src.trading.config import TradingConfig
from src.trading.execution import (
    is_fak_no_match,
    is_min_notional_error,
    is_tokens_locked_after_sell,
    order_size_min_notional,
)
from src.trading.factory import create_gateway
from src.trading.gateway import LiveTradingGateway, TradingGateway
from src.trading.book_levels import book_ready_for_buy, book_ready_for_sell
from src.trading.book_sync import order_book_from_rest
from src.trading.pricing import (
    live_buy_taker_price,
    live_sell_sweep_price,
    live_sell_taker_price,
    round_down_shares,
)
from src.trading.types import OrderRequest, OrderResult, TradingMode
from src.ws_market import MarketWebSocket
from src.ws_user import UserWebSocket

logger = logging.getLogger(__name__)

FillHandler = Callable[[dict[str, Any]], Awaitable[None]]
TradeHandler = Callable[[dict[str, Any]], Awaitable[None]]

MIN_HELD_SHARES = 0.5
# Not: API basari = trade gerceklesti (CLOB orderID dondu). WS event sadece
# state.positions bookkeeping icin; HFT'de hicbir trade WS event'i icin BEKLENMIYOR.
# Burada hiç asyncio.wait_for / sleep YOK.


class HftTradingRuntime:
    def __init__(self, cfg: TradingConfig, state: BookState | None = None):
        self.cfg = cfg
        self.state = state or BookState()
        self.gateway: TradingGateway = create_gateway(cfg)

        ws_cfg = WebSocketConfig(
            market_url=cfg.market_ws_url,
            user_url=cfg.user_ws_url,
            ping_interval_seconds=cfg.ws_ping_seconds,
        )
        self._ws_cfg = ws_cfg
        self.market_ws: MarketWebSocket | None = None
        self.user_ws: UserWebSocket | None = None
        self._clob_client = None
        self._on_fill: FillHandler | None = None
        self._on_trade: TradeHandler | None = None

    def on_fill(self, handler: FillHandler) -> None:
        self._on_fill = handler

    def on_trade(self, handler: TradeHandler) -> None:
        self._on_trade = handler

    async def attach_market(self, market: MarketInfo) -> None:
        self.state.market = market
        self.state.tick_size = market.tick_size
        self.state.books.clear()

        if self.market_ws:
            await self.market_ws.stop()

        self.market_ws = MarketWebSocket(
            self._ws_cfg,
            self.state,
            on_event=self._on_market_event,
        )
        self.market_ws.set_assets(market.yes_token_id, market.no_token_id)
        await self.market_ws.start()

        if self.cfg.mode == TradingMode.LIVE and isinstance(self.gateway, LiveTradingGateway):
            await self._start_user_ws(market)
        logger.info("WS attached | %s", market.slug)

    async def _start_user_ws(self, market: MarketInfo) -> None:
        if self.user_ws:
            await self.user_ws.stop()

        client = self.gateway.client
        self._clob_client = client
        creds = api_creds_from_client(client)

        self.user_ws = UserWebSocket(
            self._ws_cfg,
            self.state,
            api_key=creds.api_key,
            secret=creds.api_secret,
            passphrase=creds.api_passphrase,
            condition_id=market.condition_id,
            on_event=self._on_user_event,
        )
        await self.user_ws.start()

    async def _on_market_event(self, event_type: str, data: dict) -> None:
        pass

    async def _on_user_event(self, event_type: str, data: dict) -> None:
        if event_type == "fill" and self._on_fill:
            await self._on_fill(data)
        if event_type == "trade" and self._on_trade:
            await self._on_trade(data)

    async def buy(
        self,
        token_id: str,
        price: float,
        size: float | None = None,
        **kwargs,
    ) -> OrderResult:
        req = OrderRequest(
            token_id=token_id,
            price=price,
            size=size or self.cfg.default_order_size,
            tick_size=str(self.state.tick_size),
            neg_risk=self.state.market.neg_risk if self.state.market else False,
            **kwargs,
        )
        return await self.gateway.buy(req)

    async def sell(
        self,
        token_id: str,
        price: float,
        size: float | None = None,
        **kwargs,
    ) -> OrderResult:
        req = OrderRequest(
            token_id=token_id,
            price=price,
            size=size or self.cfg.default_order_size,
            tick_size=str(self.state.tick_size),
            neg_risk=self.state.market.neg_risk if self.state.market else False,
            **kwargs,
        )
        return await self.gateway.sell(req)

    async def get_balances(self, token_ids: list[str] | None = None):
        ids = token_ids
        if ids is None and self.state.market:
            ids = [self.state.market.yes_token_id, self.state.market.no_token_id]
        return await self.gateway.get_balances(ids)

    def _tick(self) -> float:
        try:
            return float(self.state.tick_size or 0.01)
        except (TypeError, ValueError):
            return 0.01

    def book_ws(self, label: TokenLabel) -> OrderBook:
        """Sadece WS kitap — REST yok (gecikme yok)."""
        return self.state.book_for(label)

    async def refresh_book_rest(self, token_id: str, label: TokenLabel) -> OrderBook:
        if not isinstance(self.gateway, LiveTradingGateway):
            return self.state.book_for(label)
        client = self.gateway.client
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, lambda: client.get_order_book(token_id))
        if not isinstance(raw, dict):
            return self.state.book_for(label)
        ob = order_book_from_rest(raw)
        ob.has_snapshot = True
        ob.updated_at = time.time()
        ts = raw.get("tick_size")
        if ts is not None:
            try:
                self.state.tick_size = float(ts)
            except (TypeError, ValueError):
                pass
        self.state.books[token_id] = ob
        return ob

    async def book_for_trade(
        self, token_id: str, label: TokenLabel, *, side: str
    ) -> OrderBook:
        book = self.book_ws(label)
        ready = book_ready_for_buy(book) if side == "BUY" else book_ready_for_sell(book)
        if ready:
            return book
        return await self.refresh_book_rest(token_id, label)

    def held_size_ws(self, token_id: str) -> float:
        """User WS pozisyonu — anında, REST yok."""
        label = self.state.token_label(token_id)
        if label:
            return round_down_shares(self.state.positions.get(label, 0.0))
        return 0.0

    async def held_size(self, token_id: str, *, force_rest: bool = False) -> float:
        """WS pozisyonu yeterse onu kullan; degilse REST."""
        if not force_rest:
            v = self.held_size_ws(token_id)
            if v >= MIN_HELD_SHARES:
                return v
        bal = await self.gateway.get_balances([token_id])
        tb = bal.tokens.get(token_id)
        return round_down_shares(tb.balance if tb else 0.0)

    def _sell_confirmed_local(self, token_id: str) -> bool:
        """WS apply_fill anlik state.positions'i azalttiysa onayli."""
        return self.held_size_ws(token_id) < MIN_HELD_SHARES

    async def sell_fast(
        self,
        token_id: str,
        label: TokenLabel,
        size: float | None = None,
        *,
        max_attempts: int = 4,
    ) -> OrderResult:
        """FAK sat — agresif retry, REST yerine WS pozisyonuna guvenir."""
        tick = self._tick()
        # Hizli yol: size verilmediyse WS pozisyonundan oku
        if size is None:
            remaining = self.held_size_ws(token_id)
        else:
            remaining = round_down_shares(size)
        if remaining < MIN_HELD_SHARES:
            return OrderResult(success=True, side="SELL", token_id=token_id, size=0.0)

        last = OrderResult(
            success=False,
            side="SELL",
            token_id=token_id,
            size=remaining,
            error="no attempt",
        )

        for attempt in range(max_attempts):
            if attempt > 0:
                # WS pozisyonu hemen guncel; REST'e dusmek gereksiz
                remaining = self.held_size_ws(token_id)
                if remaining < MIN_HELD_SHARES:
                    last.success = True
                    return last

            book = self.book_ws(label)
            px, _ = live_sell_taker_price(
                book, remaining, tick, slippage_ticks=attempt
            )
            last = await self.sell(
                token_id, price=px, size=remaining, order_type="FAK"
            )

            # API basari = FAK match oldu (kismi olabilir). WS event beklemiyoruz.
            if last.success:
                return last

            err = last.error or ""
            if is_tokens_locked_after_sell(err):
                # WS apply_fill yetisti mi?
                if self._sell_confirmed_local(token_id):
                    last.success = True
                    return last
                continue
            if not is_fak_no_match(err):
                return last
            # FAK no_match: anlik kitap bos, hemen tekrar dene (sleep YOK)

        # Son care: ladder sweep
        remaining = self.held_size_ws(token_id)
        if remaining < MIN_HELD_SHARES:
            last.success = True
            return last

        book = self.book_ws(label)
        sweep_px = live_sell_sweep_price(book, tick)
        last = await self.sell(
            token_id, price=sweep_px, size=remaining, order_type="FAK"
        )
        if last.success or is_tokens_locked_after_sell(last.error or ""):
            last.success = True
        return last

    async def buy_fast(
        self,
        token_id: str,
        label: TokenLabel,
        size: float,
        *,
        max_attempts: int = 4,
    ) -> OrderResult:
        """FAK al — agresif retry, REST yerine WS pozisyonuna guvenir."""
        tick = self._tick()
        order_size = round_down_shares(size)
        last = OrderResult(
            success=False,
            side="BUY",
            token_id=token_id,
            size=order_size,
            error="no attempt",
        )

        for attempt in range(max_attempts):
            book = self.book_ws(label)
            if not book_ready_for_buy(book):
                book = await self.refresh_book_rest(token_id, label)

            px, _ = live_buy_taker_price(
                book, order_size, tick, slippage_ticks=1 + attempt
            )
            order_size = order_size_min_notional(px, order_size)
            last = await self.buy(
                token_id, price=px, size=order_size, order_type="FAK"
            )
            if last.success:
                return last

            err = last.error or ""
            if is_fak_no_match(err) or is_min_notional_error(err):
                # WS apply_fill paralel olarak gelmis mi (parallel partial fill)?
                got_ws = self.held_size_ws(token_id)
                if got_ws >= order_size * 0.85:
                    last.success = True
                    return last
                # Bos kitap — hemen tekrar dene (sleep YOK, wait YOK)
                continue
            return last

        return last

    async def stop(self) -> None:
        if self.user_ws:
            await self.user_ws.stop()
        if self.market_ws:
            await self.market_ws.stop()
