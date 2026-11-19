from datetime import datetime, timezone, timedelta
import pytest

from dealbot.sanitizers import (
    IST,
    clean_title,
    compute_discount_pct,
    compute_honest_verdict,
    get_active_campaign,
    is_valid_category_item,
    sanitize_and_dedupe_deals,
)


def test_clean_title():
    # Strips noise patterns like "Rs.99 @ Amazon"
    assert clean_title("[Loot] Sony WH-1000XM5 Rs. 19,999 @ Amazon") == "Sony WH-1000XM5"
    assert clean_title("[Deal][Loot] Boat Airdopes @Amazon.in") == "Boat Airdopes"
    assert clean_title("  OnePlus   Nord   CE4   ") == "OnePlus Nord CE4"
    # Unclosed bracket truncation repair
    assert clean_title("Apple iPhone 15 (Blue, 128 GB") == "Apple iPhone 15"
    # Empty / None
    assert clean_title("") == ""
    assert clean_title(None) == ""


def test_compute_discount_pct():
    assert compute_discount_pct(effective_price=800, baseline_price=1000) == 20
    assert compute_discount_pct(effective_price=1000, baseline_price=1000) == 0
    assert compute_discount_pct(effective_price=1200, baseline_price=1000) == 0
    assert compute_discount_pct(effective_price=0, baseline_price=1000) == 0
    assert compute_discount_pct(effective_price=1, baseline_price=1000) == 99


def test_compute_honest_verdict():
    # Screamer (All-Time Low): >= 14 days, is_historical_min, >= 12% drop
    screamer = compute_honest_verdict(
        effective_price=700,
        median_90d=1000,
        days_tracked=20,
        is_historical_min=True,
    )
    assert screamer["badge_tier"] == "screamer"
    assert "All-Time Low" in screamer["verdict"]
    assert screamer["drop_vs_median_pct"] == 30

    # Good buy: >= 7 days, >= 10% drop
    good = compute_honest_verdict(
        effective_price=850,
        median_90d=1000,
        days_tracked=10,
        is_historical_min=False,
    )
    assert good["badge_tier"] == "good"
    assert "Good Buy" in good["verdict"]

    # Newly tracked: < 3 days
    new = compute_honest_verdict(
        effective_price=500,
        median_90d=1000,
        days_tracked=2,
        is_historical_min=True,
    )
    assert new["badge_tier"] == "neutral"
    assert "Newly tracked" in new["verdict"]

    # Normal price: wait
    normal = compute_honest_verdict(
        effective_price=950,
        median_90d=1000,
        days_tracked=15,
        is_historical_min=False,
    )
    assert normal["badge_tier"] == "wait"
    assert "Normal price" in normal["verdict"]


def test_is_valid_category_item():
    # Mobiles blocklist
    assert is_valid_category_item("mobiles", "Bedsheet double bed") is False
    assert is_valid_category_item("mobiles", "Mountain Cycle 26T") is False
    assert is_valid_category_item("mobiles", "Apple iPhone 15 Pro Max") is True

    # Electronics blocklist
    assert is_valid_category_item("electronics", "Silk Kurta for Women") is False
    assert is_valid_category_item("electronics", "Sony Bravia 55 inch TV") is True

    # General / Unknown
    assert is_valid_category_item("fashion", "Cotton T-Shirt") is True


def test_get_active_campaign():
    # Republic day
    rep_day = datetime(2026, 1, 20, 12, 0, tzinfo=IST)
    assert get_active_campaign(rep_day)["id"] == "republic_day"
    assert get_active_campaign(rep_day)["badge"] == "REPUBLIC DAY DEAL"

    # Prime day
    prime = datetime(2026, 7, 12, 10, 0, tzinfo=IST)
    assert get_active_campaign(prime)["id"] == "prime_day"

    # Diwali
    diwali = datetime(2026, 10, 25, 18, 0, tzinfo=IST)
    assert get_active_campaign(diwali)["id"] == "diwali"

    # Evergreen outside seasons
    evergreen = datetime(2026, 11, 19, 12, 0, tzinfo=IST)
    assert get_active_campaign(evergreen)["id"] == "evergreen"
    assert get_active_campaign(evergreen)["badge"] == "VERIFIED DROP"


def test_sanitize_and_dedupe_deals():
    raw_deals = [
        {
            "asin": "b0test0001",
            "title": "[Loot] Fire-Boltt Smartwatch Rs.999 @ Amazon",
            "category": "electronics",
            "effective_price": 999,
            "mrp": 2999,
            "median_90d": 1499,
            "days_tracked": 15,
            "is_historical_min": True,
            "verified_at": "2026-11-19T10:00:00+05:30",
        },
        # Newer duplicate of B0TEST0001
        {
            "asin": "B0TEST0001",
            "title": "Fire-Boltt Smartwatch",
            "category": "electronics",
            "effective_price": 899,
            "mrp": 2999,
            "median_90d": 1499,
            "days_tracked": 16,
            "is_historical_min": True,
            "verified_at": "2026-11-19T11:00:00+05:30",
        },
        # Category leak item: curtain under mobiles -> re-routed to general
        {
            "asin": "B0CURTAIN1",
            "title": "Home Decor Polyester Curtain (Set of 2)",
            "category": "mobiles",
            "effective_price": 499,
            "mrp": 999,
            "verified_at": "2026-11-19T09:00:00+05:30",
        },
        # Invalid / missing price -> dropped
        {
            "asin": "B0NOPRICE1",
            "title": "Invalid Deal",
            "effective_price": 0,
        },
    ]

    cleaned = sanitize_and_dedupe_deals(raw_deals, now=datetime(2026, 11, 19, 12, 0, tzinfo=IST))

    # Only 2 valid deals should survive (deduped B0TEST0001 + B0CURTAIN1)
    assert len(cleaned) == 2

    # Check deduped deal took the newer verified record
    smartwatch = next(d for d in cleaned if d["asin"] == "B0TEST0001")
    assert smartwatch["effective_price"] == 899
    assert smartwatch["title"] == "Fire-Boltt Smartwatch"
    assert smartwatch["campaign_badge"] == "VERIFIED DROP"
    assert smartwatch["badge_tier"] == "screamer"

    # Check category leak item was re-routed to general
    curtain = next(d for d in cleaned if d["asin"] == "B0CURTAIN1")
    assert curtain["category"] == "general"
