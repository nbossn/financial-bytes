#!/usr/bin/env bash
# scheduler-watchdog.sh — is the scheduler up, and is it running current code?
#
# Replaces the inline crontab watchdog, which could never fire:
#
#     50 5 * * * pgrep -f 'financial-bytes schedule' || (cd ... && nohup ... &)
#
# cron runs that through `sh -c`, so the shell's own argv contains the pattern
# and `pgrep -f` matches itself — verified with a pattern matching no real
# process, which still exits 0. It has never once reported the scheduler down.
#
# It also asks the wrong question. On 2026-06-25..07-14 the scheduler was UP
# every day and delivered no newsletter on any of them, because the process had
# imported src/scheduler.py on 06-23 and Python imports a module once. `pgrep`
# would have said "fine" on all 20 days. Liveness is not freshness.
#
# Install (one line, replaces the old 5:50 AM entry):
#     50 5 * * * /home/nboss/financial-bytes/scripts/scheduler-watchdog.sh --restart \
#       >> /home/nboss/financial-bytes/logs/watchdog.log 2>&1
#
# Exit codes: 0 = up and current, 1 = down, 2 = up but serving stale code,
#             3 = up, something was written since launch, but no content
#                 baseline from before that launch exists so it cannot be
#                 called either way. 3 is not folded into 0 or 2 on purpose:
#                 the first live run of the old mtime-only check reported STALE
#                 for a scheduler running byte-identical code, and a guard whose
#                 first output is a false alarm is a guard that gets ignored —
#                 which is how the 20-day blackout stayed invisible.
#                 Each run records a snapshot, so 3 resolves itself after the
#                 next restart without anyone doing anything.
set -uo pipefail

REPO="/home/nboss/financial-bytes"
PY="$REPO/.venv/bin/python"
LOG="$REPO/logs/scheduler.log"
RESTART=0
[ "${1:-}" = "--restart" ] && RESTART=1

cd "$REPO" || exit 1

# The pattern lives in the module, never in this script's argv — so nothing
# here can match itself the way the old crontab line did.
"$PY" -m src.ops.process_freshness
status=$?

if [ "$RESTART" -eq 0 ]; then
  exit $status
fi

case "$status" in
  0) ;;   # up and current — nothing to do
  1)
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] watchdog: scheduler DOWN — starting it"
    nohup "$REPO/.venv/bin/financial-bytes" schedule >> "$LOG" 2>&1 &
    ;;
  2)
    # Deliberately NOT an automatic restart. A restart here would kill a
    # pipeline mid-run, and "the tree changed" is not by itself an emergency.
    # Report it loudly instead; the point is that this state was previously
    # invisible for 20 consecutive days.
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] watchdog: scheduler is UP but serving" \
         "stale code. Restart it to pick up committed fixes:" \
         "pkill -f 'financial-bytes schedule' && nohup $REPO/.venv/bin/financial-bytes" \
         "schedule >> $LOG 2>&1 &"
    ;;
  3)
    # No restart and no alarm. This is the honest "cannot tell" state; the
    # detail line already names which files moved.
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] watchdog: freshness UNKNOWN —" \
         "no content baseline predates this process. A snapshot was recorded;" \
         "this resolves to a definite answer after the next restart."
    ;;
esac

exit $status
