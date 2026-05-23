"""WebSocket konfigurasyon — public market + authenticated user kanallari."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WebSocketConfig:
    market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    ping_interval_seconds: int = 10
    reconnect_backoff_seconds: list[int] = field(
        default_factory=lambda: [1, 2, 5, 10]
    )
    custom_feature_enabled: bool = True
