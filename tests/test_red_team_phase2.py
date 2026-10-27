import re
from unittest.mock import MagicMock, patch
import pytest

from dealbot.models import Deal
from dealbot.publisher import StateStore, deal_badge, format_caption, _ai_copy_keeps_price
from dealbot.expiry_checker import (
    enrich_amazon_deal,
    mark_telegram_deal_price_spike,
)
from dealbot.agent import (
    DealAgent,
    score_deal,
    get_brand_tier,
    extract_narrow_type,
)
from dealbot.ai_copywriter import validate_critic


# ── 1. SQLITE WAL CHECKPOINTING & STATE PERSISTENCE ──────────────────────────

def test_sqlite_wal_checkpoint(tmp_path):
    db_file = tmp_path / "test_wal.db"
    store = StateStore(str(db_file))

    # Insert a deal
    store.mark_posted(
        deal_id="amz-B0TESTWAL1",
        retailer="Amazon",
        canonical_product_id="amazon:B0TESTWAL1",
        price=1999,
        chat_id="-1001",
        message_id=42,
    )

    # Checkpoint should execute cleanly without error
    store.checkpoint()
    store.checkpoint_and_close()

    # Reconnect in a new StateStore instance to simulate runner restart
    new_store = StateStore(str(db_file))
    assert new_store.is_posted("amz-B0TESTWAL1", canonical_product_id="amazon:B0TESTWAL1", current_price=1999) is True


# ── 2. PREVENTING THE INFINITE 15% PRICE-DROP SPAM LOOP & ZERO PRICE ───────────

def test_price_drop_baseline_update_prevents_infinite_loop(tmp_path):
    db_file = tmp_path / "test_drop_loop.db"
    store = StateStore(str(db_file))

    canon_id = "amazon:B0LOOP01"
    deal_id = "amz-B0LOOP01"

    # Cycle 1: Posted at ₹1,000
    store.mark_posted(
        deal_id=deal_id,
        retailer="Amazon",
        canonical_product_id=canon_id,
        price=1000,
        chat_id="-1001",
        message_id=10,
    )

    # Cycle 2: Same price -> blocked
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=1000) is True

    # Cycle 3: Genuine price drop to ₹800 (-20%) -> allowed
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=800) is False
    assert store.is_price_drop(deal_id, canonical_product_id=canon_id, current_price=800) is True

    # Post at ₹800 (re-posting updates the baseline price in SQLite)
    store.mark_posted(
        deal_id=deal_id,
        retailer="Amazon",
        canonical_product_id=canon_id,
        price=800,
        chat_id="-1001",
        message_id=11,
    )

    # Cycle 4 (1 hour later): Feed still sends ₹800. Must NOT re-post!
    # Comparing 800 against new baseline 800 is not a drop -> BLOCKED!
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=800) is True

    # Zero or negative prices from malformed feeds must NEVER trigger a price-drop exception
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=0) is True
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=-100) is True
    assert store.is_price_drop(deal_id, canonical_product_id=canon_id, current_price=0) is False

    # Anomalous price glitch (₹19 when prev price was ₹800 or original was ₹10,000) -> blocked
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=19, original_price=10000) is True


# ── 3. PRE-POST LIVE AMAZON ENRICHMENT & VERIFICATION ─────────────────────────

