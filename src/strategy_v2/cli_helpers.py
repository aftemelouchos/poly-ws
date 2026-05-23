"""Backtest / optimizasyon CLI — ortak argumanlar."""

from __future__ import annotations

import argparse
from dataclasses import asdict

from src.strategy_v2.config import StockRsiConfig
from src.strategy_v2.resolution import ResolutionConfig


def add_resolution_arguments(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("Resolution (son P dk)")
    g.add_argument(
        "--res-block",
        type=float,
        default=1.0,
        help="Son P dk yeni pozisyon acma yok (dk)",
    )
    g.add_argument(
        "--res-force",
        type=float,
        default=1.0,
        help="Son P dk acik pozisyonlari sartli kapat (dk)",
    )
    g.add_argument(
        "--res-hold",
        type=float,
        default=0.90,
        help="Token mid >= bu → son P dk satma, settlement (0-1)",
    )
    g.add_argument(
        "--res-dump",
        type=float,
        default=0.20,
        help="Son P dk mid < bu → hemen sat (0-1)",
    )


def resolution_from_args(args: argparse.Namespace) -> ResolutionConfig:
    return ResolutionConfig(
        block_entries_minutes=args.res_block,
        force_close_minutes=args.res_force,
        hold_force_close_if_mid_gte=args.res_hold,
        dump_immediately_if_mid_lt=args.res_dump,
    )


def build_stock_rsi_config(args: argparse.Namespace) -> StockRsiConfig:
    resample = None if getattr(args, "resample", 1.0) <= 0 else args.resample
    k = getattr(args, "k", None)
    size = float(k) if k is not None else float(getattr(args, "size", 1.0))
    return StockRsiConfig(
        rsi_period=args.rsi_period,
        cross_up_level=args.cross_up,
        cross_down_level=args.cross_down,
        resample_seconds=resample,
        size=size,
        use_wilder=not getattr(args, "no_wilder", False),
        cooldown_seconds=getattr(args, "cooldown", 0.0),
        enter_down_when_flat_on_cross_down=not getattr(args, "no_down_when_flat", False),
        resolution=resolution_from_args(args),
    )


def config_summary_dict(cfg: StockRsiConfig) -> dict:
    d = asdict(cfg)
    return d
