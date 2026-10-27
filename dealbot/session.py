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


def last_posted_deal_time() -> datetime | None:
    try:
        from dealbot.publisher import StateStore
        store = StateStore(str(database_path()))
        with store._connect() as conn:
            row = conn.execute("SELECT posted_at FROM posted_deals ORDER BY posted_at DESC LIMIT 1").fetchone()
            if row and row[0]:
                ts = row[0].replace(" ", "T")
                if not ts.endswith("Z") and "+" not in ts:
                    ts += "+00:00"
                return datetime.fromisoformat(ts)
    except Exception:
        pass
    return None


def load_last_cycle() -> datetime | None:
    from dealbot.publisher import StateStore

    store = StateStore(str(database_path()))
    # Only lock pacing if the last cycle actually posted deals
    last_posted_count = store.get_meta("last_cycle_posted_count")
    if last_posted_count is not None and int(last_posted_count) == 0:
        last_empty = store.get_meta("last_cycle_empty_at")
        if last_empty:
            try:
                empty_dt = datetime.fromisoformat(last_empty.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                elapsed_sec = int((now_dt - empty_dt).total_seconds())
                if elapsed_sec < 900:
                    return empty_dt - timedelta(minutes=45)
            except Exception:
                pass
        return None

    raw = store.get_meta(META_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def mark_cycle(when: datetime | None = None, posted_count: int = 1) -> None:
    """Only lock the hourly pacing timer when deals were actually posted (posted_count > 0)."""
    if posted_count <= 0:
        return
    from dealbot.publisher import StateStore

    when = as_ist(when or datetime.now(IST)).astimezone(timezone.utc)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    store = StateStore(str(database_path()))
    store.set_meta(META_KEY, stamp)
    store.set_meta("last_posted_at", stamp)
    store.checkpoint()


def main() -> int:
    raw = os.getenv("SESSION_DEADLINE", "").strip()
    if raw:
        deadline = datetime.fromisoformat(raw)
    else:
        # Session duration: 2h 40m (~2.6 hours)
        deadline = datetime.now(IST) + timedelta(hours=2, minutes=40)

    now = datetime.now(IST)
    last_cycle = load_last_cycle()

    # Ensure the first cycle in any runner runs immediately on startup unless
    # a cycle actually posted deals within the last 45 minutes in SQLite.
    is_startup = os.getenv("SESSION_STARTED", "false").lower() != "true"
    if is_startup:
        last_deal_ts = last_posted_deal_time()
        if last_deal_ts is None or (now - as_ist(last_deal_ts)) >= timedelta(minutes=45):
            last_cycle = None

    action, wait = plan(now, last_cycle, deadline)
    if action == "sleep":
        print(f"sleep {wait}")
    else:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
