"""Aggressive limit prices for live FAK orders (order book taker)."""

from __future__ import annotations

import math

from src.models import OrderBook, PriceLevel


def round_down_shares(value: float, decimals: int = 4) -> float:
    factor = 10**decimals
    return math.floor(value * factor + 1e-12) / factor


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 6)


def round_up_to_tick(price: float, tick: float) -> float:
    """FAK BUY: yukari yuvarla ki kesinlikle ask'i kes."""
    if tick <= 0:
        return price
    steps = math.ceil((price - 1e-12) / tick)
    return round(min(0.99, steps * tick), 6)


def _sorted_asks(book: OrderBook) -> list[PriceLevel]:
    return sorted(book.asks, key=lambda lv: lv.price)


def _sorted_bids(book: OrderBook) -> list[PriceLevel]:
    return sorted(book.bids, key=lambda lv: lv.price, reverse=True)


def effective_best_ask(book: OrderBook, tick: float) -> float | None:
    asks = _sorted_asks(book)
    if asks:
        return asks[0].price
    ba = book.best_ask
    return ba if ba is not None and ba > 0 else None


def effective_best_bid(book: OrderBook, tick: float) -> float | None:
    bids = _sorted_bids(book)
    if bids:
        return bids[0].price
    bb = book.best_bid
    return bb if bb is not None and bb > 0 else None


def _levels_near_price(
    levels: list[PriceLevel], price: float, tick: float
) -> list[PriceLevel]:
    return [lv for lv in levels if abs(lv.price - price) <= tick * 0.51 + 1e-9]


def live_buy_taker_price(
    book: OrderBook,
    size: float,
    tick: float,
    *,
    slippage_ticks: int = 1,
    clob_ask: float | None = None,
) -> tuple[float, float]:
    """
    Touch ask + slippage; yeterli derinlik yoksa merdiven yurur.
    clob_ask: GET /price (BUY) — WS ile celisirse bu kullanilir.
    """
    touch = effective_best_ask(book, tick)
    if clob_ask is not None and clob_ask > 0:
        touch = max(touch or 0.0, clob_ask)
    if touch is None or touch <= 0:
        ref = book.mid() or book.last_trade_price or 0.55
        extra = max(1, slippage_ticks) * tick
        return round_up_to_tick(min(0.99, ref + extra), tick), 0.0

    asks = _sorted_asks(book)
    slip = max(1, slippage_ticks) * tick
    near = _levels_near_price(asks, touch, tick)
    touch_depth = sum(lv.size for lv in near)

    accumulated = 0.0
    limit_px = touch
    for lev in asks:
        if lev.price < touch - tick * 0.51:
            continue
        accumulated += lev.size
        limit_px = lev.price
        if accumulated >= size - 1e-6:
            break

    if accumulated < size * 0.9 and asks:
        limit_px = asks[-1].price if accumulated > 0 else touch

    limit_px = round_up_to_tick(min(0.99, limit_px + slip), tick)
    return limit_px, touch_depth if touch_depth > 0 else accumulated


def live_buy_price(book: OrderBook, tick: float, step: int = 0) -> float:
    px, _ = live_buy_taker_price(book, 1.0, tick)
    if step > 0:
        return min(0.99, px + step * tick)
    return px


def live_sell_taker_price(
    book: OrderBook,
    size: float,
    tick: float,
    *,
    slippage_ticks: int = 0,
    clob_bid: float | None = None,
) -> tuple[float, float]:
    """Touch bid - slippage; clob_bid = GET /price (SELL)."""
    touch = effective_best_bid(book, tick)
    if clob_bid is not None and clob_bid > 0:
        touch = min(touch or 1.0, clob_bid)
    if touch is None or touch <= 0:
        ref = book.mid() or book.last_trade_price or 0.25
        slip = max(0, slippage_ticks) * tick
        return round_to_tick(max(0.01, ref - slip), tick), 0.0

    bids = _sorted_bids(book)
    slip = max(0, slippage_ticks) * tick
    near = _levels_near_price(bids, touch, tick)
    touch_depth = sum(lv.size for lv in near)

    accumulated = 0.0
    limit_px = touch
    for lev in bids:
        if lev.price > touch + tick * 0.51:
            continue
        accumulated += lev.size
        limit_px = lev.price
        if accumulated >= size - 1e-6:
            break

    if accumulated < size * 0.9 and bids:
        limit_px = bids[-1].price if accumulated > 0 else touch

    limit_px = round_to_tick(max(0.01, limit_px - slip), tick)
    return limit_px, touch_depth if touch_depth > 0 else accumulated


def live_sell_price(book: OrderBook, tick: float, step: int = 0) -> float:
    px, _ = live_sell_taker_price(book, 1.0, tick)
    if step > 0:
        return max(0.01, px - step * tick)
    return px


def live_sell_sweep_price(book: OrderBook, tick: float) -> float:
    if book.bids:
        low = min(lv.price for lv in book.bids)
        return round_to_tick(max(0.01, low - 2 * tick), tick)
    ref = book.best_bid or book.mid() or 0.2
    return round_to_tick(max(0.01, ref - 3 * tick), tick)
