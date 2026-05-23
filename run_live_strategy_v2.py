#!/usr/bin/env python3
"""
Live Strategy v2 — UP RSI cross (Stock RSI) Polymarket trading bot.

Kullanim:
  python run_live_strategy_v2.py --rsi-period 8 --cross-up 45 --cross-down 55 \
      -k 6 --res-force 1 --res-hold 0.7 --res-dump 0.5

Linux servis: bkz README.md (systemd / tmux).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.live_strategy_v2 import StockRsiLiveEngine, load_live_strategy_v2_config
from src.trading.config import load_trading_config


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/live_strategy_v2.log", encoding="utf-8"),
        ],
    )
    # Strateji ve runtime ana loglari konsola
    for name in (
        "src.live_strategy_v2.engine",
        "src.trading.hft_runtime",
    ):
        logging.getLogger(name).setLevel(logging.INFO)
    # WS / 3rd party gurultusu
    for name in (
        "httpx",
        "httpcore",
        "hpack",
        "py_clob_client_v2",
        "websockets",
        "src.trading.gateway",
        "src.ws_market",
        "src.ws_user",
        "src.market_resolver",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


async def main_async(args: argparse.Namespace) -> None:
    live_cfg = load_live_strategy_v2_config(args.config)
    trading_cfg = load_trading_config()

    if args.dry_run:
        trading_cfg = replace(trading_cfg, dry_run=True)

    s = live_cfg.strategy
    updates: dict = {}
    if args.rsi_period is not None:
        updates["rsi_period"] = args.rsi_period
    if args.cross_up is not None:
        updates["cross_up_level"] = args.cross_up
    if args.cross_down is not None:
        updates["cross_down_level"] = args.cross_down
    if args.k is not None:
        updates["size"] = args.k

    res = s.resolution
    res_kw: dict = {}
    if args.res_block is not None:
        res_kw["block_entries_minutes"] = args.res_block
    if args.res_force is not None:
        res_kw["force_close_minutes"] = args.res_force
    if args.res_hold is not None:
        res_kw["hold_force_close_if_mid_gte"] = args.res_hold
    if args.res_dump is not None:
        res_kw["dump_immediately_if_mid_lt"] = args.res_dump
    if res_kw:
        updates["resolution"] = replace(res, **res_kw)

    if updates:
        s = replace(s, **updates)
        live_cfg = live_cfg.model_copy(update={"strategy": s})
    if args.k is not None:
        trading_cfg = replace(trading_cfg, default_order_size=args.k)

    strat = live_cfg.strategy
    res_cfg = strat.resolution
    print(
        f"Strateji RSI{strat.rsi_period} {strat.cross_up_level:g}/{strat.cross_down_level:g} "
        f"k={strat.size:g} | Res son {res_cfg.force_close_minutes:g}dk "
        f"hold>={res_cfg.hold_force_close_if_mid_gte} dump<{res_cfg.dump_immediately_if_mid_lt}"
    )

    engine = StockRsiLiveEngine(live_cfg, trading_cfg)
    await engine.start()

    stop = asyncio.Event()

    def _stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    mode = trading_cfg.mode.value
    dry = trading_cfg.dry_run
    print(f"Live v2 calisiyor | mode={mode} dry_run={dry} | Ctrl+C ile dur\n")

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await engine.stop()
        print("Durduruldu.")


def main() -> None:
    p = argparse.ArgumentParser(description="Live Strategy v2 (Stock RSI)")
    p.add_argument("--config", default="config/strategy_v2_live.yaml")
    p.add_argument("--dry-run", action="store_true", help="Live client, emir yok")
    p.add_argument("--rsi-period", type=int, default=None)
    p.add_argument("--cross-up", type=float, default=None)
    p.add_argument("--cross-down", type=float, default=None)
    p.add_argument("-k", type=float, default=None, help="Share / islem")
    g = p.add_argument_group("Resolution")
    g.add_argument("--res-block", type=float, default=None)
    g.add_argument("--res-force", type=float, default=None)
    g.add_argument("--res-hold", type=float, default=None)
    g.add_argument("--res-dump", type=float, default=None)
    args = p.parse_args()
    setup_logging()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nDurduruldu.")


if __name__ == "__main__":
    main()
