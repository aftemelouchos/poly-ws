"""Strategy v2 backtest: UP RSI cross + resolution (canli motor ile ayni kurallar)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pandas as pd

from src.strategy_v2.config import StockRsiConfig, is_entry_price_safe
from src.strategy_v2.resolution import (
    entries_window_minutes,
    market_end_from_slug,
    minutes_to_expiry,
    resolution_phase,
    should_force_close,
)
from src.strategy_v2.rsi import compute_rsi, detect_crosses
from src.ws_csv_loader import load_ws_csv


class Side(str, Enum):
    FLAT = "FLAT"
    UP = "UP"
    DOWN = "DOWN"


class Action(str, Enum):
    BUY_UP = "BUY_UP"
    SELL_UP = "SELL_UP"
    BUY_DOWN = "BUY_DOWN"
    SELL_DOWN = "SELL_DOWN"
    RESOLUTION_SELL_UP = "RESOLUTION_SELL_UP"
    RESOLUTION_SELL_DOWN = "RESOLUTION_SELL_DOWN"
    SETTLE_UP = "SETTLE_UP"
    SETTLE_DOWN = "SETTLE_DOWN"


@dataclass
class Leg:
    action: str
    time: pd.Timestamp
    price: float
    rsi: float
    side_after: str
    note: str = ""


@dataclass
class RoundTrip:
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl: float
    hold_seconds: float
    exit_kind: str = "signal"

    @property
    def won(self) -> bool:
        return self.pnl > 0


@dataclass
class BacktestResultV2:
    csv_path: str
    config: StockRsiConfig
    legs: list[Leg] = field(default_factory=list)
    round_trips: list[RoundTrip] = field(default_factory=list)
    signals_up: int = 0
    signals_down: int = 0
    skipped_cooldown: int = 0
    skipped_resolution_entry: int = 0
    skipped_extreme_price: int = 0
    resolution_closes: int = 0
    resolution_holds: int = 0
    resolution_settlements: int = 0

    @property
    def total_pnl(self) -> float:
        return sum(r.pnl for r in self.round_trips)

    @property
    def win_rate(self) -> float:
        if not self.round_trips:
            return 0.0
        return sum(1 for r in self.round_trips if r.won) / len(self.round_trips)

    def summary_dict(self) -> dict:
        pnls = [r.pnl for r in self.round_trips]
        cum = peak = max_dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        res = self.config.resolution
        return {
            "round_trips": len(self.round_trips),
            "legs": len(self.legs),
            "signals_cross_up": self.signals_up,
            "signals_cross_down": self.signals_down,
            "wins": sum(1 for r in self.round_trips if r.won),
            "losses": sum(1 for r in self.round_trips if not r.won),
            "win_rate_pct": round(self.win_rate * 100, 2),
            "total_pnl": round(self.total_pnl, 4),
            "avg_pnl": round(self.total_pnl / len(self.round_trips), 4)
            if self.round_trips
            else 0.0,
            "max_drawdown": round(max_dd, 4),
            "skipped_cooldown": self.skipped_cooldown,
            "skipped_resolution_entry": self.skipped_resolution_entry,
            "skipped_extreme_price": self.skipped_extreme_price,
            "resolution_closes": self.resolution_closes,
            "resolution_holds": self.resolution_holds,
            "resolution_settlements": self.resolution_settlements,
            "res_force_close_min": res.force_close_minutes,
            "res_hold_gte": res.hold_force_close_if_mid_gte,
            "res_dump_lt": res.dump_immediately_if_mid_lt,
        }


def prepare_bars(df: pd.DataFrame, cfg: StockRsiConfig) -> pd.DataFrame:
    """UP mid + RSI + cross bayraklari; istege bagli resample."""
    base = df[["time", "yes_mid", "yes_bid", "yes_ask", "no_bid", "no_ask", "no_mid"]].copy()
    base = base.dropna(subset=["yes_mid"]).drop_duplicates(subset=["time"], keep="last")
    base = base.sort_values("time").reset_index(drop=True)

    if cfg.resample_seconds and cfg.resample_seconds > 0:
        rule = f"{cfg.resample_seconds}s"
        idx = base.set_index("time")
        resampled = idx.resample(rule).last().ffill()
        resampled["time"] = resampled.index
        resampled = resampled.reset_index(drop=True)
        work = resampled
    else:
        work = base

    price = work["yes_mid"]
    rsi = compute_rsi(price, cfg.rsi_period, wilder=cfg.use_wilder)
    crosses = detect_crosses(rsi, cfg.cross_up_level, cfg.cross_down_level)
    work = work.copy()
    work["rsi"] = crosses["rsi"].values
    work["cross_up"] = crosses["cross_up"].fillna(False).values
    work["cross_down"] = crosses["cross_down"].fillna(False).values
    return work


def _px(row: pd.Series, side: Side, buy: bool) -> float:
    if side == Side.UP:
        return float(row["yes_ask"] if buy else row["yes_bid"])
    if side == Side.DOWN:
        return float(row["no_ask"] if buy else row["no_bid"])
    return 0.0


def _position_mid_row(row: pd.Series, pos: Side) -> float | None:
    if pos == Side.UP:
        v = row.get("yes_mid")
    elif pos == Side.DOWN:
        v = row.get("no_mid")
    else:
        return None
    if v is None or pd.isna(v):
        return None
    return float(v)


def run_backtest(
    csv_path: Path | str,
    config: StockRsiConfig | None = None,
) -> BacktestResultV2:
    cfg = config or StockRsiConfig()
    path = Path(csv_path)
    raw = load_ws_csv(path)
    result = BacktestResultV2(csv_path=str(csv_path), config=cfg)
    if raw.empty:
        return result
    bars = prepare_bars(raw, cfg)

    slug = path.name.split("_")[0] if "_" in path.name else path.stem
    market_end = market_end_from_slug(slug)
    res_cfg = cfg.resolution
    entry_window_m = entries_window_minutes(slug, res_cfg) if market_end else 0.0

    position = Side.FLAT
    entry_time: pd.Timestamp | None = None
    entry_price: float = 0.0
    last_trade_time: pd.Timestamp | None = None
    resolution_closes = 0
    resolution_holds = 0
    resolution_settlements = 0
    skipped_resolution_entry = 0
    skipped_extreme_price = 0

    def _minutes_left_at(t: pd.Timestamp) -> float | None:
        if market_end is None:
            return None
        dt = t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
        return minutes_to_expiry(dt, market_end)

    def _finish_round(
        row: pd.Series,
        t: pd.Timestamp,
        rsi: float,
        exit_px: float,
        action: str,
        exit_kind: str,
        note: str = "",
    ) -> None:
        nonlocal position, entry_time, entry_price
        if position == Side.FLAT:
            return
        hold = (t - entry_time).total_seconds() if entry_time else 0.0
        pnl = (exit_px - entry_price) * cfg.size
        result.legs.append(
            Leg(action, t, exit_px, rsi, Side.FLAT.value, note=note)
        )
        result.round_trips.append(
            RoundTrip(
                side=position.value,
                entry_time=entry_time,
                exit_time=t,
                entry_price=entry_price,
                exit_price=exit_px,
                pnl=pnl,
                hold_seconds=hold,
                exit_kind=exit_kind,
            )
        )
        position = Side.FLAT
        entry_time = None
        entry_price = 0.0

    def _close_position(
        row: pd.Series,
        t: pd.Timestamp,
        rsi: float,
        *,
        resolution_reason: str = "",
    ) -> None:
        if position == Side.FLAT:
            return
        sell_px = _px(row, position, buy=False)
        if position == Side.UP:
            act = (
                Action.RESOLUTION_SELL_UP.value
                if resolution_reason
                else Action.SELL_UP.value
            )
        else:
            act = (
                Action.RESOLUTION_SELL_DOWN.value
                if resolution_reason
                else Action.SELL_DOWN.value
            )
        kind = "resolution" if resolution_reason else "signal"
        _finish_round(row, t, rsi, sell_px, act, kind, note=resolution_reason)

    def _settle_position(row: pd.Series, t: pd.Timestamp, rsi: float) -> None:
        """Piyasa sonu: yuksek olasilikli pozisyonu mid ile kapat (paper settlement)."""
        if position == Side.FLAT:
            return
        exit_px = _position_mid_row(row, position)
        if exit_px is None:
            exit_px = _px(row, position, buy=False)
        act = (
            Action.SETTLE_UP.value
            if position == Side.UP
            else Action.SETTLE_DOWN.value
        )
        _finish_round(row, t, rsi, exit_px, act, "settlement")

    def _token_mid_row(row: pd.Series, side: Side) -> float | None:
        if side == Side.UP:
            v = row.get("yes_mid")
        else:
            v = row.get("no_mid")
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)

    def _open_position(row: pd.Series, t: pd.Timestamp, rsi: float, side: Side) -> None:
        nonlocal position, entry_time, entry_price, last_trade_time, skipped_extreme_price
        mid = _token_mid_row(row, side)
        if not is_entry_price_safe(mid, cfg):
            skipped_extreme_price += 1
            return
        buy_px = _px(row, side, buy=True)
        act = Action.BUY_UP if side == Side.UP else Action.BUY_DOWN
        result.legs.append(Leg(act.value, t, buy_px, rsi, side.value))
        position = side
        entry_time = t
        entry_price = buy_px
        last_trade_time = t

    def _cooldown_ok(t: pd.Timestamp) -> bool:
        if cfg.cooldown_seconds <= 0 or last_trade_time is None:
            return True
        return (t - last_trade_time).total_seconds() >= cfg.cooldown_seconds

    def _apply_resolution_for_open_position(
        row: pd.Series, t: pd.Timestamp, rsi: float, left: float
    ) -> bool:
        """True → bar islenmedi (resolution islemi yapildi)."""
        nonlocal resolution_closes, resolution_holds
        if position == Side.FLAT:
            return False
        phase = resolution_phase(left, res_cfg, slug)
        pos_mid = _position_mid_row(row, position)
        do_close, why = should_force_close(pos_mid, phase, res_cfg)
        if do_close:
            _close_position(row, t, rsi, resolution_reason=why)
            resolution_closes += 1
            return True
        if why.startswith("hold_gte"):
            resolution_holds += 1
        return False

    for i in range(len(bars)):
        row = bars.iloc[i]
        t = row["time"]
        rsi = row["rsi"]
        if pd.isna(rsi):
            continue

        left = _minutes_left_at(t)
        if left is not None and _apply_resolution_for_open_position(row, t, rsi, left):
            continue

        entries_blocked = left is not None and left <= entry_window_m

        if row["cross_up"]:
            result.signals_up += 1
            if not _cooldown_ok(t):
                result.skipped_cooldown += 1
                continue
            if position == Side.UP and cfg.ignore_same_side_signal:
                continue
            if position == Side.DOWN:
                _close_position(row, t, rsi)
            if position in (Side.FLAT, Side.DOWN):
                if position == Side.FLAT and entries_blocked:
                    skipped_resolution_entry += 1
                    continue
                _open_position(row, t, rsi, Side.UP)

        elif row["cross_down"]:
            result.signals_down += 1
            if not _cooldown_ok(t):
                result.skipped_cooldown += 1
                continue
            if position == Side.DOWN and cfg.ignore_same_side_signal:
                continue
            had_up = position == Side.UP
            if had_up:
                _close_position(row, t, rsi)
            if had_up or (
                position == Side.FLAT and cfg.enter_down_when_flat_on_cross_down
            ):
                if position == Side.FLAT and entries_blocked:
                    skipped_resolution_entry += 1
                    continue
                _open_position(row, t, rsi, Side.DOWN)

    if position != Side.FLAT and len(bars) > 0:
        row = bars.iloc[-1]
        t = row["time"]
        rsi = float(row["rsi"]) if not pd.isna(row["rsi"]) else 50.0
        left = _minutes_left_at(t)
        phase = resolution_phase(left or 0.0, res_cfg, slug)
        pos_mid = _position_mid_row(row, position)
        do_close, why = should_force_close(pos_mid, phase, res_cfg)
        if do_close:
            _close_position(row, t, rsi, resolution_reason=why or "eof")
            resolution_closes += 1
        elif why.startswith("hold_gte"):
            _settle_position(row, t, rsi)
            resolution_settlements += 1
        else:
            _close_position(row, t, rsi, resolution_reason="eof_flat")

    result.resolution_closes = resolution_closes
    result.resolution_holds = resolution_holds
    result.resolution_settlements = resolution_settlements
    result.skipped_resolution_entry = skipped_resolution_entry
    result.skipped_extreme_price = skipped_extreme_price
    return result


def format_report(result: BacktestResultV2) -> str:
    c = result.config
    s = result.summary_dict()
    res = c.resolution
    lines = [
        f"CSV: {result.csv_path}",
        (
            f"RSI period={c.rsi_period} wilder={c.use_wilder}  "
            f"resample={c.resample_seconds}s  k={c.size:g}"
        ),
        (
            f"Cross UP={c.cross_up_level}  Cross DOWN={c.cross_down_level}  "
            f"cooldown={c.cooldown_seconds}s"
        ),
        (
            f"Resolution: son {res.force_close_minutes:g} dk kapat | "
            f"tut mid>={res.hold_force_close_if_mid_gte} | "
            f"dump mid<{res.dump_immediately_if_mid_lt} | "
            f"giris engeli son {res.block_entries_minutes:g} dk"
        ),
        (
            f"Round-trip: {s['round_trips']}  |  Legs: {s['legs']}  |  "
            f"Sinyal up/down: {s['signals_cross_up']}/{s['signals_cross_down']}"
        ),
        (
            f"Win: {s['wins']}  Loss: {s['losses']}  "
            f"Rate: {s['win_rate_pct']}%  |  PnL: {s['total_pnl']:.4f}  "
            f"Avg: {s['avg_pnl']:.4f}  DD: {s['max_drawdown']:.4f}"
        ),
        (
            f"Resolution: kapat={s['resolution_closes']}  tut(bar)={s['resolution_holds']}  "
            f"settlement={s['resolution_settlements']}  "
            f"atlanan giris={s['skipped_resolution_entry']}"
        ),
        f"Cooldown atlanan: {s['skipped_cooldown']}",
        "",
        "Son 12 leg:",
    ]
    for leg in result.legs[-12:]:
        note = f"  ({leg.note})" if leg.note else ""
        lines.append(
            f"  {leg.time}  {leg.action:22s}  px={leg.price:.3f}  "
            f"rsi={leg.rsi:.1f}  -> {leg.side_after}{note}"
        )
    return "\n".join(lines)
