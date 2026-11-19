from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import pytest

from dealbot.compliance import (
    IST,
    append_compliance_disclaimer,
    export_deal_to_bharatbazaro,
    format_compliance_disclaimer,
    to_bharatbazaro_schema,
)
from dealbot.profit_ranker import (
    compute_estimated_commission,
    compute_profit_score,
    get_category_commission_rate,
    get_verdict_multiplier,
    normalize_category,
    should_publish,
)


def test_category_commission_rates():
    assert get_category_commission_rate("Fashion & Lifestyle") == 0.08
    assert get_category_commission_rate("beauty") == 0.08
    assert get_category_commission_rate("Home & Kitchen") == 0.06
    assert get_category_commission_rate("appliances") == 0.06
    assert get_category_commission_rate("toys") == 0.06
    assert get_category_commission_rate("grocery") == 0.05
    assert get_category_commission_rate("books") == 0.05
    assert get_category_commission_rate("fitness") == 0.04
    assert get_category_commission_rate("electronics") == 0.015
    assert get_category_commission_rate("mobiles") == 0.015
    assert get_category_commission_rate("unknown_cat") == 0.03


def test_compute_estimated_commission():
    # Fashion item: ₹1,500 @ 8% -> ₹120.0
    assert compute_estimated_commission(1500, "fashion") == 120.0

    # Electronics laptop: ₹40,000 @ 1.5% -> ₹600.0
    assert compute_estimated_commission(40000, "electronics") == 600.0

    # Home cookware: ₹2,000 @ 6% -> ₹120.0
    assert compute_estimated_commission(2000, "home") == 120.0

    # Zero or negative
    assert compute_estimated_commission(0, "fashion") == 0.0
    assert compute_estimated_commission(-50, "fashion") == 0.0


def test_verdict_multipliers():
    # Screamer / All-Time Low -> 1.6
    assert get_verdict_multiplier({"badge_tier": "screamer"}) == 1.6
    assert get_verdict_multiplier("All-Time Low (25% below median)") == 1.6

    # Good buy -> 1.2
    assert get_verdict_multiplier({"badge_tier": "good"}) == 1.2
    assert get_verdict_multiplier("Good Buy deal") == 1.2

    # Newly tracked -> 0.9
    assert get_verdict_multiplier({"badge_tier": "neutral"}) == 0.9

    # Wait / normal price -> 0.0 (drops to zero)
    assert get_verdict_multiplier({"badge_tier": "wait"}) == 0.0
    assert get_verdict_multiplier("Normal price — wait for drop") == 0.0

    # Fake or inflated baseline -> 0.0
    assert get_verdict_multiplier("Fake MRP detected") == 0.0

    # Default fallback
    assert get_verdict_multiplier(None) == 1.0


def test_compute_profit_score():
    # ₹2,000 Home Kitchen deal (commission = ₹120) with Screamer verdict (x1.6) -> 192.0
    score = compute_profit_score(2000, "home", {"badge_tier": "screamer"})
    assert score == 192.0

    # Same deal with Wait verdict -> 0.0
    wait_score = compute_profit_score(2000, "home", {"badge_tier": "wait"})
    assert wait_score == 0.0


def test_should_publish_gate(monkeypatch):
    monkeypatch.setenv("MIN_EST_COMMISSION_INR", "40.0")
    monkeypatch.setenv("MIN_PROFIT_SCORE", "50.0")

    # High value fashion deal: ₹1,000 @ 8% = ₹80 comm, Good Buy x1.2 = 96.0 score -> PASS
    passes, reason, metrics = should_publish(1000, "fashion", {"badge_tier": "good"})
    assert passes is True
    assert metrics["estimated_commission_inr"] == 80.0
    assert metrics["profit_score"] == 96.0

    # Low value electronics: ₹1,000 @ 1.5% = ₹15 comm (< 40 threshold) -> FAIL
    passes_low, reason_low, _ = should_publish(1000, "electronics", {"badge_tier": "screamer"})
    assert passes_low is False
    assert "below threshold" in reason_low

    # High commission but Normal price (wait tier -> multiplier 0.0 -> score 0 < 50) -> FAIL
    passes_wait, reason_wait, _ = should_publish(5000, "home", {"badge_tier": "wait"})
    assert passes_wait is False
    assert "Profit score 0.00 is below threshold" in reason_wait


def test_compliance_disclaimer():
    fixed_time = datetime(2026, 11, 19, 22, 0, tzinfo=IST)
    disclaimer = format_compliance_disclaimer(fixed_time)
    assert "Price & availability accurate as of 19 Nov, 22:00 IST" in disclaimer

    caption = "🔥 Super Deal: Boat Earbuds @ ₹999"
    full_caption = append_compliance_disclaimer(caption, fixed_time)
    assert caption in full_caption
    assert disclaimer in full_caption


def test_bharatbazaro_schema_export(tmp_path):
    deal_dict = {
        "asin": "B0BS1QCFHX",
        "title": "Sony WH-1000XM5",
        "brand": "Sony",
        "category": "electronics",
        "current_price": 19999,
        "original_price": 29999,
        "discount_percent": 33,
        "url": "https://www.amazon.in/dp/B0BS1QCFHX",
    }
    schema = to_bharatbazaro_schema(deal_dict, verdict={"badge_tier": "screamer", "verdict": "All-Time Low"})
    assert schema["asin"] == "B0BS1QCFHX"
    assert schema["effective_price"] == 19999.0
    assert schema["mrp"] == 29999.0
    assert schema["discount_pct"] == 33
    assert schema["badge_tier"] == "screamer"
    assert schema["verdict"] == "All-Time Low"

    # Export to JSON file in temporary directory
    export_dir = tmp_path / "data"
    export_deal_to_bharatbazaro(deal_dict, output_dir=export_dir)
    json_file = export_dir / "live_deals.json"
    assert json_file.exists()

    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["asin"] == "B0BS1QCFHX"
