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

  # 2) run the full pipeline (records tonight's predictions into the ledger)
  echo "[stockpicker-nightly] running pipeline ..."
  PYTHONPATH=. "$PY" -m src.stockpicker.run

  # 3) regenerate the squeeze map for the run date
  echo "[stockpicker-nightly] regenerating squeeze map ..."
  PYTHONPATH=. "$PY" -m src.stockpicker.viz || echo "  (viz non-fatal error)"

  # 4) show the measured IC so far (no-op until >=10 scored rows accrue)
  echo "[stockpicker-nightly] measured signal IC (r5):"
  PYTHONPATH=. "$PY" -m src.stockpicker.ledger ic r5 || true

  echo "[stockpicker-nightly] done $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >> "$LOG" 2>&1
