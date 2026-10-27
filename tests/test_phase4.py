import pytest
from unittest.mock import patch, MagicMock
from dealbot.agent import get_brand_tier, score_deal
from dealbot.models import Deal
from dealbot.publisher import StateStore
from dealbot.main import sweep_expired_deals
from dealbot.sources import _prices_from_text, fetch_reddit_deals


# ── 1. PREVENT "FOR APPLE IPHONE" ACCESSORY BRAND-SPOOFING ───────────────────

def test_accessory_brand_spoofing_not_tier1():
    # Preposition "for Apple iPhone" must NOT be granted Tier-1 authority
    assert get_brand_tier("Silicone Case for Apple iPhone 15") == 0
    assert get_brand_tier("Tempered Glass Screen Protector for Apple iPhone 16 Pro") == 0
    assert get_brand_tier("Fast Charging Cable Compatible with Samsung Galaxy S24") == 0
    assert get_brand_tier("Silicone Strap Compatible with Apple Watch Ultra") == 0
    assert get_brand_tier("Replacement Ear Tips for boAt Airdopes") == 0

    # Genuine branded products starting with brand MUST be recognized
    assert get_brand_tier("Apple iPhone 15 (128 GB) - Black") == 1
    assert get_brand_tier("Samsung Galaxy S24 5G AI Smartphone") == 1
    assert get_brand_tier("Sony WH-1000XM5 Wireless Headphones") == 1
    assert get_brand_tier("boAt Rockerz 450 Bluetooth On-Ear Headphones") == 2


def test_unrated_generic_accessory_does_not_trigger_loot():
    # Case for iPhone with steep discount but unrated -> NO loot alert
    fake_case = Deal(
        id="fake-case-1",
        retailer="Amazon",
        title="Silicone Case for Apple iPhone 15",
        url="https://www.amazon.in/dp/B0FAKECASE",
        current_price="₹149",
        original_price="₹999",
        discount_percent=85,
    )
    scored = score_deal(fake_case, recent_categories=[])
    assert fake_case.is_loot is False
    assert "🚨 LOOT ALERT 🔥" not in scored.reason
    assert "Tier-1 brand" not in scored.reason
    assert "fake-mrp generic junk penalty" in scored.reason


# ── 2. COUPON VS OOS PRICE-SPIKE FALSE POSITIVES ─────────────────────────────

def test_coupon_deal_does_not_trigger_price_spike_on_sweep(tmp_path):
    db_file = tmp_path / "test_coupon_spike.sqlite3"
    store = StateStore(str(db_file))

    deal_id = "amz-coupon-deal"
    # Posted price is effective_price = 1599 (list price was 1999 with 400 coupon)
    store.mark_posted(
        deal_id=deal_id,
        retailer="Amazon",
        canonical_product_id="amazon:B0COUPONSPIKE",
        price=1599,
        chat_id="-1001",
        message_id=50,
        caption="Deal caption",
    )

    # 1 hour later, live page still has ₹1,999 with ₹400 coupon (effective price = 1599)
    mock_enrichment = {
        "stock_status": "in_stock",
        "live_price": 1999,
        "coupon_discount": 400,
        "effective_price": 1599,
    }

    with patch("dealbot.main.inspect_stock", return_value="in_stock"), \
         patch("dealbot.main.enrich_amazon_deal", return_value=mock_enrichment), \
         patch("dealbot.main.mark_telegram_deal_price_spike") as mock_spike_edit:

        # Run sweep twice to test consecutive spike checks
        sweep_expired_deals(store, dry_run=False)
        sweep_expired_deals(store, dry_run=False)

        # Telegram should NOT have been edited for price spike
        mock_spike_edit.assert_not_called()

        # Deal should still be active
        active = store.get_recent_active_deals(limit=5)
        assert any(d["deal_id"] == deal_id for d in active)


# ── 3. PRICE SIGHTINGS ORDER-OF-UPDATE & POLLING DILUTION ─────────────────────

def test_price_sightings_order_of_update_and_polling_dilution(tmp_path):
    db_file = tmp_path / "test_dilution.sqlite3"
    store = StateStore(str(db_file))
    canon_id = "amazon:B0DILUTION"

    # Sighting 1 at ₹1,000
    ctx1 = store.record_price_sighting(canon_id, 1000)
    assert ctx1["sighting_count"] == 0  # Prior count was 0

    # Poll 4 times within same cycle at the exact same price (₹1,000)
    for _ in range(4):
        store.record_price_sighting(canon_id, 1000)

    # Dilution check: sighting_count must NOT have incremented to 5!
    ctx_check = store.get_price_context(canon_id, 1000)
    assert ctx_check["sighting_count"] == 1  # Still 1 because price did not change and < 12h
    assert ctx_check["status"] != "stagnant"  # Must NOT be marked stagnant!

    # Sighting with new price (price changed >= 1%): should increment count
    # Let's add 2 more sightings with price changes to reach count=3
    store.record_price_sighting(canon_id, 980)  # Count -> 2, min=980
    store.record_price_sighting(canon_id, 960)  # Count -> 3, min=960

    # Now test All-Time Low: current_price 800 < min 960 with count >= 3
    # Context BEFORE upsert must return all_time_low
    ctx_atl = store.record_price_sighting(canon_id, 800)
    assert ctx_atl["status"] == "all_time_low"
    assert ctx_atl["score_adj"] == 10


# ── 4. REDDIT PRICE EXTRACTION AGAINST "₹X OFF" DISCOUNTS ────────────────────

def test_reddit_price_extraction_ignores_discount_amounts():
    # Title: "[Loot] Get ₹2,000 Off on Sony Headphones @ ₹14,999"
    current, original, pct = _prices_from_text("[Loot] Get ₹2,000 Off on Sony Headphones @ ₹14,999")
    assert current == 14999

    # Title: "Save ₹500 on Philips Trimmer - Deal at ₹1,299 (MRP ₹2,499)"
    curr2, orig2, pct2 = _prices_from_text("Save ₹500 on Philips Trimmer - Deal at ₹1,299 (MRP ₹2,499)")
    assert curr2 == 1299
    assert orig2 == 2499

    # Title: "boAt Airdopes flat ₹300 coupon discount at ₹999"
    curr3, orig3, pct3 = _prices_from_text("boAt Airdopes flat ₹300 coupon discount at ₹999")
    assert curr3 == 999