def test_enrich_amazon_deal_live_data():
    mock_html = """
    <html>
      <body>
        <div id="corePriceDisplay_desktop_feature_div">
          <span class="a-price-whole">1,499</span>
          <span class="a-price a-text-price"><span class="a-offscreen">₹3,999</span></span>
        </div>
        <div id="averageCustomerReviews">
          <span class="a-icon-alt">4.3 out of 5 stars</span>
          <span id="acrCustomerReviewText">2,450 ratings</span>
        </div>
        <div id="couponBadge">
          <label>Apply ₹200 coupon</label>
        </div>
        <div id="bankOffers_feature_div">
          <span>10% Instant Discount on HDFC Bank Cards</span>
        </div>
        <div id="imgTagWrapperId">
          <img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/71xyz_hires.jpg" src="https://m.media-amazon.com/images/I/71xyz_low.jpg" />
        </div>
        <div id="buyNow">
          <input id="buy-now-button" type="submit" value="Buy Now" />
        </div>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch("requests.get", return_value=mock_resp):
        info = enrich_amazon_deal("https://www.amazon.in/dp/B0LIVE01")
        assert info["stock_status"] == "in_stock"
        assert info["live_price"] == 1499
        assert info["mrp"] == 3999
        assert info["rating"] == 4.3
        assert info["review_count"] == 2450
        assert "₹200 coupon" in info["coupon"]
        assert "HDFC" in info["bank_offer"]
        assert info["hi_res_image"] == "https://m.media-amazon.com/images/I/71xyz_hires.jpg"


def test_enrich_amazon_deal_captcha_returns_unknown():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body><h4>Enter the characters you see below</h4><form action='/validateCaptcha'></form></body></html>"

    with patch("requests.get", return_value=mock_resp):
        info = enrich_amazon_deal("https://www.amazon.in/dp/B0CAPTCHA")
        assert info["stock_status"] == "unknown"
        assert info["live_price"] is None


# ── 4. IN-STOCK PRICE SPIKE DETECTION (LIGHTNING DEAL EXPIRATION) ──────────────

def test_price_spike_telegram_update():
    token = "test_token"
    chat_id = "-100123"
    msg_id = 999
    original_caption = "⚡ <b>DEAL</b> · Amazon\n<b>₹999</b> · <b>75% OFF</b>\n\nSony Earbuds"

    with patch("dealbot.publisher.safe_telegram_post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        success = mark_telegram_deal_price_spike(
            token=token,
            chat_id=chat_id,
            message_id=msg_id,
            original_caption=original_caption,
            new_price=3999,
            is_photo=True,
        )
        assert success is True
        assert mock_post.call_count == 1
        called_payload = mock_post.call_args[1]["payload"]
        assert "PRICE INCREASED TO ₹3,999" in called_payload["caption"]
        assert "Sony Earbuds" in called_payload["caption"]


def test_sweep_expired_deals_with_price_spike(tmp_path):
    from dealbot.main import sweep_expired_deals

    db_file = tmp_path / "test_sweep.db"
    store = StateStore(str(db_file))

    deal_id = "amz-B0SPIKE1"
    store.mark_posted(
        deal_id=deal_id,
        retailer="Amazon",
        chat_id="-1001",
        message_id=55,
        caption="⚡ <b>DEAL</b> · Amazon\n<b>₹999</b>\nDeal Title",
        price=999,
    )

    # Mock enrichment returning in_stock with price spiked to ₹2,499 (+150%)
    spiked_enrichment = {
        "stock_status": "in_stock",
        "live_price": 2499,
    }

    with patch("dealbot.main.inspect_stock", return_value="in_stock"), \
         patch("dealbot.main.enrich_amazon_deal", return_value=spiked_enrichment):

        # Check 1: First signal detected
        sweep_expired_deals(store, dry_run=True)
        active = store.get_recent_active_deals(limit=5)
        assert any(d["deal_id"] == deal_id for d in active)

        # Check 2: Second consecutive signal confirms price spike -> marked expired
        sweep_expired_deals(store, dry_run=True)
        active_after = store.get_recent_active_deals(limit=5)
        assert not any(d["deal_id"] == deal_id for d in active_after)


# ── 5. WORLD-CLASS PRODUCT CURATION ENGINE (BUYER VALUE SCORE) ─────────────────

def test_product_curation_brand_tier_and_scoring():
    # 1. Tier-1 Authority Brands
    assert get_brand_tier("Sony WH-1000XM5 Wireless Headphones") == 1
    assert get_brand_tier("Apple iPad 10th Gen") == 1
    assert get_brand_tier("Samsung Galaxy S24 Ultra") == 1
    assert get_brand_tier("Nike Men Running Shoes") == 1

    # 2. Tier-2 Trusted Brands
    assert get_brand_tier("boAt Rockerz 450 Bluetooth Headphones") == 2
    assert get_brand_tier("Noise ColorFit Pro Smartwatch") == 2

    # 3. Unbranded / Generic
    assert get_brand_tier("High Speed USB Type C Charging Cable") == 0


def test_fake_mrp_generic_junk_penalty():
    junk_cable = Deal(
        id="cable-junk",
        retailer="Amazon",
        title="Generic Fast USB Cable",
        url="https://amazon.in/dp/JUNK1",
        current_price="₹99",
        original_price="₹1,999",
        discount_percent=95,
    )
    scored_junk = score_deal(junk_cable, recent_categories=[])
    assert "fake-mrp generic junk penalty" in scored_junk.reason
    assert scored_junk.score < 40  # Below quality floor


def test_social_proof_and_loot_alert_boost():
    sony_flagship = Deal(
        id="sony-loot",
        retailer="Amazon",
        title="Sony WH-1000XM4 Noise Cancelling Headphones",
        url="https://amazon.in/dp/SONY01",
        current_price="₹14,999",
        original_price="₹29,999",
        discount_percent=50,
        rating=4.6,
        review_count=12500,
    )
    scored = score_deal(sony_flagship, recent_categories=[])
    assert "Tier-1 brand" in scored.reason
    assert "strong social proof" in scored.reason
    assert scored.score >= 70

    # Loot badge on steep Tier-1 discount
    sony_loot = Deal(
        id="sony-super-loot",
        retailer="Amazon",
        title="Sony Extra Bass Wireless Earbuds",
        url="https://amazon.in/dp/SONY02",
        current_price="₹2,999",
        original_price="₹9,999",
        discount_percent=70,
    )
    scored_loot = score_deal(sony_loot, recent_categories=[])
    assert sony_loot.is_loot is True
    assert "🚨 LOOT ALERT 🔥" in scored_loot.reason
    assert deal_badge(sony_loot) == "LOOT ALERT"


def test_narrow_product_type_fatigue_guard():
    assert extract_narrow_type("boAt Bassheads 100 Wired Earphones") == "earphone"
    assert extract_narrow_type("Noise Pulse 2 Smartwatch") in ("smartwatch", "watch")
    assert extract_narrow_type("Mi 20000mAh Power Bank 3i") == "power bank"
    assert extract_narrow_type("Generic Nylon Braided Type C Cable") == "cable"


# ── 6. HARDENED CRITIC NORMALIZATION & PER-DEAL BATCH ISOLATION ────────────────

def test_critic_normalizes_indian_formatting_and_decimals():
    deal = Deal(
        id="amz-B0INDIAN",
        retailer="Amazon",
        title="OnePlus 12R Smartphone",
        url="https://amazon.in/dp/B0INDIAN",
        current_price="₹1,24,999.00",
        original_price="₹1,39,999",
        discount_percent=11,
    )

    # 1. Standard Indian formatted copy with commas and ₹
    indian_fmt_copy = "Grab OnePlus 12R for ₹1,24,999 with 11% off today!"
    assert validate_critic(indian_fmt_copy, deal) is True
    assert _ai_copy_keeps_price(indian_fmt_copy, deal) is True

    # 2. Copy with decimals .00
    decimal_copy = "Get the OnePlus 12R at ₹1,24,999.00!"
    assert validate_critic(decimal_copy, deal) is True
    assert _ai_copy_keeps_price(decimal_copy, deal) is True

    # 3. Hallucinated bank offer rejection
    deal_no_bank = Deal(
        id="amz-B0NOBANK",
        retailer="Amazon",
        title="Boat Earbuds",
        url="https://amazon.in/dp/B0NOBANK",
        current_price="₹999",
        discount_percent=50,
        bank_offer=None,
    )
    fake_bank_copy = "Boat Earbuds at ₹999 with 50% off + instant 10% discount on HDFC cards!"
    assert validate_critic(fake_bank_copy, deal_no_bank) is False


def test_per_deal_batch_isolation_in_agent_llm():
    deals = [
        Deal(
            id="deal-1",
            retailer="Amazon",
            title="Sony Wireless Headphones",
            url="https://amazon.in/dp/D1",
            current_price="₹1,499",
            original_price="₹2,999",
            discount_percent=50,
        ),
        Deal(
            id="deal-2",
            retailer="Amazon",
            title="Philips Beard Trimmer",
            url="https://amazon.in/dp/D2",
            current_price="₹899",
            original_price="₹1,499",
            discount_percent=40,
        ),
    ]

    mock_copywriter = MagicMock()
    mock_copywriter.is_configured.return_value = True

    # LLM returns valid copy for deal-1, but hallucinated price ₹199 for deal-2
    mock_copywriter.generate_text.return_value = """{
      "posts": [
        {"id": "deal-1", "why": "great saving", "copy": "Sony Headphones at ₹1,499 with 50% discount!"},
        {"id": "deal-2", "why": "trimmer deal", "copy": "Philips Trimmer at ₹199 crazy steal!"}
      ]
    }"""

    agent = DealAgent(copywriter=mock_copywriter, min_score=20)
    plan = agent.plan(deals, recent_categories=[], max_posts=2)

    # Both deals should be scheduled for posting
    assert len(plan.posts) == 2
    by_id = {p.deal.id: p.deal for p in plan.posts}

    # deal-1 gets the validated AI copy
    assert getattr(by_id["deal-1"], "_cached_ai_copy", None) == "Sony Headphones at ₹1,499 with 50% discount!"

    # deal-2 failed Critic, so its cached copy is None (falls back cleanly to template!)
    assert getattr(by_id["deal-2"], "_cached_ai_copy", None) is None
