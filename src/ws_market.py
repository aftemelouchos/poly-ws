"""Public market WebSocket client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

import time

from src.config import WebSocketConfig
from src.models import BookState, OrderBook, PriceLevel
from src.trading.book_levels import apply_price_change

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
RawHandler = Callable[[str], Awaitable[None]]


class MarketWebSocket:
    def __init__(
        self,
        cfg: WebSocketConfig,
        state: BookState,
        on_event: EventHandler | None = None,
        on_raw: RawHandler | None = None,
    ):
        self.cfg = cfg
        self.state = state
        self.on_event = on_event
        self.on_raw = on_raw
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._running = False
        self._asset_ids: list[str] = []

    def set_assets(self, yes_id: str, no_id: str) -> None:
        self._asset_ids = [yes_id, no_id]

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._ws:
            await self._ws.close()
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
            except Exception as e:
                self.state.ws_connected = False
                if self.on_event:
                    await self.on_event("error", {"message": str(e)})
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(
            self.cfg.market_url,
            ping_interval=None,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            sub = {
                "assets_ids": self._asset_ids,
                "type": "market",
                "custom_feature_enabled": self.cfg.custom_feature_enabled,
            }
            await ws.send(json.dumps(sub))
            self.state.ws_connected = True
            self._ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue
                    await self._handle_message(raw)
            finally:
                self.state.ws_connected = False
                if self._ping_task:
                    self._ping_task.cancel()

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
            if self.on_raw:
                await self.on_raw(raw)
            if raw == "PING":
                if self._ws:
                    await self._ws.send("PONG")
            return
        if self.on_raw:
            await self.on_raw(raw)
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
        event_type = data.get("event_type") or data.get("type")
        if not event_type:
            return
        handler = {
            "book": self._on_book,
            "price_change": self._on_price_change,
            "best_bid_ask": self._on_best_bid_ask,
            "last_trade_price": self._on_last_trade,
            "tick_size_change": self._on_tick_size_change,
            "market_resolved": self._on_market_resolved,
        }.get(event_type)
        if handler:
            handler(data)
        if self.on_event:
            await self.on_event(event_type, data)

    def _on_book(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id", "")
        ob = OrderBook()
        for b in data.get("bids", []):
            ob.bids.append(PriceLevel(float(b["price"]), float(b["size"])))
        for a in data.get("asks", []):
            ob.asks.append(PriceLevel(float(a["price"]), float(a["size"])))
        ob.bids.sort(key=lambda x: x.price, reverse=True)
        ob.asks.sort(key=lambda x: x.price)
        if ob.bids:
            ob.best_bid = ob.bids[0].price
        from src.trading.book_levels import _sync_bba_from_ladder

        _sync_bba_from_ladder(ob)
        ob.has_snapshot = True
        ob.updated_at = time.time()
        self.state.books[asset_id] = ob

    def _on_price_change(self, data: dict[str, Any]) -> None:
        for pc in data.get("price_changes", []):
            if not isinstance(pc, dict):
                continue
            asset_id = pc.get("asset_id", "")
            book = self.state.books.get(asset_id) or OrderBook()
            apply_price_change(book, pc)
            self.state.books[asset_id] = book

    def _on_best_bid_ask(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id", "")
        book = self.state.books.get(asset_id) or OrderBook()
        if data.get("best_bid") is not None:
            book.best_bid = float(data["best_bid"])
        if data.get("best_ask") is not None:
            book.best_ask = float(data["best_ask"])
        book.updated_at = time.time()
        self.state.books[asset_id] = book

    def _on_last_trade(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id", "")
        book = self.state.books.get(asset_id) or OrderBook()
        book.last_trade_price = float(data.get("price", 0))
        book.last_trade_side = data.get("side")
        self.state.books[asset_id] = book

    def _on_tick_size_change(self, data: dict[str, Any]) -> None:
        new_tick = data.get("new_tick_size")
        if new_tick:
            self.state.tick_size = float(new_tick)
            if self.state.market:
                self.state.market.tick_size = float(new_tick)

    def _on_market_resolved(self, data: dict[str, Any]) -> None:
        if self.state.market:
            self.state.market.active = False
