"""RSI hesaplama (Wilder veya SMA tabanlı)."""

from __future__ import annotations

import pandas as pd


def compute_rsi(close: pd.Series, period: int, *, wilder: bool = True) -> pd.Series:
    """close: fiyat serisi (UP mid). İlk `period` bar NaN."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    if wilder:
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    else:
        avg_gain = gain.rolling(period, min_periods=period).mean()
        avg_loss = loss.rolling(period, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def detect_cross(
    prev_rsi: float | None,
    rsi: float,
    cross_up_level: float,
    cross_down_level: float,
) -> tuple[bool, bool]:
    """Tek adim cross: (cross_up, cross_down)."""
    import math

    if prev_rsi is None or math.isnan(prev_rsi) or math.isnan(rsi):
        return False, False
    cross_up = prev_rsi < cross_up_level <= rsi
    cross_down = prev_rsi > cross_down_level >= rsi
    return cross_up, cross_down


def detect_crosses(
    rsi: pd.Series,
    cross_up_level: float,
    cross_down_level: float,
) -> pd.DataFrame:
    """cross_up / cross_down boolean kolonları."""
    prev = rsi.shift(1)
    cross_up = (prev < cross_up_level) & (rsi >= cross_up_level)
    cross_down = (prev > cross_down_level) & (rsi <= cross_down_level)
    return pd.DataFrame({"rsi": rsi, "cross_up": cross_up, "cross_down": cross_down})
