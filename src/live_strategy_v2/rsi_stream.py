"""Canli UP mid -> resample bar -> RSI cross."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from src.strategy_v2.config import StockRsiConfig
from src.strategy_v2.rsi import compute_rsi, detect_cross


@dataclass
class BarSnapshot:
    time: datetime
    yes_mid: float
    yes_bid: float | None
    yes_ask: float | None
    no_mid: float | None
    no_bid: float | None
    no_ask: float | None
    rsi: float
    cross_up: bool
    cross_down: bool


@dataclass
class RsiLiveStream:
    cfg: StockRsiConfig
    _mids: list[float] = field(default_factory=list)
    _bucket: int | None = None
    _pending_mid: float | None = None
    _pending_snap: dict | None = None
    _prev_rsi: float | None = None
    _last_rsi: float | None = None

    def _bucket_id(self, ts: datetime) -> int:
        sec = int(self.cfg.resample_seconds or 1)
        return int(ts.timestamp()) // sec

    def update(
        self,
        ts: datetime,
        yes_mid: float,
        *,
        yes_bid: float | None = None,
        yes_ask: float | None = None,
        no_mid: float | None = None,
        no_bid: float | None = None,
        no_ask: float | None = None,
    ) -> BarSnapshot | None:
        """Yeni bar kapandiginda snapshot doner (RSI + cross)."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        bid = self._bucket_id(ts)
        snap = {
            "yes_mid": yes_mid,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_mid": no_mid,
            "no_bid": no_bid,
            "no_ask": no_ask,
        }

        if self._bucket is None:
            self._bucket = bid
            self._pending_mid = yes_mid
            self._pending_snap = snap
            return None

        if bid == self._bucket:
            self._pending_mid = yes_mid
            self._pending_snap = snap
            return None

        # Onceki bucket kapandi
        closed = self._close_bar(self._bucket, self._pending_mid, self._pending_snap)
        self._bucket = bid
        self._pending_mid = yes_mid
        self._pending_snap = snap
        return closed

    def _close_bar(self, bucket: int, yes_mid: float, snap: dict) -> BarSnapshot | None:
        if yes_mid is None or math.isnan(yes_mid):
            return None

        self._mids.append(yes_mid)
        max_len = max(self.cfg.rsi_period * 4, 200)
        if len(self._mids) > max_len:
            self._mids = self._mids[-max_len:]

        if len(self._mids) < self.cfg.rsi_period + 1:
            return None

        series = pd.Series(self._mids)
        rsi_series = compute_rsi(series, self.cfg.rsi_period, wilder=self.cfg.use_wilder)
        rsi = float(rsi_series.iloc[-1])
        if math.isnan(rsi):
            return None

        cross_up, cross_down = detect_cross(
            self._prev_rsi,
            rsi,
            self.cfg.cross_up_level,
            self.cfg.cross_down_level,
        )
        self._prev_rsi = rsi
        self._last_rsi = rsi

        bar_time = datetime.fromtimestamp(
            bucket * int(self.cfg.resample_seconds or 1), tz=timezone.utc
        )
        return BarSnapshot(
            time=bar_time,
            yes_mid=yes_mid,
            yes_bid=snap.get("yes_bid"),
            yes_ask=snap.get("yes_ask"),
            no_mid=snap.get("no_mid"),
            no_bid=snap.get("no_bid"),
            no_ask=snap.get("no_ask"),
            rsi=rsi,
            cross_up=cross_up,
            cross_down=cross_down,
        )

    @property
    def last_rsi(self) -> float | None:
        return self._last_rsi
