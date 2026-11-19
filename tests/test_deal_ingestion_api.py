import json
import sqlite3
from pathlib import Path
import pytest

from dealbot.deal_ingestion import (
    DealIngestionEngine,
    authenticate_sync_request,
    validate_deal_payload,
)


def test_authenticate_sync_request(monkeypatch):
    monkeypatch.setenv("INTERNAL_SYNC_SECRET", "super_secret_test_key_999")

    # Direct match
    assert authenticate_sync_request("super_secret_test_key_999") is True

    # Bearer header format
    assert authenticate_sync_request("Bearer super_secret_test_key_999") is True
    assert authenticate_sync_request("bearer super_secret_test_key_999") is True

    # Invalid secret
    assert authenticate_sync_request("wrong_secret") is False
    assert authenticate_sync_request("") is False
    assert authenticate_sync_request(None) is False

    # Server missing secret
    monkeypatch.delenv("INTERNAL_SYNC_SECRET", raising=False)
    assert authenticate_sync_request("super_secret_test_key_999") is False


def test_validate_deal_payload():
    # Valid payload
    valid_payload = {
        "canonical_id": "amz-B0BS1QCFHX",
        "title": "Sony WH-1000XM5 Wireless Headphones",
        "merchant": "amazon",
        "category": "electronics",
        "price": 19999.0,
        "mrp": 29999.0,
        "discount_pct": 33,
        "bank_offer": "₹2,000 instant bank discount",
        "coupon_code": "SONY2000",
        "affiliate_url": "https://www.amazon.in/dp/B0BS1QCFHX",
        "image_url": "https://m.media-amazon.com/images/I/xyz.jpg",
        "expected_payout": 300.0,
        "score": 85.0,
        "subid": "bot_tg_electronics_B0BS1QCFHX",
        "expires_at": "2026-11-20T23:59:59Z",
    }
    is_valid, msg, clean = validate_deal_payload(valid_payload)
    assert is_valid is True
    assert clean["canonical_id"] == "amz-B0BS1QCFHX"
    assert clean["price"] == 19999.0
    assert clean["mrp"] == 29999.0
    assert clean["discount_pct"] == 33
    assert clean["coupon_code"] == "SONY2000"

    # Missing canonical_id
    bad_id = dict(valid_payload)
    bad_id["canonical_id"] = ""
    is_valid, msg, _ = validate_deal_payload(bad_id)
    assert is_valid is False
    assert "canonical_id" in msg

    # Missing title
    bad_title = dict(valid_payload)
    bad_title["title"] = ""
    is_valid, msg, _ = validate_deal_payload(bad_title)
    assert is_valid is False
    assert "title" in msg

    # Invalid price
    bad_price = dict(valid_payload)
    bad_price["price"] = -50
    is_valid, msg, _ = validate_deal_payload(bad_price)
    assert is_valid is False
    assert "Price must be greater than zero" in msg

    bad_price_str = dict(valid_payload)
    bad_price_str["price"] = "not_a_number"
    is_valid, msg, _ = validate_deal_payload(bad_price_str)
    assert is_valid is False
    assert "Invalid price" in msg


def test_deal_ingestion_engine_upsert_and_web_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAZON_TAG", "dammaiprasad-21")
    monkeypatch.setenv("BHARATBAZARO_BASE_URL", "https://bharatbazaro.com")

    db_path = tmp_path / "test_state.sqlite3"
    json_path = tmp_path / "live_deals.json"

    engine = DealIngestionEngine(db_path=db_path, json_path=json_path)

    payload = {
        "canonical_id": "amz-B0BS1QCFHX",
        "title": "Sony WH-1000XM5 Headphones",
        "merchant": "amazon",
        "category": "electronics",
        "price": 24999.0,
        "mrp": 29999.0,
        "discount_pct": 17,
        "affiliate_url": "https://www.amazon.in/dp/B0BS1QCFHX?tag=old-21",
        "subid": "bot_tg_electronics_B0BS1QCFHX",
    }

    # First upsert
    res1 = engine.upsert_deal(payload)
    assert res1["status"] == "success"
    assert res1["deal_id"] == "amz-B0BS1QCFHX"
    assert res1["canonical_web_url"] == "https://bharatbazaro.com/deal/B0BS1QCFHX.html"
    assert res1["web_subid"].startswith("web_deal_electron_")
    assert res1["price_status"] == "new_deal"

    # Verify SQLite state
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT price, web_subid, canonical_web_url FROM live_deals WHERE canonical_id = ?", ("amz-B0BS1QCFHX",))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 24999.0
        assert "web_deal_" in row[1]
        assert row[2] == "https://bharatbazaro.com/deal/B0BS1QCFHX.html"

        # Verify price history
        cur_hist = conn.execute("SELECT price FROM deal_price_history WHERE canonical_id = ?", ("amz-B0BS1QCFHX",))
        hist_rows = cur_hist.fetchall()
        assert len(hist_rows) == 1
        assert hist_rows[0][0] == 24999.0

    # Verify JSON file export
    assert json_path.exists()
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["asin"] == "B0BS1QCFHX"
    assert data[0]["effective_price"] == 24999.0


def test_price_history_and_lowest_badges(tmp_path, monkeypatch):
    db_path = tmp_path / "test_hist.sqlite3"
    json_path = tmp_path / "test_hist.json"
    engine = DealIngestionEngine(db_path=db_path, json_path=json_path)

    deal = {
        "canonical_id": "amz-B0BS1QCFHX",
        "title": "Sony Headphones",
        "merchant": "amazon",
        "category": "electronics",
        "price": 25000.0,
        "mrp": 30000.0,
    }

    # 1. Day 1: Initial Price ₹25,000
    res1 = engine.upsert_deal(deal)
    assert res1["price_status"] == "new_deal"
    assert res1["all_time_low"] == 25000.0

    # 2. Day 2: Same Price ₹25,000 (Deduplication / unchanged)
    res2 = engine.upsert_deal(deal)
    assert res2["price_status"] == "price_unchanged"

    # Verify deal_price_history only recorded 1 row, not 2
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM deal_price_history WHERE canonical_id = ?", ("amz-B0BS1QCFHX",))
        assert cur.fetchone()[0] == 1

    # 3. Day 3: Price drops to ₹19,999 (New All-Time Low!)
    deal["price"] = 19999.0
    res3 = engine.upsert_deal(deal)
    assert res3["price_status"] == "price_updated"
    assert res3["lowest_badge"] == "ALL-TIME LOW"
    assert res3["all_time_low"] == 19999.0

    # 4. Day 4: Price rises to ₹22,000
    deal["price"] = 22000.0
    res4 = engine.upsert_deal(deal)
    assert res4["price_status"] == "price_updated"
    assert res4["lowest_badge"] == ""  # Higher than ATL
    assert res4["all_time_low"] == 19999.0  # ATL stays 19999
