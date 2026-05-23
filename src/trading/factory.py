"""Trading gateway factory."""

from __future__ import annotations

from src.trading.clob_client import create_clob_client
from src.trading.config import TradingConfig
from src.trading.gateway import LiveTradingGateway, PaperTradingGateway, TradingGateway
from src.trading.types import TradingMode


def create_gateway(cfg: TradingConfig) -> TradingGateway:
    if cfg.mode == TradingMode.PAPER:
        return PaperTradingGateway(cfg)
    client = create_clob_client(cfg)
    return LiveTradingGateway(cfg, client)
