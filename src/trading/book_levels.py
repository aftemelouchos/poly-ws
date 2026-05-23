"""WS order book seviye guncellemeleri (price_change)."""

from __future__ import annotations

import time

from src.models import OrderBook, PriceLevel


def _touch(book: OrderBook) -> None:
    book.updated_at = time.time()


def apply_level(
    levels: list[PriceLevel],
    price: float,
    size: float,
    *,
    descending: bool,
) -> list[PriceLevel]:
    """size=0 -> seviye sil; aksi halde upsert."""
    out = [lv for lv in levels if abs(lv.price - price) > 1e-9]
    if size > 1e-9:
        out.append(PriceLevel(price, size))
    out.sort(key=lambda lv: lv.price, reverse=descending)
    return out


def apply_price_change(book: OrderBook, pc: dict) -> None:
    """
    Polymarket price_change: side=BUY -> bid, side=SELL -> ask.
    https://docs.polymarket.com/market-data/websocket/market-channel
    """
    price_raw = pc.get("price")
    size_raw = pc.get("size")
    side = str(pc.get("side", "")).upper()
    if price_raw is None:
        return

    price = float(price_raw)
    size = float(size_raw) if size_raw is not None else 0.0

    if side == "BUY":
        book.bids = apply_level(book.bids, price, size, descending=True)
        if book.bids:
            book.best_bid = book.bids[0].price
        elif pc.get("best_bid") is not None:
            bb = float(pc["best_bid"])
            book.best_bid = bb if bb > 0 else None
    elif side == "SELL":
        book.asks = apply_level(book.asks, price, size, descending=False)
        if book.asks:
            book.best_ask = book.asks[0].price
        elif pc.get("best_ask") is not None:
            ba = float(pc["best_ask"])
            book.best_ask = ba if ba > 0 else None

    _sync_bba_from_ladder(book)
    _touch(book)


def _sync_bba_from_ladder(book: OrderBook) -> None:
    """Merdiven ile BBA uyumlu olsun (WS best_ask bazen geride kalir)."""
    if book.bids:
        book.best_bid = book.bids[0].price
    if book.asks:
        book.best_ask = book.asks[0].price


def book_ready_for_buy(book: OrderBook) -> bool:
    return bool(book.asks) or (book.best_ask is not None and book.best_ask > 0)


def book_ready_for_sell(book: OrderBook) -> bool:
    return bool(book.bids) or (book.best_bid is not None and book.best_bid > 0)
