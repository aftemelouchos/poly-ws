#!/usr/bin/env bash
# Polymarket Live Strategy v2 — Linux ic launcher
# Kullanim: ./scripts/run_live.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

mkdir -p logs

exec ./.venv/bin/python run_live_strategy_v2.py \
  --rsi-period 8 \
  --cross-up 45 \
  --cross-down 55 \
  -k 6 \
  --res-force 1 \
  --res-hold 0.7 \
  --res-dump 0.5
