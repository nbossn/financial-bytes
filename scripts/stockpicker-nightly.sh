#!/usr/bin/env bash
# stockpicker-nightly.sh — run the stock-picker pipeline + score the ledger.
# Invoked manually and by 3 dated cron lines (nights of Jun 29 / 30 / Jul 1 2026).
# Self-terminating: the cron lines target specific dates, so no recurrence lingers.
set -euo pipefail

REPO="/home/nboss/financial-bytes"
PY="$REPO/.venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/stockpicker-nightly.log"

mkdir -p "$LOG_DIR"
cd "$REPO"

{
  echo "════════════════════════════════════════════════════════════"
  echo "[stockpicker-nightly] start $(date '+%Y-%m-%d %H:%M:%S %Z')"

  # 1) score any prior predictions that are now old enough to resolve
  echo "[stockpicker-nightly] scoring ledger (realized returns) ..."
  PYTHONPATH=. "$PY" -m src.stockpicker.ledger score || echo "  (ledger score non-fatal error)"

  # 2) recompute accuracy + running weights from realized outcomes, write scorecard.
  #    Runs BEFORE the pipeline so the run picks up the freshly updated weights.
  echo "[stockpicker-nightly] updating accuracy scorecard + running weights ..."
  PYTHONPATH=. "$PY" -m src.stockpicker.accuracy || echo "  (accuracy non-fatal error)"

  # 3) run the full pipeline (uses running_weights.json; records tonight's predictions)
  echo "[stockpicker-nightly] running pipeline ..."
  PYTHONPATH=. "$PY" -m src.stockpicker.run

  # 4) regenerate the squeeze map for the run date
  echo "[stockpicker-nightly] regenerating squeeze map ..."
  PYTHONPATH=. "$PY" -m src.stockpicker.viz || echo "  (viz non-fatal error)"

  echo "[stockpicker-nightly] done $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >> "$LOG" 2>&1
