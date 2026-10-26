"""Clock for the GitHub Actions runner.

One Actions start stays up and posts about once an hour. GitHub often
never creates a scheduled run, so the job that did start has to cover
the hours until the next start, or until the channel window closes.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW_OPEN = 8 * 60 + 40
WINDOW_CLOSE = 23 * 60 + 20
GAP = timedelta(hours=1)
META_KEY = "last_cycle_at"


def as_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def _minutes(moment: datetime) -> int:
    moment = as_ist(moment)
    return moment.hour * 60 + moment.minute


def posting_open(moment: datetime) -> bool:
    return WINDOW_OPEN <= _minutes(moment) <= WINDOW_CLOSE


def _next_open(moment: datetime) -> datetime | None:
    moment = as_ist(moment)
    if _minutes(moment) < WINDOW_OPEN:
        return moment.replace(hour=8, minute=40, second=0, microsecond=0)
    return None


def plan(now: datetime, last_cycle: datetime | None, deadline: datetime) -> tuple[str, int]:
    """Return ('post', 0), ('sleep', seconds), or ('stop', 0)."""
    now = as_ist(now)
    deadline = as_ist(deadline)
    if now >= deadline:
        return ("stop", 0)

    if not posting_open(now):
        opener = _next_open(now)
        if opener is None or opener >= deadline:
            return ("stop", 0)
        return ("sleep", max(1, int((opener - now).total_seconds())))

    if last_cycle is not None:
        ready = as_ist(last_cycle) + GAP
        if now < ready:
            if ready >= deadline or not posting_open(ready):
                return ("stop", 0)
            return ("sleep", max(1, int((ready - now).total_seconds())))

    return ("post", 0)


def database_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    raw = os.getenv("DATABASE_PATH", "data/state.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path


def load_last_cycle() -> datetime | None:
    from dealbot.publisher import StateStore

    raw = StateStore(str(database_path())).get_meta(META_KEY)
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def mark_cycle(when: datetime | None = None) -> None:
    from dealbot.publisher import StateStore

    when = as_ist(when or datetime.now(IST)).astimezone(timezone.utc)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    StateStore(str(database_path())).set_meta(META_KEY, stamp)


def main() -> int:
    raw = os.getenv("SESSION_DEADLINE", "").strip()
    if raw:
        deadline = datetime.fromisoformat(raw)
    else:
        deadline = datetime.now(IST) + timedelta(hours=5, minutes=10)
    action, wait = plan(datetime.now(IST), load_last_cycle(), deadline)
    if action == "sleep":
        print(f"sleep {wait}")
    else:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
