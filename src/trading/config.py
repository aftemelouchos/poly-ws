"""Trading configuration (env + optional YAML)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv

from src.trading.types import TradingMode


@dataclass(frozen=True)
class TradingConfig:
    mode: TradingMode = TradingMode.PAPER
    # live: gercek emir; paper: simulasyon
    dry_run: bool = False
    # live client var ama emir gondermez (log only) — paper disinda nadir

    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1  # 0 EOA, 1 POLY_PROXY, 2 GNOSIS_SAFE, 3 POLY_1271

    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    ws_ping_seconds: int = 10

    paper_initial_usdc: float = 10_000.0
    default_order_size: float = 10.0


def _resolve_funder() -> str:
    return (
        os.getenv("FUNDER_ADDRESS", "").strip()
        or os.getenv("POLY_FUNDER", "").strip()
        or os.getenv("PROXY_ADDRESS", "").strip()
        or os.getenv("DEPOSIT_WALLET_ADDRESS", "").strip()
    )


def load_trading_config(
    *,
    env_path: str | Path = ".env",
    yaml_path: str | Path | None = "config/trading.yaml",
    mode_override: str | None = None,
) -> TradingConfig:
    load_dotenv(env_path)

    yaml_defaults: dict = {}
    if yaml_path and Path(yaml_path).is_file():
        with open(yaml_path, encoding="utf-8") as f:
            yaml_defaults = yaml.safe_load(f) or {}

    def _get(key: str, env_keys: tuple[str, ...], default: str = "") -> str:
        for ek in env_keys:
            v = os.getenv(ek)
            if v:
                return v.strip()
        return str(yaml_defaults.get(key, default)).strip()

    mode_str = (mode_override or _get("mode", ("TRADING_MODE", "MODE"), "paper")).lower()
    mode = TradingMode.LIVE if mode_str == "live" else TradingMode.PAPER

    sig = int(_get("signature_type", ("SIGNATURE_TYPE",), "1") or "1")

    return TradingConfig(
        mode=mode,
        dry_run=_get("dry_run", ("TRADING_DRY_RUN",)).lower() in ("1", "true", "yes"),
        private_key=_get("private_key", ("PRIVATE_KEY", "PK")),
        funder_address=_resolve_funder(),
        signature_type=sig,
        clob_host=_get("clob_host", ("CLOB_HOST",), "https://clob.polymarket.com"),
        chain_id=int(_get("chain_id", ("CHAIN_ID",), "137") or "137"),
        market_ws_url=_get(
            "market_ws_url",
            ("MARKET_WS_URL",),
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        user_ws_url=_get(
            "user_ws_url",
            ("USER_WS_URL",),
            "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        ),
        ws_ping_seconds=int(_get("ws_ping_seconds", ("WS_PING_SECONDS",), "10") or "10"),
        paper_initial_usdc=float(
            _get("paper_initial_usdc", ("PAPER_INITIAL_USDC",), "10000") or "10000"
        ),
        default_order_size=float(
            _get("default_order_size", ("DEFAULT_ORDER_SIZE",), "10") or "10"
        ),
    )
