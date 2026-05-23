"""Trading domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class OrderRequest:
    token_id: str
    price: float
    size: float
    tick_size: str = "0.01"
    neg_risk: bool = False
    order_type: str = "GTC"
    post_only: bool = False


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    side: str = ""
    token_id: str = ""
    price: float = 0.0
    size: float = 0.0
    dry_run: bool = False
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssetBalance:
    asset_type: str
    token_id: str | None
    balance: float
    allowance: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountBalances:
    mode: TradingMode
    funder: str
    signer: str
    usdc: AssetBalance | None = None
    tokens: dict[str, AssetBalance] = field(default_factory=dict)
