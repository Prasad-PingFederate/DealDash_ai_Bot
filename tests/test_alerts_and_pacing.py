# tests/test_alerts_and_pacing.py
"""Comprehensive tests for:
1. User price-drop & keyword alerts (/alert, /myalerts, /delalert, instant DMs, SubIDs, deduplication)
2. Peak-hour priority queue & anti-mute rate limiting (IST shopping windows, score thresholds)
3. Admin revenue digest & leakage auditing (/admin_stats, CLI digest)
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from dealbot.alerts import (
    AlertStore,
    handle_alert_command,
    handle_delalert_command,
    handle_myalerts_command,
    match_and_dispatch_alerts,
)
from dealbot.pacing import (
    IST,
    can_post_deal_now,
    is_peak_shopping_window,
    is_within_active_hours,
)
from scripts.revenue_digest import get_revenue_digest


def test_alert_store_crud(tmp_path):
    db_file = tmp_path / "test_alerts.sqlite3"
    store = AlertStore(db_file)

    # 1. Add alert
    ok, msg, alert_id = store.add_alert(
        user_id="user_123",
        chat_id="chat_123",
        keyword="Sony XM5",
        max_price=22000.0,
    )
    assert ok is True
    assert alert_id is not None
    assert "activated" in msg

    # 2. Prevent duplicate alert
    ok2, msg2, id2 = store.add_alert(
        user_id="user_123",
        chat_id="chat_123",
        keyword="sony xm5",
        max_price=22000.0,
    )
    assert ok2 is False
    assert id2 == alert_id

    # 3. List active alerts
    alerts = store.list_alerts("user_123")
    assert len(alerts) == 1
    assert alerts[0]["keyword"] == "sony xm5"
    assert alerts[0]["max_price"] == 22000.0

    # 4. Delete alert
    ok3, msg3 = store.delete_alert("user_123", alert_id)
    assert ok3 is True
    assert "deleted" in msg3

    # List after delete
    assert len(store.list_alerts("user_123")) == 0


def test_alert_command_handlers(tmp_path):
    db_file = tmp_path / "test_cmd_alerts.sqlite3"
    store = AlertStore(db_file)

    # Test /alert valid
    resp = handle_alert_command("user_456", "chat_456", "iPhone 16 75000", store=store)
    assert "activated" in resp

    # Test /alert invalid syntax
    resp_bad = handle_alert_command("user_456", "chat_456", "invalid_only_keyword", store=store)
    assert "/alert" in resp_bad
    assert "Usage:" in resp_bad

    # Test /myalerts
    resp_list = handle_myalerts_command("user_456", store=store)
    assert "iphone 16" in resp_list.lower()
    assert "₹75,000" in resp_list

    # Test /delalert
    alerts = store.list_alerts("user_456")
    alert_id = alerts[0]["id"]
    resp_del = handle_delalert_command("user_456", str(alert_id), store=store)
    assert "deleted" in resp_del


def test_match_and_dispatch_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAZON_TAG", "dammaiprasad-21")
    db_file = tmp_path / "test_match_alerts.sqlite3"
    store = AlertStore(db_file)

    # Register alerts for two different users
    store.add_alert(user_id="user_A", chat_id="chat_A", keyword="ipad air", max_price=55000.0)
    store.add_alert(user_id="user_B", chat_id="chat_B", keyword="ipad air", max_price=45000.0)

    deal = {
        "id": "amz-B09V3HN4D3",
        "title": "Apple iPad Air (5th Gen) 64GB Space Grey",
        "current_price": 49999.0,
        "original_price": 59900.0,
        "discount_percent": 17,
        "category": "electronics",
        "url": "https://www.amazon.in/dp/B09V3HN4D3",
    }

    # Match and dispatch in dry-run mode
    dispatched = match_and_dispatch_alerts(deal, store=store, dry_run=True)

    # User A (target 55k >= 49999) should match
    # User B (target 45k < 49999) should NOT match
    assert len(dispatched) == 1
    d = dispatched[0]
    assert d["user_id"] == "user_A"
    assert d["canonical_id"] == "amz-B09V3HN4D3"
    assert d["price"] == 49999.0
    assert d["subid"].startswith("bot_alert_electron_")
    assert "ascsubtag=" in d["buy_url"]
    assert "bharatbazaro.com/deal/" in d["web_url"]

    # Delivery deduplication: running again should not dispatch second time for same price
    dispatched_again = match_and_dispatch_alerts(deal, store=store, dry_run=True)
    assert len(dispatched_again) == 0

    # Further price drop to ₹44,999: now BOTH User A and User B should receive it
    deal["current_price"] = 44999.0
    dispatched_drop = match_and_dispatch_alerts(deal, store=store, dry_run=True)
    assert len(dispatched_drop) == 2


def test_peak_shopping_windows():
    # Morning peak: 08:30 - 10:30 IST
    t_morning_peak = datetime(2026, 11, 20, 9, 30, tzinfo=IST)
    assert is_peak_shopping_window(t_morning_peak) is True

    # Afternoon peak: 12:30 - 14:30 IST
    t_afternoon_peak = datetime(2026, 11, 20, 13, 15, tzinfo=IST)
    assert is_peak_shopping_window(t_afternoon_peak) is True

    # Evening peak: 19:00 - 23:30 IST
    t_evening_peak = datetime(2026, 11, 20, 21, 0, tzinfo=IST)
    assert is_peak_shopping_window(t_evening_peak) is True

    # Off-peak hours
    t_offpeak_morning = datetime(2026, 11, 20, 11, 30, tzinfo=IST)
    assert is_peak_shopping_window(t_offpeak_morning) is False

    t_offpeak_afternoon = datetime(2026, 11, 20, 16, 0, tzinfo=IST)
    assert is_peak_shopping_window(t_offpeak_afternoon) is False

    # Midnight quiet hours
    t_quiet = datetime(2026, 11, 20, 2, 0, tzinfo=IST)
    assert is_peak_shopping_window(t_quiet) is False
    assert is_within_active_hours(t_quiet) is False


def test_can_post_deal_now_priority_queue_and_rate_limiting(tmp_path):
    empty_db = tmp_path / "empty.db"
    t_peak = datetime(2026, 11, 20, 20, 0, tzinfo=IST)
    t_offpeak = datetime(2026, 11, 20, 16, 0, tzinfo=IST)
    t_quiet = datetime(2026, 11, 20, 3, 0, tzinfo=IST)

    high_profit_deal = {"id": "d1", "is_loot": False}
    medium_profit_deal = {"id": "d2", "is_loot": False}
    loot_deal = {"id": "d3", "is_loot": True}

    # 1. High score (>= 70) posts immediately even during off-peak
    can_post, reason, _ = can_post_deal_now(high_profit_deal, profit_score=85.0, now=t_offpeak, db_path=empty_db)
    assert can_post is True
    assert "Priority Queue: Immediate post" in reason

    # 2. Medium score (40 <= score < 70) posts during peak, deferred during off-peak
    can_post_peak, reason_peak, _ = can_post_deal_now(medium_profit_deal, profit_score=55.0, now=t_peak, db_path=empty_db)
    assert can_post_peak is True
    assert "Peak Window Approved" in reason_peak

    can_post_off, reason_off, _ = can_post_deal_now(medium_profit_deal, profit_score=55.0, now=t_offpeak, db_path=empty_db)
    assert can_post_off is False
    assert "Off-Peak Queue" in reason_off

    # 3. Quiet hours deferral unless verified loot
    can_post_quiet, reason_quiet, _ = can_post_deal_now(high_profit_deal, profit_score=85.0, now=t_quiet, db_path=empty_db)
    assert can_post_quiet is False
    assert "Quiet hours active" in reason_quiet

    can_post_quiet_loot, _, _ = can_post_deal_now(loot_deal, profit_score=95.0, now=t_quiet, db_path=empty_db)
    assert can_post_quiet_loot is True


def test_revenue_digest(tmp_path):
    db_file = tmp_path / "test_revenue.sqlite3"
    import sqlite3
    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted_deals (
                id INTEGER PRIMARY KEY,
                deal_id TEXT,
                retailer TEXT,
                category TEXT,
                price REAL,
                agent_score REAL,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_alert_deliveries (
                id INTEGER PRIMARY KEY,
                user_id TEXT,
                canonical_id TEXT,
                price REAL,
                delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unmonetized_domains (
                id INTEGER PRIMARY KEY,
                domain TEXT,
                url TEXT,
                merchant TEXT,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Insert test records
        conn.execute(
            "INSERT INTO posted_deals (deal_id, retailer, category, price, agent_score) VALUES (?, ?, ?, ?, ?)",
            ("amz-1", "amazon", "electronics", 25000, 85),
        )
        conn.execute(
            "INSERT INTO posted_deals (deal_id, retailer, category, price, agent_score) VALUES (?, ?, ?, ?, ?)",
            ("fk-1", "flipkart", "fashion", 1500, 75),
        )
        conn.execute(
            "INSERT INTO user_alert_deliveries (user_id, canonical_id, price) VALUES (?, ?, ?)",
            ("u1", "amz-1", 25000),
        )
        conn.execute(
            "INSERT INTO unmonetized_domains (domain, url, merchant) VALUES (?, ?, ?)",
            ("unknownstore.in", "https://unknownstore.in/item", "unknown"),
        )

    digest = get_revenue_digest(db_path=db_file)
    assert digest["last_24h_posted"] == 2
    assert digest["last_24h_alerts"] == 1
