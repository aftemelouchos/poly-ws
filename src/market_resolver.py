"""Gamma API market resolution and rollover."""

from __future__ import annotations

import fnmatch
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from src.models import MarketInfo

_INTERVAL_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "4h": 4 * 3600,
}


def _pattern_prefix(pattern: str) -> str:
    """btc-updown-5m-* → btc-updown-5m (rstrip('*') yanlışlıkla sondaki 'm'yi de siler)."""
    return pattern.removesuffix("*").rstrip("-")


def _parse_tick_size(value: Any) -> float:
    if value is None:
        return 0.01
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    mapping = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}
    return mapping.get(s, float(s) if s else 0.01)


def _extract_token_ids(market: dict[str, Any]) -> tuple[str, str]:
    """Return (yes_token_id, no_token_id)."""
    tokens = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if not tokens or len(tokens) < 2:
        raise ValueError("Market missing clob token ids")
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if outcomes and len(outcomes) >= 2:
        outcome_lower = [str(o).lower() for o in outcomes]
        if "up" in outcome_lower[0] or "yes" in outcome_lower[0]:
            return str(tokens[0]), str(tokens[1])
        if "up" in outcome_lower[1] or "yes" in outcome_lower[1]:
            return str(tokens[1]), str(tokens[0])
    return str(tokens[0]), str(tokens[1])


