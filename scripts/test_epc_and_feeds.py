"""Dry-run verification test script for EPC Scoring and Feed Exporter."""

import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from dealbot.profit_ranker import calculate_epc_score, get_category_commission_rate, attach_epc_to_deal
from dealbot.feed_exporter import export_feeds, dispatch_downstream_webhooks

REQUIRED_SCHEMA_KEYS = [
    "asin",
    "title",
    "category",
    "mrp",
    "live_price",
    "coupon_discount",
    "effective_price",
    "commission_rate",
    "expected_commission",
    "epc_score",
    "rating",
    "image_url",
    "base_affiliate_url",
    "video_eligible",
    "updated_at",
]

def test_epc_formula():
    print("--- 1. Testing calculate_epc_score Formula ---")
    
    # Test 1: In bracket (1449), high rating (4.4)
    # Expected comm = 1449 * 0.08 = 115.92
    # Base epc = 115.92 * 0.37 = 42.8904
    # Multipliers = * 1.25 * 1.15 = 61.65495 -> 61.65
    s1 = calculate_epc_score(1449, 0.08, 37, 4.4)
    print(f"Test 1 (price=1449, rate=0.08, disc=37%, rat=4.4): {s1}")
    assert s1 == 61.65, f"Expected 61.65, got {s1}"

    # Test 2: Above bracket (6000 -> 0.85), low rating (3.9 -> 0.80)
    # Expected comm = 6000 * 0.05 = 300
    # Base epc = 300 * 0.50 = 150
    # Multipliers = * 0.85 * 0.80 = 102.0
    s2 = calculate_epc_score(6000, 0.05, 50, 3.9)
    print(f"Test 2 (price=6000, rate=0.05, disc=50%, rat=3.9): {s2}")
    assert s2 == 102.0, f"Expected 102.0, got {s2}"

    # Test 3: Below bracket (400 -> 0.85), high rating (4.5 -> 1.15)
    # Expected comm = 400 * 0.08 = 32
    # Base epc = 32 * 0.50 = 16
    # Multipliers = * 0.85 * 1.15 = 15.64
    s3 = calculate_epc_score(400, 0.08, 50, 4.5)
    print(f"Test 3 (price=400, rate=0.08, disc=50%, rat=4.5): {s3}")
    assert s3 == 15.64, f"Expected 15.64, got {s3}"

    print("✅ All EPC formula tests passed!")

def test_feed_export():
    print("\n--- 2. Testing export_feeds() ---")
    summary = export_feeds()
    print("Export Summary:", json.dumps(summary, indent=2))

    deals_feed_file = Path(summary["deals_feed_path"])
    video_queue_file = Path(summary["video_queue_path"])

    assert deals_feed_file.exists(), f"Missing {deals_feed_file}"
    assert video_queue_file.exists(), f"Missing {video_queue_file}"

    with open(deals_feed_file, "r", encoding="utf-8") as f:
        deals = json.load(f)

    with open(video_queue_file, "r", encoding="utf-8") as f:
        video_queue = json.load(f)

    print(f"Total deals in deals_feed.json: {len(deals)}")
    print(f"Total deals in video_queue.json: {len(video_queue)}")

    assert len(deals) > 0, "deals_feed.json is empty!"
    assert 1 <= len(video_queue) <= 3, f"video_queue.json expected <= 3, got {len(video_queue)}"

    # Check sorting of deals_feed.json (epc_score descending)
    scores = [d["epc_score"] for d in deals]
    assert scores == sorted(scores, reverse=True), "deals_feed.json is not sorted by epc_score descending!"
    print(f"Highest EPC: {scores[0]}, Lowest EPC: {scores[-1]}")

    # Check schema on every deal
    print("\n--- 3. Validating Exact Schema on Every Deal ---")
    for idx, d in enumerate(deals):
        for k in REQUIRED_SCHEMA_KEYS:
            assert k in d, f"Deal index {idx} ({d.get('asin')}) missing key '{k}'!"
        assert len(d.keys()) == len(REQUIRED_SCHEMA_KEYS), f"Deal has extra or missing keys: {set(d.keys()) ^ set(REQUIRED_SCHEMA_KEYS)}"

    print("✅ All deals match the strict 15-key schema!")

    # Check video queue constraints
    print("\n--- 4. Validating video_queue.json Requirements ---")
    for d in video_queue:
        for k in REQUIRED_SCHEMA_KEYS:
            assert k in d, f"Video queue deal missing key '{k}'"
        assert d["video_eligible"] is True, f"Video deal {d['asin']} has video_eligible != True"
        assert d["effective_price"] >= 499, f"Video deal {d['asin']} price < 499: {d['effective_price']}"
        print(f"  🎬 ASIN: {d['asin']} | Price: Rs {d['effective_price']} | EPC: {d['epc_score']} | Title: {d['title'][:40]}...")

    print("✅ video_queue.json strictly verified!")

def test_webhook_dispatch_safety():
    print("\n--- 5. Testing Downstream Dispatch Safety (Missing Token) ---")
    res = dispatch_downstream_webhooks(dispatch_pat="")
    assert res.get("status") == "skipped"
    print("✅ Missing token handled safely without throwing an exception.")

if __name__ == "__main__":
    test_epc_formula()
    test_feed_export()
    test_webhook_dispatch_safety()
    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY!")
