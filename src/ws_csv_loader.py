"""WS kayıt CSV yükleme (backtest / viz ortak)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

NUM_COLS = [
    "yes_bid",
    "yes_ask",
    "yes_mid",
    "no_bid",
    "no_ask",
    "no_mid",
    "spread",
]


def load_ws_csv(path: Path, *, require_both_sides: bool = True) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
        df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)

    for col in NUM_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if df.empty:
        return df

    df[NUM_COLS] = df[NUM_COLS].ffill()

    if require_both_sides:
        mask = df["yes_mid"].notna() & df["no_mid"].notna()
        df = df.loc[mask].copy()

    return df