class MarketResolver:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=15.0, follow_redirects=True
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def resolve_by_slug(self, slug: str) -> MarketInfo:
        client = await self._get_client()
        # Try event slug first (BTC 4h markets are often under events)
        for path in (f"/events/slug/{slug}", f"/markets/slug/{slug}"):
            r = await client.get(path)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                data = data[0]
            return self._parse_payload(slug, data)
        # Fallback query param
        r = await client.get("/events", params={"slug": slug})
        r.raise_for_status()
        items = r.json()
        if not items:
            r2 = await client.get("/markets", params={"slug": slug})
            r2.raise_for_status()
            items = r2.json()
        if not items:
            raise ValueError(f"No market found for slug: {slug}")
        return self._parse_payload(slug, items[0] if isinstance(items, list) else items)

    def _parse_payload(self, slug: str, data: dict[str, Any]) -> MarketInfo:
        markets = data.get("markets") or []
        market = markets[0] if markets else data
        if not market.get("conditionId") and not market.get("condition_id"):
            if data.get("conditionId") or data.get("condition_id"):
                market = {**data, **market}
        condition_id = market.get("conditionId") or market.get("condition_id") or ""
        yes_id, no_id = _extract_token_ids(market)
        tick = _parse_tick_size(
            market.get("orderPriceMinTickSize")
            or market.get("minimum_tick_size")
            or market.get("tick_size")
        )
        neg_risk = bool(market.get("negRisk") or market.get("neg_risk") or False)
        return MarketInfo(
            slug=slug,
            condition_id=condition_id,
            yes_token_id=yes_id,
            no_token_id=no_id,
            tick_size=tick,
            neg_risk=neg_risk,
            question=market.get("question") or data.get("title") or "",
            end_date=market.get("endDate") or market.get("end_date"),
            active=bool(market.get("active", True)),
            closed=bool(market.get("closed", False)),
        )

    @staticmethod
    def _interval_seconds_from_pattern(pattern: str) -> int | None:
        # btc-updown-5m-* → 5m (sondaki * nedeniyle -5m- regex'i tutmaz)
        m = re.search(r"updown-(5m|15m|4h)-", pattern)
        if not m:
            return None
        return _INTERVAL_SECONDS.get(m.group(1))

    @staticmethod
    def _market_end_dt(market: MarketInfo) -> datetime | None:
        if not market.end_date:
            return None
        try:
            return datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _is_live_market(self, market: MarketInfo, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if market.closed or not market.active:
            return False
        end_dt = self._market_end_dt(market)
        if end_dt is None:
            return True
        return end_dt > now

    async def _resolve_slug_or_none(self, slug: str) -> MarketInfo | None:
        try:
            return await self.resolve_by_slug(slug)
        except (httpx.HTTPStatusError, ValueError):
            return None

    async def resolve_by_epoch_window(
        self, pattern: str, around_ts: int | None = None, look_back: int = 2, look_ahead: int = 4
    ) -> MarketInfo | None:
        """
        Slug epoch = pencere başlangıcı (ör. btc-updown-5m-{unix}).
        Gamma /events listesi bu piyasaları döndürmediği için doğrudan slug ile çözülür.
        """
        interval = self._interval_seconds_from_pattern(pattern)
        prefix = _pattern_prefix(pattern)
        if interval is None:
            return None

        now_ts = int(around_ts or time.time())
        base_epoch = (now_ts // interval) * interval
        now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        live: list[tuple[datetime, MarketInfo]] = []

        for off in range(-look_back, look_ahead + 1):
            ep = base_epoch + off * interval
            slug = f"{prefix}-{ep}"
            market = await self._resolve_slug_or_none(slug)
            if market and self._is_live_market(market, now):
                end_dt = self._market_end_dt(market)
                if end_dt:
                    live.append((end_dt, market))

        if not live:
            return None
        # Şu anki pencere: en yakın gelecek bitiş zamanı
        live.sort(key=lambda x: x[0])
        return live[0][1]

    async def _collect_candidates(self, pattern: str) -> list[tuple[str, dict]]:
        client = await self._get_client()
        prefix = _pattern_prefix(pattern)
        candidates: list[tuple[str, dict]] = []

        r = await client.get(
            "/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "startDate",
                "ascending": "true",
            },
        )
        if r.status_code == 200:
            for ev in r.json():
                s = ev.get("slug") or ""
                if fnmatch.fnmatch(s, pattern) or s.startswith(prefix):
                    candidates.append((s, ev))

        if not candidates:
            q = prefix.replace("-", " ").replace("btc", "bitcoin")
            sr = await client.get("/public-search", params={"q": q})
            if sr.status_code == 200:
                for ev in sr.json().get("events", []):
                    s = ev.get("slug") or ""
                    if fnmatch.fnmatch(s, pattern) or s.startswith(prefix):
                        candidates.append((s, ev))

        candidates.sort(key=lambda x: x[0])
        return candidates

    @staticmethod
    def _slug_epoch(slug: str, prefix: str) -> int | None:
        """btc-updown-4h-1779278400 → 1779278400"""
        base = _pattern_prefix(prefix) if "*" in prefix else prefix
        if not slug.startswith(base):
            return None
        tail = slug[len(base) :].lstrip("-")
        try:
            return int(tail)
        except ValueError:
            return None

    async def find_next_by_pattern(
        self, pattern: str, current_slug: str
    ) -> MarketInfo | None:
        """Aktif piyasalarda current'tan sonraki slug'ı bul (timestamp veya sıra)."""
        interval = self._interval_seconds_from_pattern(pattern)
        prefix = _pattern_prefix(pattern)
        current_epoch = self._slug_epoch(current_slug, prefix)
        if interval is not None and current_epoch is not None:
            for step in range(1, 8):
                ep = current_epoch + step * interval
                slug = f"{prefix}-{ep}"
                market = await self._resolve_slug_or_none(slug)
                if market and self._is_live_market(market):
                    return market

        candidates = await self._collect_candidates(pattern)
        if not candidates:
            return None

        prefix = _pattern_prefix(pattern)
        current_epoch = self._slug_epoch(current_slug, prefix)

        # Timestamp'li slug'larda: strictly greater epoch
        if current_epoch is not None:
            timed: list[tuple[int, str, dict]] = []
            for s, ev in candidates:
                ep = self._slug_epoch(s, prefix)
                if ep is not None:
                    timed.append((ep, s, ev))
            timed.sort(key=lambda x: x[0])
            for ep, s, ev in timed:
                if ep > current_epoch:
                    return self._parse_payload(s, ev)

        # Fallback: alfabetik sırada current'tan sonraki
        candidates.sort(key=lambda x: x[0])
        found_current = False
        for s, ev in candidates:
            if s == current_slug:
                found_current = True
                continue
            if found_current:
                return self._parse_payload(s, ev)

        # Current listede yoksa (süresi dolmuş): en yakın gelecek veya en güncel aktif
        if current_epoch is not None:
            for ep, s, ev in sorted(
                [
                    (self._slug_epoch(s, prefix), s, ev)
                    for s, ev in candidates
                    if self._slug_epoch(s, prefix) is not None
                ],
                key=lambda x: x[0],
            ):
                if ep > current_epoch:
                    return self._parse_payload(s, ev)

        # Son çare: aktif listedeki en son slug (farklı current ise)
        if candidates and candidates[-1][0] != current_slug:
            return self._parse_payload(candidates[-1][0], candidates[-1][1])
        return None

    async def find_active_latest(self, pattern: str) -> MarketInfo | None:
        """Pattern'e uyan şu anki canlı piyasa (epoch penceresi ile)."""
        probed = await self.resolve_by_epoch_window(pattern)
        if probed:
            return probed

        candidates = await self._collect_candidates(pattern)
        if not candidates:
            return None
        prefix = _pattern_prefix(pattern)
        now = datetime.now(timezone.utc)
        live: list[tuple[datetime, str, dict]] = []
        for s, ev in candidates:
            info = self._parse_payload(s, ev)
            end_dt = self._market_end_dt(info)
            if end_dt and end_dt > now and not info.closed:
                live.append((end_dt, s, ev))
        if live:
            live.sort(key=lambda x: x[0])
            return self._parse_payload(live[0][1], live[0][2])

        best: tuple[int, str, dict] | None = None
        for s, ev in candidates:
            ep = self._slug_epoch(s, prefix)
            key = ep if ep is not None else 0
            if best is None or key > best[0]:
                best = (key, s, ev)
        return None
