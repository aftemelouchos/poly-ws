#!/usr/bin/env python3
"""
Strategy v2 — UP token Stock RSI cross backtest (+ resolution).

Kullanım:
  python scripts/backtest_strategy_v2.py -f data/ws/5m/....csv
  python scripts/backtest_strategy_v2.py -f ... --cross-up 20 --cross-down 60 -k 6
  python scripts/backtest_strategy_v2.py -f ... --res-force 1 --res-hold 0.9 --res-dump 0.2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy_v2.backtest import format_report, run_backtest
from src.strategy_v2.cli_helpers import add_resolution_arguments, build_stock_rsi_config, config_summary_dict


def main() -> None:
    p = argparse.ArgumentParser(description="Strategy v2 — UP RSI cross backtest")
    p.add_argument("--file", "-f", type=Path, required=True)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--cross-up", type=float, default=50.0)
    p.add_argument("--cross-down", type=float, default=50.0)
    p.add_argument("--resample", type=float, default=1.0)
    p.add_argument("-k", "--k", type=float, default=None)
    p.add_argument("--size", type=float, default=1.0, help=argparse.SUPPRESS)
    p.add_argument("--cooldown", type=float, default=0.0)
    p.add_argument("--no-wilder", action="store_true")
    p.add_argument("--no-down-when-flat", action="store_true")
    p.add_argument("--json", type=Path)
    p.add_argument("--trades-csv", type=Path)
    add_resolution_arguments(p)
    args = p.parse_args()

    if not args.file.is_file():
        raise SystemExit(f"Dosya yok: {args.file}")

    cfg = build_stock_rsi_config(args)
    result = run_backtest(args.file, cfg)
    print(format_report(result))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "csv": str(args.file),
            "config": config_summary_dict(cfg),
            "summary": result.summary_dict(),
            "round_trips": [
                {
                    "side": r.side,
                    "entry_time": str(r.entry_time),
                    "exit_time": str(r.exit_time),
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "pnl": r.pnl,
                    "hold_seconds": r.hold_seconds,
                    "exit_kind": r.exit_kind,
                }
                for r in result.round_trips
            ],
            "legs": [
                {
                    "action": lg.action,
                    "time": str(lg.time),
                    "price": lg.price,
                    "rsi": lg.rsi,
                    "side_after": lg.side_after,
                    "note": lg.note,
                }
                for lg in result.legs
            ],
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")

    if args.trades_csv:
        import pandas as pd

        args.trades_csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {
                    "side": r.side,
                    "entry_time": r.entry_time,
                    "exit_time": r.exit_time,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "pnl": r.pnl,
                    "hold_seconds": r.hold_seconds,
                    "exit_kind": r.exit_kind,
                }
                for r in result.round_trips
            ]
        ).to_csv(args.trades_csv, index=False)
        print(f"Trades: {args.trades_csv}")


if __name__ == "__main__":
    main()
