#!/bin/bash
# Stay on the runner and post about once an hour.
# GitHub may never start the next scheduled job. This one covers the gap.
set -uo pipefail

cd "$(dirname "$0")/.."

persist() {
  (
    set -euo pipefail
    python -c "from dealbot.session import database_path; from dealbot.publisher import StateStore; StateStore(str(database_path())).checkpoint()" || true
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    mkdir -p data
    cp data/state.sqlite3 "$RUNNER_TEMP/state.sqlite3"
    git fetch origin main
    git checkout -f -B main origin/main
    mkdir -p data
    cp "$RUNNER_TEMP/state.sqlite3" data/state.sqlite3
    git add -f data/state.sqlite3
    if git diff --staged --quiet; then
      echo "State unchanged."
      exit 0
    fi
    git commit -m "chore(bot): persist posted deal state [skip ci]"
    git push origin HEAD:main
  )
}

run_cycle() {
  echo "Posting cycle at $(TZ=Asia/Kolkata date '+%H:%M IST')"
  cycle_start=$(date +%s)
  if python run_once.py; then
    cycle_end=$(date +%s)
    cycle_elapsed=$((cycle_end - cycle_start))
    posted_count=$(python -c "from dealbot.publisher import StateStore; from dealbot.session import database_path; print(StateStore(str(database_path())).get_meta('last_cycle_posted_count') or 0)" 2>/dev/null || echo 0)
    echo "Cycle completed in ${cycle_elapsed}s. Posted deals: ${posted_count}."
    
    if [ "$posted_count" -gt 0 ]; then
      # Set last_cycle_at to cycle_start to eliminate progressive session clock drift
      python -c "from datetime import datetime, timezone; from dealbot.session import mark_cycle; mark_cycle(when=datetime.fromtimestamp(${cycle_start}, timezone.utc), posted_count=${posted_count})"
    else
      echo "Empty cycle (0 deals posted). Scheduling 15-minute retry."
      python -c "from datetime import datetime, timezone; from dealbot.publisher import StateStore; from dealbot.session import database_path; s = StateStore(str(database_path())); s.set_meta('last_cycle_empty_at', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')); s.checkpoint()"
    fi
  else
    echo "Cycle execution encountered an error. Waiting 15 minutes before the next try."
    python -c "from datetime import datetime, timezone; from dealbot.publisher import StateStore; from dealbot.session import database_path; s = StateStore(str(database_path())); s.set_meta('last_cycle_empty_at', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')); s.checkpoint()" || true
  fi
  export SESSION_STARTED=true
  persist || echo "State push failed. The next cycle will try again."
}

# Graceful signal handling: ensure state is persisted if runner is cancelled or stopped
cleanup() {
  echo "Caught termination signal. Checkpointing and persisting state before exit..."
  python -c "from dealbot.session import database_path; from dealbot.publisher import StateStore; StateStore(str(database_path())).checkpoint()" || true
  persist || true
  exit 0
}
trap cleanup SIGTERM SIGINT

if [ "${MANUAL:-false}" = "true" ]; then
  echo "Manual run: posting once even if the channel window is closed."
  run_cycle
fi

export SESSION_DEADLINE
# Session duration: 2h 40m (~2.6 hours) so runs finish cleanly before next cron
SESSION_DEADLINE="$(python -c 'from datetime import datetime, timedelta; from dealbot.session import IST; print((datetime.now(IST) + timedelta(hours=2, minutes=40)).isoformat())')"
echo "Runner stays up until ${SESSION_DEADLINE} or 23:20 IST, whichever is first."

while true; do
  line="$(python -m dealbot.session)"
  echo "scheduler: ${line}"
  case "$line" in
    post)
      run_cycle
      ;;
    sleep\ *)
      seconds="${line#sleep }"
      case "$seconds" in
        ''|*[!0-9]*)
          echo "Bad sleep value: ${line}"
          exit 1
          ;;
      esac
      echo "Sleeping ${seconds}s until next cycle (signal-responsive)..."
      slept=0
      while [ "$slept" -lt "$seconds" ]; do
        chunk=5
        rem=$((seconds - slept))
        if [ "$rem" -lt 5 ]; then
          chunk="$rem"
        fi
        sleep "$chunk"
        slept=$((slept + chunk))
      done
      ;;
    stop)
      echo "Channel window closed or the runner budget is over."
      break
      ;;
    *)
      echo "Scheduler returned ${line}. Stopping."
      exit 1
      ;;
  esac
done
