#!/usr/bin/env python3
"""
Strategy v2 grid search — rsi_period x cross_up x cross_down (resolution dahil).

  python scripts/optimize_strategy_v2.py --n 15 --k 10
  python scripts/optimize_strategy_v2.py --n 15 -k 6 --rsi-min 8 --rsi-max 21 --rsi-step 2
  python scripts/optimize_strategy_v2.py --n 15 -k 6 --up-min 20 --up-max 50 --down-min 55 --down-max 80 --step 5
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import sys
import time
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.strategy_v2.cli_helpers import add_resolution_arguments, resolution_from_args
from src.strategy_v2.config import StockRsiConfig


def _load_batch_module():
    path = Path(__file__).parent / "batch_backtest_strategy_v2.py"
    spec = importlib.util.spec_from_file_location("batch_backtest_strategy_v2", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_batch = _load_batch_module()
batch_metrics = _batch.batch_metrics
list_5m_csvs = _batch.list_5m_csvs


def frange(start: float, stop: float, step: float) -> list[float]:
    vals: list[float] = []
    x = start
    while x <= stop + 1e-9:
        vals.append(round(x, 4))
        x += step
    return vals


def irange(start: int, stop: int, step: int) -> list[int]:
    return list(range(start, stop + 1, step))


def build_grid(
    rsi_periods: list[int],
    up_min: float,
    up_max: float,
    down_min: float,
    down_max: float,
    step: float,
    min_gap: float,
) -> list[tuple[int, float, float]]:
    ups = frange(up_min, up_max, step)
    downs = frange(down_min, down_max, step)
    combos: list[tuple[int, float, float]] = []
    for rsi_p, cu, cd in itertools.product(rsi_periods, ups, downs):
        if cu < cd and (cd - cu) >= min_gap:
            combos.append((rsi_p, cu, cd))
    return combos


def evaluate_combo(
    files: list[Path],
    rsi_period: int,
    cross_up: float,
    cross_down: float,
    base: dict,
) -> dict:
    cfg = StockRsiConfig(
        rsi_period=rsi_period,
        cross_up_level=cross_up,
        cross_down_level=cross_down,
        resample_seconds=base["resample_seconds"],
        size=base["size"],
        use_wilder=base["use_wilder"],
        cooldown_seconds=base["cooldown_seconds"],
        enter_down_when_flat_on_cross_down=base["enter_down_when_flat"],
        resolution=base["resolution"],
    )
    m = batch_metrics(files, cfg)
    return {
        "rsi_period": rsi_period,
        "cross_up": cross_up,
        "cross_down": cross_down,
        "cum_pnl": m["cum_pnl"],
        "cum_trades": m["cum_trades"],
        "cum_wins": m["cum_wins"],
        "cum_win_rate_pct": m["cum_win_rate_pct"],
        "profitable_files": m["profitable_files"],
        "n_files": m["n_files"],
        "avg_file_pnl": m["avg_file_pnl"],
        "resolution_closes": m.get("resolution_closes", 0),
        "resolution_settlements": m.get("resolution_settlements", 0),
    }


def print_top(results: pd.DataFrame, top: int, baseline: dict | None, res_label: str) -> None:
    print()
    print("=" * 80)
    print("Strategy v2 — RSI period + cross optimizasyonu")
    print("=" * 80)
    print(f"Resolution: {res_label}")
    if baseline:
        print(
            f"Baseline rsi={baseline['rsi_period']} up={baseline['cross_up']} "
            f"down={baseline['cross_down']} -> cum_pnl={baseline['cum_pnl']:.4f}"
        )
    best = results.iloc[0]
    print(
        f"EN IYI  rsi={int(best['rsi_period'])}  cross_up={best['cross_up']:.0f}  "
        f"cross_down={best['cross_down']:.0f}  cum_pnl={best['cum_pnl']:.4f}  "
        f"trades={int(best['cum_trades'])}  win%={best['cum_win_rate_pct']:.1f}  "
        f"kazanan dosya={int(best['profitable_files'])}/{int(best['n_files'])}"
    )
    print("-" * 80)
    cols = [
        "rsi_period",
        "cross_up",
        "cross_down",
        "cum_pnl",
        "cum_trades",
        "cum_win_rate_pct",
        "profitable_files",
        "avg_file_pnl",
    ]
    print(results[cols].head(top).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("=" * 80)
    print()


def plot_heatmap(results: pd.DataFrame, out: Path, rsi_period: int | None = None) -> None:
    import matplotlib.pyplot as plt

    df = results
    if rsi_period is not None:
        df = df[df["rsi_period"] == rsi_period]
    if df.empty:
        print("Heatmap atlandi: veri yok")
        return

    pivot = df.pivot_table(
        index="cross_up", columns="cross_down", values="cum_pnl", aggfunc="max"
    )
    fig, ax = plt.subplots(figsize=(10, 7))
    im = ax.imshow(pivot.values, aspect="auto", origin="lower", cmap="RdYlGn")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([f"{c:.0f}" for c in pivot.columns])
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([f"{r:.0f}" for r in pivot.index])
    ax.set_xlabel("cross_down")
    ax.set_ylabel("cross_up")
    title = f"PnL heatmap (rsi={rsi_period})" if rsi_period else "PnL heatmap (en iyi rsi dilimi)"
    ax.set_title(title)
    plt.colorbar(im, ax=ax, label="cum_pnl")
    best = df.iloc[0]
    try:
        j = list(pivot.columns).index(best["cross_down"])
        i = list(pivot.index).index(best["cross_up"])
        ax.scatter(j, i, s=120, c="blue", marker="*", edgecolors="white")
    except ValueError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Heatmap: {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Strategy v2 — RSI period + cross optimizasyonu")
    p.add_argument("--dir", type=Path, default=Path("data/ws/5m"))
    p.add_argument("--n", type=int, default=15)
    p.add_argument("-k", "--k", type=float, default=10.0)
    p.add_argument("--resample", type=float, default=1.0)
    p.add_argument("--cooldown", type=float, default=0.0)
    p.add_argument("--no-wilder", action="store_true")
    p.add_argument("--no-down-when-flat", action="store_true")
    g = p.add_argument_group("RSI period")
    g.add_argument("--rsi-min", type=int, default=8)
    g.add_argument("--rsi-max", type=int, default=21)
    g.add_argument("--rsi-step", type=int, default=2)
    g = p.add_argument_group("Cross seviyeleri")
    g.add_argument("--up-min", type=float, default=15.0)
    g.add_argument("--up-max", type=float, default=50.0)
    g.add_argument("--down-min", type=float, default=50.0)
    g.add_argument("--down-max", type=float, default=85.0)
    g.add_argument("--step", type=float, default=5.0)
    g.add_argument("--min-gap", type=float, default=5.0)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--save", type=Path)
    p.add_argument("--heatmap", type=Path)
    p.add_argument("--baseline-rsi", type=int, default=None)
    p.add_argument("--baseline-up", type=float, default=None)
    p.add_argument("--baseline-down", type=float, default=None)
    add_resolution_arguments(p)
    args = p.parse_args()

    files = list_5m_csvs(args.dir, n=args.n, order="epoch")
    rsi_periods = irange(args.rsi_min, args.rsi_max, args.rsi_step)
    combos = build_grid(
        rsi_periods,
        args.up_min,
        args.up_max,
        args.down_min,
        args.down_max,
        args.step,
        args.min_gap,
    )
    res = resolution_from_args(args)
    res_label = (
        f"son {res.force_close_minutes:g} dk  hold>={res.hold_force_close_if_mid_gte}  "
        f"dump<{res.dump_immediately_if_mid_lt}"
    )
    print(
        f"Dosya: {len(files)}  |  k={args.k:g}  |  "
        f"Grid: {len(combos)} (rsi x cross)  |  rsi={rsi_periods}  |  {res_label}"
    )

    base = {
        "resample_seconds": None if args.resample <= 0 else args.resample,
        "size": args.k,
        "use_wilder": not args.no_wilder,
        "cooldown_seconds": args.cooldown,
        "enter_down_when_flat": not args.no_down_when_flat,
        "resolution": res,
    }

    rows: list[dict] = []
    t0 = time.perf_counter()
    for i, (rsi_p, cu, cd) in enumerate(combos, 1):
        rows.append(evaluate_combo(files, rsi_p, cu, cd, base))
        if i % 25 == 0 or i == len(combos):
            r = rows[-1]
            print(
                f"  [{i}/{len(combos)}] rsi={rsi_p} up={cu} down={cd} "
                f"pnl={r['cum_pnl']:.4f}"
            )

    elapsed = time.perf_counter() - t0
    results = pd.DataFrame(rows).sort_values("cum_pnl", ascending=False).reset_index(drop=True)

    baseline = None
    if (
        args.baseline_rsi is not None
        and args.baseline_up is not None
        and args.baseline_down is not None
    ):
        baseline = evaluate_combo(
            files, args.baseline_rsi, args.baseline_up, args.baseline_down, base
        )

    print_top(results, args.top, baseline, res_label)
    print(f"Sure: {elapsed:.1f}s  |  Kombinasyon: {len(combos)}")

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        results.to_csv(args.save, index=False)
        print(f"Kaydedildi: {args.save}")

    if args.heatmap and len(results):
        best_rsi = int(results.iloc[0]["rsi_period"])
        plot_heatmap(results, args.heatmap, rsi_period=best_rsi)

    b = results.iloc[0]
    print(
        "Onerilen batch komutu:\n"
        f"  python scripts/batch_backtest_strategy_v2.py --n {args.n} --k {args.k:g} "
        f"--rsi-period {int(b['rsi_period'])} "
        f"--cross-up {b['cross_up']:.0f} --cross-down {b['cross_down']:.0f} "
        f"--res-force {res.force_close_minutes:g} --res-hold {res.hold_force_close_if_mid_gte} "
        f"--res-dump {res.dump_immediately_if_mid_lt}"
    )


if __name__ == "__main__":
    main()
