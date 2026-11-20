# dealbot/pacing.py
"""Peak-Hour Priority Queue & Anti-Mute Rate Limiting Engine.

Handles:
1. IST Peak Shopping Windows:
   - Morning Peak:   08:30 – 10:30 IST
   - Afternoon Peak: 12:30 – 14:30 IST
   - Evening Peak:   19:00 – 23:30 IST
2. Expected Profit Priority Queue:
   - Top Expected-Profit deals (score >= 70 or is_loot=True or Screamer) post immediately.
   - Medium deals (score between 40 and 70) are deferred to nearest peak window.
3. Anti-Mute Rate Limiting:
   - Caps posts per hour (default: 3 / MAX_POSTS_PER_HOUR) so subscribers never mute the channel.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Peak Shopping Windows (minutes from midnight in IST)
PEAK_WINDOWS = [
    (8 * 60 + 30, 10 * 60 + 30),   # 08:30 - 10:30 IST
    (12 * 60 + 30, 14 * 60 + 30),  # 12:30 - 14:30 IST
    (19 * 60, 23 * 60 + 30),       # 19:00 - 23:30 IST
]

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "state.sqlite3"


def get_current_ist_time(now: Optional[datetime] = None) -> datetime:
    """Return current or provided datetime normalized to IST."""
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def is_within_active_hours(
    now: Optional[datetime] = None,
    start_hour: int = 8,
    end_hour: int = 23,
) -> bool:
    """Check if current IST time is within channel active hours."""
    current = get_current_ist_time(now)
    mins = current.hour * 60 + current.minute
    end_mins = 24 * 60 if end_hour >= 23 else (end_hour * 60 + 30)
    return (start_hour * 60) <= mins <= end_mins


def is_peak_shopping_window(now: Optional[datetime] = None) -> bool:
    """Check if given IST time falls inside peak Indian online shopping hours."""
    current = get_current_ist_time(now)
    mins = current.hour * 60 + current.minute
    for start_m, end_m in PEAK_WINDOWS:
        if start_m <= mins <= end_m:
            return True
    return False


def get_recent_posts_count(hours: float = 1.0, db_path: Optional[Union[str, Path]] = None) -> int:
    """Count how many deals were posted to the channel in the trailing `hours`."""
    path = Path(db_path or os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))
    if not path.exists():
        return 0

    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*) FROM posted_deals
                WHERE posted_at >= datetime('now', ?)
                """,
                (f"-{hours * 60} minutes",),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("Could not count recent posts: %s", e)
        return 0


def can_post_deal_now(
    deal_or_dict: Any,
    profit_score: float,
    max_per_hour: Optional[int] = None,
    now: Optional[datetime] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Determine whether deal should post now or wait for peak/rate-limit cooldown.

    Rules:
    1. Active Hours: Block during midnight quiet hours (23:30 - 08:00 IST).
    2. Anti-Mute Rate Limit: Max posts per hour cap (default 3), bypassed only for Screamer/Loot deals.
    3. Peak Priority:
       - High Expected-Profit (score >= 70 or is_loot) posts immediately.
       - Medium Expected-Profit (40 <= score < 70) posts during peak hours, deferred during off-peak.
       - Low score (< 40) rejected.
    """
    current_ist = get_current_ist_time(now)
    limit = max_per_hour if max_per_hour is not None else int(os.getenv("MAX_POSTS_PER_HOUR", "3"))

    raw = {}
    if hasattr(deal_or_dict, "__dict__"):
        raw = {k: v for k, v in deal_or_dict.__dict__.items() if not k.startswith("_")}
    elif isinstance(deal_or_dict, dict):
        raw = dict(deal_or_dict)

    is_loot = bool(raw.get("is_loot", False))
    badge = str(raw.get("badge") or raw.get("badge_tier") or "").upper()
    is_urgent = is_loot or "SCREAMER" in badge or "ALL-TIME LOW" in badge

    recent_count = get_recent_posts_count(hours=1.0, db_path=db_path)
    in_peak = is_peak_shopping_window(current_ist)
    in_active = is_within_active_hours(current_ist)

    metadata = {
        "profit_score": profit_score,
        "is_loot": is_loot,
        "is_urgent": is_urgent,
        "recent_posts_last_hour": recent_count,
        "max_posts_per_hour": limit,
        "is_peak_window": in_peak,
        "is_active_hours": in_active,
        "ist_time": current_ist.strftime("%H:%M IST"),
    }

    # 1. Quiet hours check
    if not in_active and not is_loot:
        return False, f"Quiet hours active ({current_ist.strftime('%H:%M IST')}) - postponed to 08:30 IST", metadata

    # 2. Anti-Mute Rate Limiter
    if recent_count >= limit and not is_urgent:
        return (
            False,
            f"Anti-Mute Limiter: Already posted {recent_count} deals in last hour (max {limit})",
            metadata,
        )

    # 3. Peak-Hour Priority Evaluation
    # High score (>= 70) or urgent deals post immediately
    if profit_score >= 70.0 or is_urgent:
        return True, f"Priority Queue: Immediate post (High Score {profit_score:.1f} / Urgent)", metadata

    # Medium score (40 to 70): only post during peak shopping windows
    if in_peak:
        return True, f"Peak Window Approved: Score {profit_score:.1f} within shopping peak", metadata

    return (
        False,
        f"Off-Peak Queue: Medium score {profit_score:.1f} deferred to next IST peak shopping window",
        metadata,
    )


def get_recommended_sleep_seconds(poll_minutes: int = 30) -> int:
    """Return adaptive sleep duration, pausing longer during midnight quiet hours."""
    if not is_within_active_hours():
        logger.info("Quiet hours active (11:30 PM - 8 AM IST). Sleeping for 60 minutes...")
        return 60 * 60
    return poll_minutes * 60
