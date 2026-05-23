"""Live strategy v2 configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from src.strategy_v2.config import StockRsiConfig


class MarketLiveConfig(BaseModel):
    slug: str = ""
    slug_pattern: str = "btc-updown-5m-*"
    auto_rollover: bool = True
    rollover_check_seconds: int = 15


class LiveStrategyV2Config(BaseModel):
    market: MarketLiveConfig = Field(default_factory=MarketLiveConfig)
    strategy: StockRsiConfig = Field(default_factory=StockRsiConfig)
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    balance_poll_seconds: int = 60
    log_every_bar: bool = True


def load_live_strategy_v2_config(path: str | Path = "config/strategy_v2_live.yaml") -> LiveStrategyV2Config:
    p = Path(path)
    if not p.is_file():
        return LiveStrategyV2Config()
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return LiveStrategyV2Config.model_validate(raw)
