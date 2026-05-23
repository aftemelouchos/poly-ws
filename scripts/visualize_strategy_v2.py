#!/usr/bin/env python3
"""
Strategy v2 görselleştirme — UP fiyat, RSI, alım/satım noktaları, PnL, istatistik.

Kullanım:
  python scripts/visualize_strategy_v2.py -f data/ws/5m/....csv
  python scripts/visualize_strategy_v2.py -f ... --cross-up 30 --cross-down 70 --save charts/v2.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy_v2.backtest import Action, BacktestResultV2, prepare_bars, run_backtest
from src.strategy_v2.cli_helpers import add_resolution_arguments, build_stock_rsi_config

_UP_EXITS = (
    Action.SELL_UP.value,
    Action.RESOLUTION_SELL_UP.value,
    Action.SETTLE_UP.value,
)
_DOWN_EXITS = (
    Action.SELL_DOWN.value,
    Action.RESOLUTION_SELL_DOWN.value,
    Action.SETTLE_DOWN.value,
)

LEG_STYLE = {
    Action.BUY_UP.value: {"marker": "^", "color": "#16a34a", "s": 120, "zorder": 5, "label": "UP al"},
    Action.SELL_UP.value: {"marker": "v", "color": "#14532d", "s": 120, "zorder": 5, "label": "UP sat"},
    Action.RESOLUTION_SELL_UP.value: {"marker": "X", "color": "#dc2626", "s": 100, "zorder": 6, "label": "Res UP sat"},
    Action.SETTLE_UP.value: {"marker": "D", "color": "#ca8a04", "s": 90, "zorder": 6, "label": "Settle UP"},
    Action.BUY_DOWN.value: {"marker": "^", "color": "#2563eb", "s": 120, "zorder": 5, "label": "DOWN al"},
    Action.SELL_DOWN.value: {"marker": "v", "color": "#1e3a8a", "s": 120, "zorder": 5, "label": "DOWN sat"},
    Action.RESOLUTION_SELL_DOWN.value: {"marker": "X", "color": "#dc2626", "s": 100, "zorder": 6, "label": "Res DOWN sat"},
    Action.SETTLE_DOWN.value: {"marker": "D", "color": "#ca8a04", "s": 90, "zorder": 6, "label": "Settle DOWN"},
}


def _shade_positions(ax, legs: list, *, up_price_col: str = "yes_mid") -> None:
    """UP/DOWN tutma aralıklarını hafif arka plan ile göster."""
    side = "FLAT"
    entry_t = None
    for leg in legs:
        t = leg.time
        if leg.action == Action.BUY_UP.value:
            side = "UP"
            entry_t = t
        elif leg.action in _UP_EXITS and side == "UP" and entry_t:
            ax.axvspan(entry_t, t, alpha=0.12, color="#22c55e", zorder=0)
            side, entry_t = "FLAT", None
        elif leg.action == Action.BUY_DOWN.value:
            side = "DOWN"
            entry_t = t
        elif leg.action in _DOWN_EXITS and side == "DOWN" and entry_t:
            ax.axvspan(entry_t, t, alpha=0.12, color="#3b82f6", zorder=0)
            side, entry_t = "FLAT", None


def _plot_legs(ax, legs: list, panel: str) -> None:
    """panel: 'up' | 'down' — ilgili işlemleri çiz."""
    shown: set[str] = set()
    for leg in legs:
        up_actions = (Action.BUY_UP.value, *_UP_EXITS)
        down_actions = (Action.BUY_DOWN.value, *_DOWN_EXITS)
        if panel == "up" and leg.action not in up_actions:
            continue
        if panel == "down" and leg.action not in down_actions:
            continue
        st = LEG_STYLE.get(leg.action)
        if not st:
            continue
        lbl = st["label"] if leg.action not in shown else None
        ax.scatter(leg.time, leg.price, **{k: v for k, v in st.items() if k != "label"}, label=lbl)
        shown.add(leg.action)
        ax.annotate(
            f"{leg.price:.2f}",
            (leg.time, leg.price),
            textcoords="offset points",
            xytext=(0, 8),
            ha="center",
            fontsize=7,
            color=st["color"],
        )


def _equity_curve(result: BacktestResultV2) -> pd.DataFrame:
    rows = []
    cum = 0.0
    for r in result.round_trips:
        cum += r.pnl
        rows.append({"time": r.exit_time, "pnl": r.pnl, "cum_pnl": cum})
    return pd.DataFrame(rows)


def plot_strategy_v2(
    bars: pd.DataFrame,
    result: BacktestResultV2,
    title: str,
    save_path: Path | None = None,
    show: bool = True,
) -> None:
    cfg = result.config
    s = result.summary_dict()

    fig = plt.figure(figsize=(15, 13))
    gs = fig.add_gridspec(5, 1, height_ratios=[2.2, 2.2, 2.0, 1.6, 1.4], hspace=0.08)
    ax_up = fig.add_subplot(gs[0])
    ax_dn = fig.add_subplot(gs[1], sharex=ax_up)
    ax_rsi = fig.add_subplot(gs[2], sharex=ax_up)
    ax_pnl = fig.add_subplot(gs[3], sharex=ax_up)
    ax_tbl = fig.add_subplot(gs[4])
    ax_tbl.axis("off")

    t = bars["time"]

    # --- UP ---
    ax_up.fill_between(t, bars["yes_bid"], bars["yes_ask"], alpha=0.2, color="#86efac")
    ax_up.plot(t, bars["yes_mid"], color="#15803d", lw=1.0, alpha=0.85, label="UP mid")
    _shade_positions(ax_up, result.legs)
    _plot_legs(ax_up, result.legs, panel="up")
    ax_up.set_ylabel("UP")
    ax_up.set_ylim(0, 1)
    ax_up.legend(loc="upper left", fontsize=8, ncol=3)
    ax_up.grid(True, alpha=0.3)
    ax_up.set_title("UP — alim / satim (yes fiyat)")

    # --- DOWN ---
    ax_dn.fill_between(t, bars["no_bid"], bars["no_ask"], alpha=0.2, color="#fca5a5")
    ax_dn.plot(t, bars["no_mid"], color="#b91c1c", lw=1.0, alpha=0.85, label="DOWN mid")
    _plot_legs(ax_dn, result.legs, panel="down")
    ax_dn.set_ylabel("DOWN")
    ax_dn.set_ylim(0, 1)
    ax_dn.legend(loc="upper left", fontsize=8, ncol=2)
    ax_dn.grid(True, alpha=0.3)
    ax_dn.set_title("DOWN — alim / satim (no fiyat)")

    # --- RSI ---
    ax_rsi.plot(t, bars["rsi"], color="#7c3aed", lw=1.2, label="RSI (UP mid)")
    ax_rsi.axhline(cfg.cross_up_level, color="#16a34a", ls="--", lw=0.9, alpha=0.8)
    ax_rsi.axhline(cfg.cross_down_level, color="#dc2626", ls="--", lw=0.9, alpha=0.8)
    ax_rsi.axhline(50, color="#94a3b8", ls=":", lw=0.7, alpha=0.6)

    cu = bars.loc[bars["cross_up"].fillna(False)]
    cd = bars.loc[bars["cross_down"].fillna(False)]
    if len(cu):
        ax_rsi.scatter(
            cu["time"], cu["rsi"], marker="*", s=80, c="#16a34a", zorder=4, label="Cross UP", alpha=0.9
        )
    if len(cd):
        ax_rsi.scatter(
            cd["time"], cd["rsi"], marker="*", s=80, c="#dc2626", zorder=4, label="Cross DOWN", alpha=0.9
        )

    for leg in result.legs:
        ax_rsi.axvline(leg.time, color="#cbd5e1", lw=0.6, alpha=0.5, zorder=1)

    ax_rsi.set_ylabel("RSI")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.legend(loc="upper left", fontsize=8, ncol=3)
    ax_rsi.grid(True, alpha=0.3)
    ax_rsi.set_title(
        f"RSI period={cfg.rsi_period}  |  cross up={cfg.cross_up_level}  down={cfg.cross_down_level}"
    )

    # --- PnL ---
    eq = _equity_curve(result)
    if not eq.empty:
        colors = ["#16a34a" if p >= 0 else "#dc2626" for p in eq["pnl"]]
        w = pd.Timedelta(seconds=max(cfg.resample_seconds or 1, 1) * 0.8)
        ax_pnl.bar(eq["time"], eq["pnl"], width=w, color=colors, alpha=0.7, label="Islem PnL")
        ax_pnl.plot(eq["time"], eq["cum_pnl"], color="#0f172a", lw=1.5, marker="o", ms=4, label="Kumulatif")
    ax_pnl.axhline(0, color="#64748b", lw=0.8)
    ax_pnl.set_ylabel("PnL")
    ax_pnl.legend(loc="upper left", fontsize=8)
    ax_pnl.grid(True, alpha=0.3)
    ax_pnl.set_title(f"Toplam PnL: {s['total_pnl']:.4f}  |  Max DD: {s['max_drawdown']:.4f}")

    ax_pnl.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    plt.setp(ax_up.get_xticklabels(), visible=False)
    plt.setp(ax_dn.get_xticklabels(), visible=False)
    plt.setp(ax_rsi.get_xticklabels(), visible=False)

    # --- Stats + trade table ---
    header = (
        f"Round-trip: {s['round_trips']}   Win: {s['wins']}/{s['losses']} ({s['win_rate_pct']}%)\n"
        f"Sinyal cross up/down: {s['signals_cross_up']}/{s['signals_cross_down']}   "
        f"Legs: {s['legs']}   Cooldown skip: {s['skipped_cooldown']}\n"
        f"Resample: {cfg.resample_seconds}s   Size: {cfg.size}"
    )
    lines = ["Islem listesi (round-trip):"]
    for i, r in enumerate(result.round_trips, 1):
        sign = "+" if r.pnl >= 0 else ""
        lines.append(
            f"{i:2d}. {r.side:4s}  {r.entry_time.strftime('%H:%M:%S')}->{r.exit_time.strftime('%H:%M:%S')}  "
            f"{r.entry_price:.3f}->{r.exit_price:.3f}  {sign}{r.pnl:.4f}  ({r.hold_seconds:.0f}s)"
        )
    if not lines[1:]:
        lines.append("  (islem yok)")

    ax_tbl.text(
        0.01,
        0.98,
        header + "\n\n" + "\n".join(lines[:18]),
        transform=ax_tbl.transAxes,
        fontsize=8,
        family="monospace",
        va="top",
        ha="left",
        bbox=dict(boxstyle="round", facecolor="#f8fafc", edgecolor="#cbd5e1"),
    )
    if len(result.round_trips) > 17:
        ax_tbl.text(0.55, 0.98, "\n".join(lines[18:36]), transform=ax_tbl.transAxes, fontsize=8, family="monospace", va="top", ha="left")

    fig.suptitle(title, fontsize=13, fontweight="bold", y=0.995)

    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"Kaydedildi: {save_path}")

    if show:
        plt.show()
    else:
        plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description="Strategy v2 — grafik + islemler")
    p.add_argument("--file", "-f", type=Path, required=True)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--cross-up", type=float, default=50.0)
    p.add_argument("--cross-down", type=float, default=50.0)
    p.add_argument("--resample", type=float, default=1.0)
    p.add_argument("-k", "--k", type=float, default=None, help="Pay sayisi / islem")
    p.add_argument("--size", type=float, default=1.0, help=argparse.SUPPRESS)
    p.add_argument("--cooldown", type=float, default=0.0)
    p.add_argument("--no-wilder", action="store_true")
    p.add_argument("--no-down-when-flat", action="store_true")
    p.add_argument("--save", "-s", type=Path)
    p.add_argument("--no-show", action="store_true")
    add_resolution_arguments(p)
    args = p.parse_args()

    if not args.file.is_file():
        raise SystemExit(f"Dosya yok: {args.file}")

    cfg = build_stock_rsi_config(args)
    result = run_backtest(args.file, cfg)
    from src.ws_csv_loader import load_ws_csv

    bars = prepare_bars(load_ws_csv(args.file), cfg)
    title = f"Strategy v2 Stock RSI  |  {args.file.name}"
    show = not args.no_show and args.save is None
    plot_strategy_v2(bars, result, title=title, save_path=args.save, show=show)

    s = result.summary_dict()
    print(
        f"PnL={s['total_pnl']:.4f}  trades={s['round_trips']}  "
        f"win%={s['win_rate_pct']}  res_close={s['resolution_closes']}  "
        f"settle={s['resolution_settlements']}  "
        f"signals={s['signals_cross_up']}/{s['signals_cross_down']}"
    )


if __name__ == "__main__":
    main()
