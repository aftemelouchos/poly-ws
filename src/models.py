"""Domain modelleri — token, kitap, market, durum."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TokenLabel(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    last_trade_side: str | None = None
    updated_at: float = 0.0  # time.time() — WS book / price_change
    has_snapshot: bool = False  # tam "book" event alindi mi

    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        if self.last_trade_price is not None:
            return self.last_trade_price
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return None

    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class OpenOrder:
    order_id: str
    token: TokenLabel
    side: Side
    price: float
    size: float
    remaining: float


@dataclass
class Fill:
    order_id: str
    token: TokenLabel
    side: Side
    price: float
    size: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class MarketInfo:
    slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    tick_size: float
    neg_risk: bool
    question: str = ""
    end_date: str | None = None
    active: bool = True
    closed: bool = False


@dataclass
class BookState:
    """Merkezi durum — kitaplar, pozisyonlar, fillsler."""

    market: MarketInfo | None = None
    books: dict[str, OrderBook] = field(default_factory=dict)
    positions: dict[TokenLabel, float] = field(
        default_factory=lambda: {TokenLabel.YES: 0.0, TokenLabel.NO: 0.0}
    )
    open_orders: dict[str, OpenOrder] = field(default_factory=dict)
    tick_size: float = 0.01
    ws_connected: bool = False
    mode: str = "paper"
    minutes_to_expiry: float | None = None

    def token_label(self, asset_id: str) -> TokenLabel | None:
        if not self.market:
            return None
        if asset_id == self.market.yes_token_id:
            return TokenLabel.YES
        if asset_id == self.market.no_token_id:
            return TokenLabel.NO
        return None

    def asset_id(self, token: TokenLabel) -> str | None:
        if not self.market:
            return None
        if token == TokenLabel.YES:
            return self.market.yes_token_id
        return self.market.no_token_id

    def book_for(self, token: TokenLabel) -> OrderBook:
        aid = self.asset_id(token)
        if aid is None:
            return OrderBook()
        return self.books.setdefault(aid, OrderBook())

    def apply_fill(self, fill: Fill) -> None:
        signed = fill.size if fill.side == Side.BUY else -fill.size
        self.positions[fill.token] += signed
