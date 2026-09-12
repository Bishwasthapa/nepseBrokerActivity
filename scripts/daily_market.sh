#!/usr/bin/env bash
# Daily NEPSE post-market runbook.
# Runs weekdays ~15:30 NPT (after market close):
#   fetch --today  -> download + ingest today's floorsheet
#   run            -> preview Track A / Track B signals (no persistence)
#   momentum       -> capital rotation vs. baseline windows
#   wash           -> internal broker matching / cross trades
# Output is appended to logs/daily_market.log.
set -euo pipefail

cd "$(dirname "$0")/.." # project root
LOG="logs/daily_market.log"
mkdir -p logs

run() {
  echo
  echo "==== $(date '+%F %T %Z')  ==>  $*" | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
}

run docker compose run --rm app python -m src.cli fetch --today
run docker compose run --rm app python -m src.cli run --top 20 --no-persist
run docker compose run --rm app python -m src.cli momentum
run docker compose run --rm app python -m src.cli wash

echo
echo "Daily run complete at $(date '+%F %T %Z')" | tee -a "$LOG"