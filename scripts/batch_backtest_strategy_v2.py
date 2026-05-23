#!/usr/bin/env python3
"""
Strategy v2 toplu backtest — data/ws/5m ilk N CSV, kümülatif PnL.

Kullanım:
  python scripts/batch_backtest_strategy_v2.py --n 10 --k 10
  python scripts/batch_backtest_strategy_v2.py --n 15 --cross-up 20 --cross-down 60 -k 6
  python scripts/batch_backtest_strategy_v2.py --n 10 --res-force 1 --res-hold 0.9 --res-dump 0.2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy_v2.backtest import run_backtest
from src.strategy_v2.cli_helpers import add_resolution_arguments, build_stock_rsi_config, config_summary_dict
from src.strategy_v2.config import StockRsiConfig

SLUG_EPOCH_RE = re.compile(r"btc-updown-5m-(\d+)_")


def list_5m_csvs(
    data_dir: Path,
    *,
    n: int | None,
    order: str = "epoch",
) -> list[Path]:
    files = [p for p in data_dir.glob("*.csv") if p.is_file()]
    if not files:
        raise FileNotFoundError(f"CSV yok: {data_dir}")

    def _epoch_key(p: Path) -> int:
        m = SLUG_EPOCH_RE.search(p.name)
        return int(m.group(1)) if m else 0

    if order == "mtime":
        files.sort(key=lambda p: p.stat().st_mtime)
    else:
        files.sort(key=_epoch_key)

    if n is not None and n > 0:
        files = files[:n]
    return files


def run_batch(
    files: list[Path],
    cfg: StockRsiConfig,
) -> pd.DataFrame:
    rows: list[dict] = []
    cum_pnl = 0.0
    cum_trades = 0
    cum_wins = 0

    for i, path in enumerate(files, start=1):
        result = run_backtest(path, cfg)
        s = result.summary_dict()
        file_pnl = s["total_pnl"]
        cum_pnl += file_pnl
        cum_trades += s["round_trips"]
        cum_wins += s["wins"]

        m = SLUG_EPOCH_RE.search(path.name)
        epoch = int(m.group(1)) if m else 0

        rows.append(
            {
                "idx": i,
                "file": path.name,
                "slug_epoch": epoch,
                "shares_k": cfg.size,
                "trades": s["round_trips"],
                "wins": s["wins"],
                "losses": s["losses"],
                "win_rate_pct": s["win_rate_pct"],
                "file_pnl": file_pnl,
                "avg_pnl": s["avg_pnl"],
                "max_dd": s["max_drawdown"],
                "signals_up": s["signals_cross_up"],
                "signals_down": s["signals_cross_down"],
                "resolution_closes": s["resolution_closes"],
                "resolution_settlements": s["resolution_settlements"],
                "resolution_holds": s["resolution_holds"],
                "skipped_res_entry": s["skipped_resolution_entry"],
                "cum_pnl": round(cum_pnl, 4),
                "cum_trades": cum_trades,
                "cum_wins": cum_wins,
                "cum_win_rate_pct": round(100 * cum_wins / cum_trades, 2)
                if cum_trades
                else 0.0,
            }
        )

    return pd.DataFrame(rows)


def batch_metrics(files: list[Path], cfg: StockRsiConfig) -> dict:
    """Toplu backtest ozeti (optimizasyon icin)."""
    df = run_batch(files, cfg)
    if df.empty:
        return {
            "cum_pnl": 0.0,
            "cum_trades": 0,
            "cum_wins": 0,
            "cum_win_rate_pct": 0.0,
            "profitable_files": 0,
            "n_files": len(files),
            "avg_file_pnl": 0.0,
            "resolution_closes": 0,
            "resolution_settlements": 0,
            "df": df,
        }
    last = df.iloc[-1]
    return {
        "cum_pnl": float(last["cum_pnl"]),
        "cum_trades": int(last["cum_trades"]),
        "cum_wins": int(last["cum_wins"]),
        "cum_win_rate_pct": float(last["cum_win_rate_pct"]),
        "profitable_files": int((df["file_pnl"] > 0).sum()),
        "n_files": len(df),
        "avg_file_pnl": float(df["file_pnl"].mean()),
        "resolution_closes": int(df["resolution_closes"].sum()),
        "resolution_settlements": int(df["resolution_settlements"].sum()),
        "df": df,
    }


def print_report(df: pd.DataFrame, cfg: StockRsiConfig, data_dir: Path) -> None:
    res = cfg.resolution
    print()
    print("=" * 72)
    print("Strategy v2 — Toplu backtest (Stock RSI + resolution)")
    print("=" * 72)
    print(
        f"Klasor: {data_dir}  |  Dosya: {len(df)}  |  k={cfg.size:g}  |  "
        f"RSI={cfg.rsi_period}  up={cfg.cross_up_level}  down={cfg.cross_down_level}  "
        f"resample={cfg.resample_seconds}s"
    )
    print(
        f"Resolution: son {res.force_close_minutes:g} dk  hold>={res.hold_force_close_if_mid_gte}  "
        f"dump<{res.dump_immediately_if_mid_lt}  block_giris={res.block_entries_minutes:g} dk"
    )
    print("(PnL = fiyat farki x k; son P dk tut/settlement/dump canli ile ayni)")
    print("-" * 72)

    cols = [
        "idx",
        "slug_epoch",
        "trades",
        "wins",
        "file_pnl",
        "cum_pnl",
        "resolution_closes",
        "resolution_settlements",
        "win_rate_pct",
    ]
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("-" * 72)
    if len(df):
        last = df.iloc[-1]
        print(
            f"TOPLAM  islem={int(last['cum_trades'])}  "
            f"kazanan={int(last['cum_wins'])}  "
            f"win%={last['cum_win_rate_pct']:.2f}  "
            f"kumulatif PnL={last['cum_pnl']:.4f}"
        )
        print(
            f"        resolution kapat={int(df['resolution_closes'].sum())}  "
            f"settlement={int(df['resolution_settlements'].sum())}  "
            f"dosya ort PnL={df['file_pnl'].mean():.4f}  "
            f"kazanan dosya={int((df['file_pnl'] > 0).sum())}/{len(df)}"
        )
    print("=" * 72)
    print()


def plot_cumulative(df: pd.DataFrame, out: Path, cfg: StockRsiConfig) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(12, 7), constrained_layout=True)
    x = df["idx"]
    ax = axes[0]
    ax.bar(
        x,
        df["file_pnl"],
        color=["#16a34a" if p >= 0 else "#dc2626" for p in df["file_pnl"]],
        alpha=0.75,
    )
    ax.axhline(0, color="#64748b", lw=0.8)
    ax.set_ylabel("Dosya PnL")
    ax.set_title("Piyasa basina PnL")
    ax.grid(True, alpha=0.3)

    ax2 = axes[1]
    ax2.plot(x, df["cum_pnl"], "o-", color="#0f172a", lw=2, ms=6)
    ax2.fill_between(x, 0, df["cum_pnl"], alpha=0.15, color="#6366f1")
    ax2.axhline(0, color="#64748b", lw=0.8)
    ax2.set_ylabel("Kumulatif PnL")
    res = cfg.resolution
    ax2.set_title(
        f"Kumulatif PnL  |  up={cfg.cross_up_level} down={cfg.cross_down_level}  "
        f"res P={res.force_close_minutes:g}m hold={res.hold_force_close_if_mid_gte}"
    )
    ax2.grid(True, alpha=0.3)

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Grafik: {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Strategy v2 toplu backtest (5m CSV)")
    p.add_argument("--dir", type=Path, default=Path("data/ws/5m"))
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--order", choices=("epoch", "mtime"), default="epoch")
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--cross-up", type=float, default=50.0)
    p.add_argument("--cross-down", type=float, default=50.0)
    p.add_argument("--resample", type=float, default=1.0)
    p.add_argument("-k", "--k", type=float, default=None)
    p.add_argument("--size", type=float, default=1.0, help=argparse.SUPPRESS)
    p.add_argument("--cooldown", type=float, default=0.0)
    p.add_argument("--no-wilder", action="store_true")
    p.add_argument("--no-down-when-flat", action="store_true")
    p.add_argument("--save", type=Path)
    p.add_argument("--json", type=Path)
    p.add_argument("--plot", type=Path)
    add_resolution_arguments(p)
    args = p.parse_args()

    if not args.dir.is_dir():
        raise SystemExit(f"Klasor yok: {args.dir}")

    files = list_5m_csvs(args.dir, n=args.n, order=args.order)
    print(f"Calistirilacak {len(files)} dosya ({args.order} sirasi):")
    for f in files:
        print(f"  {f.name}")

    cfg = build_stock_rsi_config(args)
    df = run_batch(files, cfg)
    print_report(df, cfg, args.dir)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.save, index=False)
        print(f"CSV: {args.save}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config": config_summary_dict(cfg),
            "data_dir": str(args.dir),
            "n_files": len(df),
            "total_cum_pnl": float(df["cum_pnl"].iloc[-1]) if len(df) else 0.0,
            "rows": df.to_dict(orient="records"),
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"JSON: {args.json}")

    if args.plot and len(df):
        plot_cumulative(df, args.plot, cfg)


if __name__ == "__main__":
    main()
