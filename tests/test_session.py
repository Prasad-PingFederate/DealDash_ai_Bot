from datetime import datetime, timedelta

from dealbot.session import IST, plan

OPEN = datetime(2026, 10, 26, 10, 0, tzinfo=IST)
DEADLINE = datetime(2026, 10, 26, 16, 0, tzinfo=IST)


def test_posts_when_the_window_is_open_and_nothing_ran():
    assert plan(OPEN, None, DEADLINE) == ("post", 0)


def test_waits_for_pacing_cooldown_after_the_last_cycle():
    last = OPEN - timedelta(minutes=20)
    action, seconds = plan(OPEN, last, DEADLINE)
    assert action == "sleep"
    assert 24 * 60 <= seconds <= 25 * 60


def test_posts_again_once_an_hour_has_passed():
    last = OPEN - timedelta(minutes=61)
    assert plan(OPEN, last, DEADLINE) == ("post", 0)


def test_stops_after_the_channel_window():
    late = datetime(2026, 10, 26, 23, 30, tzinfo=IST)
    assert plan(late, None, late + timedelta(hours=2)) == ("stop", 0)


def test_sleeps_until_the_window_opens():
    early = datetime(2026, 10, 26, 7, 10, tzinfo=IST)
    action, seconds = plan(early, None, early + timedelta(hours=5))
    assert action == "sleep"
    assert seconds == 90 * 60


def test_stops_when_the_runner_budget_is_over():
    assert plan(OPEN, None, OPEN - timedelta(minutes=1)) == ("stop", 0)


def test_does_not_wait_past_the_window_for_the_next_post():
    last = datetime(2026, 10, 26, 22, 40, tzinfo=IST)
    now = datetime(2026, 10, 26, 22, 50, tzinfo=IST)
    deadline = datetime(2026, 10, 27, 4, 0, tzinfo=IST)
    assert plan(now, last, deadline) == ("stop", 0)


def test_naive_timestamps_are_treated_as_ist():
    naive = datetime(2026, 10, 26, 10, 0)
    assert plan(naive, None, DEADLINE) == ("post", 0)
