"""Stock RSI (UP token) — parametrik cross stratejisi."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.strategy_v2.resolution import ResolutionConfig


@dataclass(frozen=True)
class StockRsiConfig:
    # RSI (UP / yes_mid üzerinde)
    rsi_period: int = 14
    use_wilder: bool = True

    # Cross seviyeleri (UP token RSI)
    cross_up_level: float = 50.0    # altından yukarı kesince → UP al
    cross_down_level: float = 50.0  # üstünden aşağı kesince → UP sat, DOWN al

    # Seri: tick gürültüsünü azaltmak için yeniden örnekleme (saniye)
    resample_seconds: float | None = 1.0

    # Her alım/satımda işlem gören pay (share) sayısı — k
    size: float = 1.0
    # Cross down + pozisyon yokken sadece DOWN aç
    enter_down_when_flat_on_cross_down: bool = True
    # Aynı yönde tekrar cross (zaten UP iken cross up) — yeniden alım yok
    ignore_same_side_signal: bool = True
    # İki işlem arası minimum süre (sn)
    cooldown_seconds: float = 0.0
    # Alim yok: token fiyati bu aralik disinda (asiri uç / settlement bolgesi)
    min_entry_price: float = 0.03
    max_entry_price: float = 0.97
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)


def is_entry_price_safe(price: float | None, cfg: StockRsiConfig) -> bool:
    if price is None:
        return False
    return cfg.min_entry_price <= price <= cfg.max_entry_price
