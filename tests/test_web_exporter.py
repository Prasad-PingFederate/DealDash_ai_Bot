import json
import sqlite3
from pathlib import Path
import pytest

from dealbot.models import Deal
from dealbot.publisher import StateStore
from dealbot.web_exporter import (
    detect_brand,
    extract_asin,
    map_ui_category,
    deal_to_web_json,
    export_web_feed,
)


def test_extract_asin_and_detect_brand():
    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        current_price="₹24,990",
    )
    assert extract_asin(deal) == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/dp/B09V3HM4XY") == "B09V3HM4XY"
    assert detect_brand(deal.title) == "Sony"
    assert detect_brand("Apple iPad 10th Gen") == "Apple"


def test_map_ui_category():
    assert map_ui_category("electronics", "Samsung Galaxy S24 Ultra") == "Mobiles & Accessories"
    assert map_ui_category("electronics", "Apple MacBook Air M2 16GB") == "Laptops & Computing"
    assert map_ui_category("electronics", "Sony WH-CH520 Wireless Bluetooth Headphones") == "Audio & Headphones"
    assert map_ui_category("home", "Prestige Deluxe Alpha Pressure Cooker 3L") == "Home & Kitchen"
    assert map_ui_category("fashion", "Puma Men Nitro Running Shoes") == "Fashion & Footwear"
    assert map_ui_category("grocery", "Tata Tea Gold 1kg") == "Personal Care & Grocery"


def test_deal_to_web_json():
    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        original_price="₹34,990",
        current_price="₹24,990",
        discount_percent=29,
        effective_price=22990,
        coupon_discount=2000,
        promo_code="SONY2K",
        bank_offer="10% Instant Discount on HDFC Credit Cards",
        rating=4.6,
        review_count=5200,
    )
    sightings = {"min_price": 22990, "avg_price": 27490, "max_price": 34990}
    w_json = deal_to_web_json(deal, score=92, price_sightings=sightings)

    assert w_json["id"] == "amazon:B0BS1QCFHX"
    assert w_json["asin"] == "B0BS1QCFHX"
    assert w_json["brand"] == "Sony"
    assert w_json["brand_tier"] == "tier1"
    assert w_json["effective_price"] == 22990
    assert w_json["mrp"] == 34990
    assert w_json["coupon_discount"] == 2000
    assert w_json["coupon_text"] == "Apply ₹2,000 on-page coupon"
    assert w_json["promo_code"] == "SONY2K"
    assert w_json["savings_inr"] == 12000
    assert w_json["rating"] == 4.6
    assert w_json["review_count"] == 5200
    assert w_json["score"] == 92
    assert w_json["historical_min"] == 22990
    assert w_json["historical_avg"] == 27490
    assert w_json["is_loot"] is True
    assert "https://www.amazon.in/dp/B0BS1QCFHX?tag=dammaiprasad-21&linkCode=ll1" in w_json["affiliate_url"]


def test_export_web_feed(tmp_path):
    db_file = tmp_path / "test_store.db"
    store = StateStore(db_file)

    # Insert 3 deals
    for i in range(1, 4):
        store.mark_posted(
            deal_id=f"amz-B00000000{i}",
            retailer="Amazon",
            chat_id="-1001",
            message_id=200 + i,
            caption=f"🔥 <b>LOOT</b> · Amazon\n<b>Sony Product {i} Audio Gadget</b>\nList Price: ₹1,499 (Apply ₹200 on-page coupon)\n<code>SAVE200</code>",
            price=1499,
            discount=40,
            agent_score=85,
            canonical_product_id=f"amazon:B00000000{i}",
        )

    out_dir = tmp_path / "web_data"
    res = export_web_feed(store, output_dir=out_dir)

    assert res["deals_count"] == 3
    assert (out_dir / "live_deals.json").exists()
    assert (out_dir / "stats.json").exists()

    with open(out_dir / "live_deals.json", "r", encoding="utf-8") as f:
        live_deals = json.load(f)
    assert len(live_deals) == 3
    assert live_deals[0]["asin"] in {"B000000001", "B000000002", "B000000003"}
    assert live_deals[0]["brand"] == "Sony"

    with open(out_dir / "stats.json", "r", encoding="utf-8") as f:
        stats = json.load(f)
    assert stats["total_live_deals"] == 3
    assert stats["max_discount_pct"] >= 40

