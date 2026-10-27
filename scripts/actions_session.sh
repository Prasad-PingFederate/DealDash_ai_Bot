#!/bin/bash
# Stay on the runner and post about once an hour.
# GitHub may never start the next scheduled job. This one covers the gap.
set -uo pipefail

cd "$(dirname "$0")/.."

persist() {
  (
    set -euo pipefail
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
  if python run_once.py; then
    python -c "from dealbot.session import mark_cycle; mark_cycle()"
  else
    echo "Cycle failed. Waiting 15 minutes before the next try."
    sleep 900
    return
  fi
  persist || echo "State push failed. The next cycle will try again."
}

# Graceful signal handling: ensure state is persisted if runner is cancelled or stopped
cleanup() {
  echo "Caught termination signal. Persisting state before exit..."
  persist || true
  exit 0
}
trap cleanup SIGTERM SIGINT

if [ "${MANUAL:-false}" = "true" ]; then
  echo "Manual run: posting once even if the channel window is closed."
  run_cycle
fi

export SESSION_DEADLINE
# Session duration: 2h 40m (~2.6 hours) so runs finish cleanly before next 3-hour cron
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
      echo "Sleeping ${seconds}s"
      sleep "$seconds"
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
