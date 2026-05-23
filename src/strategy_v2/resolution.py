"""Piyasa sonu / resolution — pozisyon kapatma ve giriş engeli."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class ResolutionConfig:
    """
    Son P dakika (force_close_minutes) kurallari:
    - Yeni pozisyon acma yok (block_entries_minutes; 5m'de force_close ile ayni tutun)
    - Acik pozisyon: token fiyati (mid) = resolve olasiligi
      * mid >= hold → kapatma (lehine cozulme beklenir)
      * mid < dump → hemen sat (zarari kes)
      * arada → son P dk icinde kapat
    """
    block_entries_minutes: float = 1.0
    force_close_minutes: float = 1.0
    cancel_signals_minutes: float = 3.0
    hold_force_close_if_mid_gte: float = 0.90
    dump_immediately_if_mid_lt: float = 0.20


SLUG_EPOCH_RE = re.compile(r"btc-updown-(?:5m|15m|4h)-(\d+)")


def interval_seconds_from_slug(slug: str) -> int:
    if "-5m-" in slug:
        return 300
    if "-15m-" in slug:
        return 900
    if "-4h-" in slug:
        return 4 * 3600
    return 300


def market_end_from_slug(slug: str) -> datetime | None:
    """Slug epoch = pencere baslangici; bitis = baslangic + interval."""
    m = SLUG_EPOCH_RE.search(slug)
    if not m:
        return None
    start = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    return start + timedelta(seconds=interval_seconds_from_slug(slug))


def minutes_to_expiry(now: datetime, end_dt: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (end_dt - now).total_seconds() / 60.0


def entries_window_minutes(slug: str, cfg: ResolutionConfig) -> float:
    """Son P dk: yeni giris engeli (block ile force_close birlesik)."""
    block_m = effective_block_entries_minutes(slug, cfg) if slug else cfg.block_entries_minutes
    return max(block_m, cfg.force_close_minutes)


def effective_block_entries_minutes(slug: str, cfg: ResolutionConfig) -> float:
    """
    block_entries, piyasa suresinden uzunsa (5m + 5dk) tum pencere kilitlenir.
    En az force_close + 1 dk islem alani birakilir.
    """
    interval_m = interval_seconds_from_slug(slug) / 60.0
    headroom = cfg.force_close_minutes + 1.0
    cap = max(0.0, interval_m - headroom)
    if cap <= 0:
        return min(cfg.block_entries_minutes, interval_m * 0.5)
    return min(cfg.block_entries_minutes, cap)


def resolution_phase(
    minutes_left: float,
    cfg: ResolutionConfig,
    slug: str = "",
) -> str:
    block_m = (
        effective_block_entries_minutes(slug, cfg)
        if slug
        else cfg.block_entries_minutes
    )
    if minutes_left <= 0:
        return "expired"
    if minutes_left <= cfg.force_close_minutes:
        return "force_close"
    if minutes_left <= block_m:
        return "block_entries"
    return ""


def in_resolution_close_window(phase: str) -> bool:
    """Son P dk veya piyasa bitti — pozisyon kapatma kurallari burada gecerli."""
    return phase in ("force_close", "expired")


def should_force_close(
    position_mid: float | None,
    phase: str,
    cfg: ResolutionConfig,
) -> tuple[bool, str]:
    """
    Sadece son P dakikada (force_close / expired) pozisyon kapatma karari.
    Token mid'i ~ resolve olasiligi: 0.90 lehine, 0.20 aleyhine.
    Donus: (kapat, sebep)
    """
    if position_mid is None:
        return False, "no_mid"

    if not in_resolution_close_window(phase):
        return False, "outside_window"

    if position_mid >= cfg.hold_force_close_if_mid_gte:
        return False, f"hold_gte_{cfg.hold_force_close_if_mid_gte:.2f}"

    if position_mid < cfg.dump_immediately_if_mid_lt:
        return True, f"dump_lt_{cfg.dump_immediately_if_mid_lt:.2f}"

    return True, f"window_{phase}"
