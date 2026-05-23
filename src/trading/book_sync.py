"""REST order book -> local BookState (yedek; WS yeterli degilse)."""

from __future__ import annotations

import time
from typing import Any

from src.models import OrderBook, PriceLevel


def _level_price_size(item: Any) -> tuple[float, float]:
    if isinstance(item, dict):
        return float(item["price"]), float(item["size"])
    return float(item.price), float(item.size)


def order_book_from_rest(raw: dict[str, Any]) -> OrderBook:
    """CLOB GET /book yanitini OrderBook'a cevir."""
    ob = OrderBook()
    for b in raw.get("bids") or []:
        p, s = _level_price_size(b)
        ob.bids.append(PriceLevel(p, s))
    for a in raw.get("asks") or []:
        p, s = _level_price_size(a)
        ob.asks.append(PriceLevel(p, s))
    ob.bids.sort(key=lambda lv: lv.price, reverse=True)
    ob.asks.sort(key=lambda lv: lv.price)
    if ob.bids:
        ob.best_bid = ob.bids[0].price
    if ob.asks:
        ob.best_ask = ob.asks[0].price
    ltp = raw.get("last_trade_price")
    if ltp is not None and str(ltp).strip():
        ob.last_trade_price = float(ltp)
    ob.has_snapshot = True
    ob.updated_at = time.time()
    return ob
