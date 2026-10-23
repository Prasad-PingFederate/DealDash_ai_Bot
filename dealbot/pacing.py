from datetime import datetime, timezone, timedelta
import logging
import os

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_current_ist_time() -> datetime:
    return datetime.now(IST)

def is_within_active_hours(start_hour: int = 8, end_hour: int = 23) -> bool:
    """
    Check if current IST time is within channel active hours (default: 8:00 AM to 11:00 PM IST).
    During quiet hours (11 PM - 8 AM), channel posting can be paused or throttled to avoid subscriber churn.
    """
    now = get_current_ist_time()
    current_hour = now.hour
    return start_hour <= current_hour <= end_hour

def get_recommended_sleep_seconds(poll_minutes: int = 30) -> int:
    """
    Returns sleep duration in seconds, automatically extending sleep duration during midnight quiet hours.
    """
    if not is_within_active_hours():
        logger.info("Quiet hours active (11 PM - 8 AM IST). Sleeping for 60 minutes...")
        return 60 * 60
    return poll_minutes * 60
