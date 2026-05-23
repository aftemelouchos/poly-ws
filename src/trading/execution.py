"""CLOB execution helpers — order book taker fiyat, hata yorumlama."""

from __future__ import annotations

import re

# Polymarket marketable emir minimum notional (USD)
MIN_MARKETABLE_USD = 1.05


def is_fak_no_match(error: str | None) -> bool:
    return bool(error and "no orders found to match" in error)


def is_tokens_locked_after_sell(error: str | None) -> bool:
    """Emir doldu; token henuz settlement'te kilitli."""
    if not error:
        return False
    e = error.lower()
    return "not enough balance" in e and "matched orders" in e


def is_min_notional_error(error: str | None) -> bool:
    return bool(error and "min size" in error and "marketable" in error)


def order_size_min_notional(price: float, size: float, min_usd: float = MIN_MARKETABLE_USD) -> float:
    """FAK BUY icin en az $1 notional."""
    if price <= 0:
        return size
    if price * size >= min_usd - 1e-6:
        return size
    import math

    need = min_usd / price
    return math.ceil(need * 10000) / 10000


def parse_matched_shares_from_error(error: str | None) -> float | None:
    """balance: 6000000 -> 6.0 share (mikro birim)."""
    if not error:
        return None
    m = re.search(r"balance:\s*(\d+)", error)
    if not m:
        return None
    raw = int(m.group(1))
    if raw >= 100:
        return raw / 1_000_000
    return float(raw)
