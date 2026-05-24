"""Authenticated user WebSocket (live mode)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from src.config import WebSocketConfig
from src.models import BookState, Fill, OpenOrder, Side, TokenLabel


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
logger = logging.getLogger(__name__)


class UserWebSocket:
    def __init__(
        self,
        cfg: WebSocketConfig,
        state: BookState,
        api_key: str,
        secret: str,
        passphrase: str,
        condition_id: str,
        on_event: EventHandler | None = None,
    ):
        self.cfg = cfg
        self.state = state
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.condition_id = condition_id
        self.on_event = on_event
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        # Polymarket trade id dedup — ayni trade icin MATCHED + CONFIRMED iki kez gelir.
        # Sadece ilk gorenden apply_fill cagrilir (cift sayim olmaz).
        self._seen_trades: deque[str] = deque(maxlen=2048)
        self._seen_set: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        backoff = list(self.cfg.reconnect_backoff_seconds)
        attempt = 0
        while self._running:
            try:
                await self._connect_and_listen()
                attempt = 0
            except asyncio.CancelledError:
                break
            except Exception:
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(self.cfg.user_url, ping_interval=None) as ws:
            self._ws = ws
            sub = {
                "auth": {
                    "apiKey": self.api_key,
                    "secret": self.secret,
                    "passphrase": self.passphrase,
                },
                "markets": [self.condition_id],
                "type": "user",
            }
            await ws.send(json.dumps(sub))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue
                    await self._handle_message(raw)
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws: ClientConnection) -> None:
        while self._running:
            try:
                await ws.send("PING")
            except Exception:
                break
            await asyncio.sleep(self.cfg.ping_interval_seconds)

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        if raw in ("PING", "PONG"):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            for item in data:
                await self._dispatch(item)
        else:
            await self._dispatch(data)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        event_type = data.get("event_type") or data.get("type", "").lower()
        if event_type == "trade":
            await self._on_trade(data)
        elif event_type == "order":
            await self._on_order(data)
        if self.on_event:
            await self.on_event(event_type, data)

    def _mark_seen(self, trade_id: str) -> bool:
        """Return True if first time seeing this trade id, False if duplicate."""
        if not trade_id:
            return True  # id yoksa dedup yapma
        if trade_id in self._seen_set:
            return False
        self._seen_set.add(trade_id)
        self._seen_trades.append(trade_id)
        # deque maxlen asilirsa eski id'yi setten de cikar
        if len(self._seen_set) > self._seen_trades.maxlen:
            for old in list(self._seen_set - set(self._seen_trades)):
                self._seen_set.discard(old)
        return True

    async def _on_trade(self, data: dict[str, Any]) -> None:
        status = data.get("status", "")
        if status not in ("MATCHED", "CONFIRMED"):
            return
        trade_id = str(data.get("id", "") or data.get("trade_id", "") or "")
        # MATCHED + CONFIRMED ayni trade icin gelir — fill sadece bir kez apply edilmeli
        if not self._mark_seen(trade_id):
            return
        asset_id = data.get("asset_id", "")
        label = self.state.token_label(asset_id)
        if not label:
            return
        side = Side.BUY if str(data.get("side", "")).upper() == "BUY" else Side.SELL
        size = float(data.get("size", 0))
        price = float(data.get("price", 0))
        fill = Fill(
            order_id=data.get("taker_order_id", trade_id),
            token=label,
            side=side,
            price=price,
            size=size,
        )
        self.state.apply_fill(fill)
        logger.debug(
            "Fill %s %s %.4f x %.4f id=%s status=%s",
            label.value,
            side.value,
            price,
            size,
            trade_id[:12],
            status,
        )
        if self.on_event:
            await self.on_event("fill", {"fill": fill})

    async def _on_order(self, data: dict[str, Any]) -> None:
        oid = data.get("id", "")
        otype = str(data.get("type", "")).upper()
        asset_id = data.get("asset_id", "")
        label = self.state.token_label(asset_id)
        if not label:
            return
        side = Side.BUY if str(data.get("side", "")).upper() == "BUY" else Side.SELL
        price = float(data.get("price", 0))
        if otype == "CANCELLATION":
            self.state.open_orders.pop(oid, None)
        elif otype in ("PLACEMENT", "UPDATE"):
            orig = float(data.get("original_size", data.get("size", 0)))
            matched = float(data.get("size_matched", 0))
            self.state.open_orders[oid] = OpenOrder(
                order_id=oid,
                token=label,
                side=side,
                price=price,
                size=orig,
                remaining=orig - matched,
            )
