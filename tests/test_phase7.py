import html
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from dealbot.models import Deal
from dealbot.agent import (
    DealAgent,
    ScoredDeal,
    compute_buy_confidence,
    format_buy_confidence_meter,
    score_deal,
)
from dealbot.publisher import (
    StateStore,
    format_caption,
    send_telegram_deal,
    send_top_live_deals_digest,
)
from dealbot.ai_copywriter import validate_critic
from dealbot.main import deduplicate_deals_intra_cycle, run_once
from dealbot.regions import (
    get_currency_symbol,
    get_impulse_tiers,
    get_keepa_url,
    get_market_region,
    get_price_history_url,
    get_region_config,
)
from dealbot.sources import expand_and_convert_url


# ── 1. MULTI-SOURCE VIRAL CONSENSUS DETECTOR TESTS ───────────────────────────

def test_multi_source_viral_consensus_dedup_and_boost():
    """Verify that identical ASINs from distinct communities merge, accumulate sources, and earn viral boost."""
    deal_ifs = Deal(
        id="ifs-B0VIRAL001",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Headphones",
        url="https://www.amazon.in/dp/B0VIRAL001",
        current_price="₹24,990",
        original_price="₹34,990",
        discount_percent=29,
        canonical_product_id="amazon:B0VIRAL001",
        source_platform="indiafreestuff",
        matched_sources=["indiafreestuff"],
        source_count=1,
    )

    deal_reddit = Deal(
        id="amz-reddit-B0VIRAL001",
        retailer="Amazon",
        title="Sony WH-1000XM5 ANC Headphone Deal",
        url="https://www.amazon.in/dp/B0VIRAL001",
        current_price="₹24,990",
        original_price="₹34,990",
        discount_percent=29,
        canonical_product_id="amazon:B0VIRAL001",
        source_platform="reddit",
        matched_sources=["reddit"],
        source_count=1,
    )

    deal_movers = Deal(
        id="amz-B0VIRAL001",
        retailer="Amazon",
        title="Trending Deal: Amazon Movers & Shakers (B0VIRAL001)",
        url="https://www.amazon.in/dp/B0VIRAL001",
        canonical_product_id="amazon:B0VIRAL001",
        source_platform="amazon_movers",
        matched_sources=["amazon_movers"],
        source_count=1,
    )

    # 1. Intra-cycle deduplication merges 3 sources into 1 candidate
    deduped = deduplicate_deals_intra_cycle([deal_ifs, deal_reddit, deal_movers])
    assert len(deduped) == 1
    winner = deduped[0]
    assert winner.canonical_product_id == "amazon:B0VIRAL001"
    assert winner.source_count == 3
    assert set(winner.matched_sources) == {"indiafreestuff", "reddit", "amazon_movers"}

    # 2. Viral Consensus Score Boost (+15 for 2 sources, +22 for 3+ sources)
    single_source_deal = Deal(
        id="single-1",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Headphones",
        url="https://www.amazon.in/dp/B0VIRAL001",
        current_price="₹24,990",
        original_price="₹34,990",
        discount_percent=29,
        source_count=1,
    )
    two_source_deal = Deal(
        id="two-1",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Headphones",
        url="https://www.amazon.in/dp/B0VIRAL001",
        current_price="₹24,990",
        original_price="₹34,990",
        discount_percent=29,
        source_count=2,
    )
    three_source_deal = Deal(
        id="three-1",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Headphones",
        url="https://www.amazon.in/dp/B0VIRAL001",
        current_price="₹24,990",
        original_price="₹34,990",
        discount_percent=29,
        source_count=3,
    )

    base_score = score_deal(single_source_deal, []).score
    two_score = score_deal(two_source_deal, []).score
    three_score = score_deal(three_source_deal, []).score

    assert two_score - base_score == 15
    assert three_score - base_score == 22

    # 3. Caption formatting displays viral badge
    caption = format_caption(winner)
    assert "🔥 Trending across 3 deal communities" in caption


# ── 2. BUYHATKE BUY-NOW CONFIDENCE METER & PRICE TRAJECTORY ──────────────────

def test_buyhatke_buy_confidence_meter_and_price_trajectory():
    """Verify BuyHatke-style 0-99% confidence score, visual meter, and price trajectory sparkline."""
    # 1. High-confidence Screaming Deal
    high_deal = Deal(
        id="amz-high-deal",
        retailer="Amazon",
        title="Apple iPad 10th Gen 64GB",
        url="https://www.amazon.in/dp/B09V3HM4XYZ",
        current_price="₹27,999",
        original_price="₹39,900",
        discount_percent=30,
        rating=4.6,
        review_count=3500,
        price_context="historical_low",
        source_count=3,
    )
    conf = compute_buy_confidence(high_deal, score=85)
    assert conf >= 90
    meter_str = format_buy_confidence_meter(conf)
    assert f"🟢 Buy Confidence: {conf}%" in meter_str
    assert "Screaming Deal, Buy Now!" in meter_str

    # 2. Medium-confidence Solid Deal
    mid_deal = Deal(
        id="amz-mid-deal",
        retailer="Amazon",
        title="Portronics Fast Charger 65W",
        url="https://www.amazon.in/dp/B0XYZPORT1",
        current_price="₹1,299",
        original_price="₹1,999",
        discount_percent=35,
        rating=4.1,
        review_count=120,
    )
    conf_mid = compute_buy_confidence(mid_deal, score=60)
    assert 80 <= conf_mid <= 89
    meter_mid = format_buy_confidence_meter(conf_mid)
    assert f"🟢 Buy Confidence: {conf_mid}%" in meter_mid
    assert "Strong Price Drop" in meter_mid

    # 3. Low-confidence deal (<80%)
    low_deal = Deal(
        id="amz-low-deal",
        retailer="Amazon",
        title="Unbranded Generic Cover",
        url="https://www.amazon.in/dp/B0GENERIC1",
        current_price="₹199",
    )
    conf_low = compute_buy_confidence(low_deal, score=30)
    assert conf_low < 80
    meter_low = format_buy_confidence_meter(conf_low)
    assert f"🟡 Buy Confidence: {conf_low}%" in meter_low
    assert "Good Value" in meter_low

    # 4. Price Trajectory Sparkline in format_caption
    high_deal.price_trend_text = "📉 Price Trend: ₹34,900 (Avg) ➔ ₹27,999 (Now!)"
    high_deal.buy_confidence = conf
    caption = format_caption(high_deal)
    assert "📉 Price Trend: ₹34,900 (Avg) ➔ ₹27,999 (Now!)" in caption
    assert f"🟢 Buy Confidence: {conf}%" in caption


# ── 3. GRABON STACKABLE DISCOUNT WATERFALL & 1-TAP PROMO CODES ───────────────

def test_grabon_waterfall_discount_and_1tap_promo_code():
    """Verify GrabOn-style HTML waterfall calculation, promo code <code> tag, and critic validation."""
    deal = Deal(
        id="amz-grabon-01",
        retailer="Amazon",
        title="Philips Series 5000 Air Fryer",
        url="https://www.amazon.in/dp/B0PHILAIR5",
        original_price="₹9,995",
        current_price="₹6,999",
        discount_percent=30,
        effective_price=5499,
        coupon_discount=500,
        bank_discount_amount=1000,
        promo_code="SAVE500",
        bank_offer="Flat ₹1,000 Instant Discount on HDFC Bank Cards",
    )

    caption = format_caption(deal)

    # 1. Effective price headline
    assert "Effective Price: ₹5,499" in caption

    # 2. Stackable Waterfall breakdown
    assert "🧾 MRP: <del>₹9,995</del> | List: ₹6,999 | Coupon: -₹500 | Bank: -₹1,000" in caption

    # 3. 1-Tap Copy Promo Code
    assert "🎟️ Promo Code: <code>SAVE500</code> <i>(Tap to copy)</i>" in caption

    # 4. validate_critic accepts the promo code
    ai_copy_valid = "Get Philips Series 5000 Air Fryer for ₹5,499 with promo code SAVE500!"
    assert validate_critic(ai_copy_valid, deal) is True

    ai_copy_fake = "Get Philips Air Fryer for ₹5,499 with promo code FAKECODE!"
    assert validate_critic(ai_copy_fake, deal) is False


# ── 4. DUAL PRICE-VERIFICATION INLINE KEYBOARD ───────────────────────────────

def test_dual_price_verification_inline_keyboard(monkeypatch):
    """Verify 3-row inline keyboard: Primary CTA, Keepa + PriceHistory Check, and Worth it callback."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:fake_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456789")
    monkeypatch.setenv("MARKET_REGION", "IN")

    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Apple iPad 10th Gen",
        url="https://www.amazon.in/dp/B0BS1QCFHX?tag=dammaiprasad-21",
        current_price="₹29,999",
        discount_percent=25,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 888}}

    with patch("dealbot.publisher.safe_telegram_post", return_value=mock_resp) as mock_post:
        ok, msg_id, caption = send_telegram_deal(deal, dry_run=False)
        assert ok is True
        assert msg_id == 888

        sent_payload = mock_post.call_args[1]["payload"]
        keyboard = sent_payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 3

        # Row 1: Primary CTA
        assert "Grab Deal @ ₹29,999 (25% OFF)" in keyboard[0][0]["text"]
        assert keyboard[0][0]["url"] == deal.url

        # Row 2: Verification tools
        assert "Keepa Chart" in keyboard[1][0]["text"]
        assert keyboard[1][0]["url"] == "https://keepa.com/#!product/10-B0BS1QCFHX"

        assert "Price History Check" in keyboard[1][1]["text"]
        assert keyboard[1][1]["url"] == "https://pricehistoryapp.com/product/B0BS1QCFHX"

        # Row 3: Social feedback
        assert "Worth it" in keyboard[2][0]["text"]
        assert keyboard[2][0]["callback_data"] == "worth:amz-B0BS1QCFHX"


# ── 5. AUTOMATED NIGHTLY TOP-5 LIVE MEGA-DEALS DIGEST ────────────────────────

def test_nightly_top5_live_deals_digest(tmp_path, monkeypatch):
    """Verify nightly digest generates curated summary of still-live deals, pins message, and gates once per day."""
    db_file = tmp_path / "test_digest.db"
    store = StateStore(db_file)

    # 1. Post 4 active deals and 1 expired deal
    for i in range(1, 5):
        store.mark_posted(
            deal_id=f"amz-B00000000{i}",
            retailer="Amazon",
            chat_id="-1001",
            message_id=100 + i,
            caption=f"🔥 <b>LOOT</b> · Amazon\n<b>Product {i} High Quality Gadget</b>\n<a href=\"https://www.amazon.in/dp/B00000000{i}\">Buy</a>",
            price=1000 * i,
            discount=20 + i * 5,
            agent_score=70 + i,
            canonical_product_id=f"amazon:B00000000{i}",
        )
    # Mark deal 1 as expired
    store.mark_expired("amz-B000000001")

    # 2. Test get_top_live_deals_today retrieves only still-active deals
    live_deals = store.get_top_live_deals_today(limit=5)
    assert len(live_deals) == 3
    assert all(d["deal_id"] != "amz-B000000001" for d in live_deals)
    # Highest discount first
    assert live_deals[0]["discount"] >= live_deals[1]["discount"]

    # 3. Test send_top_live_deals_digest dry-run
    ok = send_top_live_deals_digest(store, chat_id="-1001", token="fake_token", dry_run=True)
    assert ok is True

    # 4. Test live send with Telegram API pinning
    post_resp = MagicMock(status_code=200)
    post_resp.json.return_value = {"ok": True, "result": {"message_id": 9999}}
    pin_resp = MagicMock(status_code=200)
    pin_resp.json.return_value = {"ok": True}

    with patch("dealbot.publisher.safe_telegram_post", side_effect=[post_resp, pin_resp]) as mock_call:
        sent = send_top_live_deals_digest(store, chat_id="-1001", token="fake_token", dry_run=False)
        assert sent is True
        assert mock_call.call_count == 2
        # First call is sendMessage
        assert "sendMessage" in mock_call.call_args_list[0][0][0]
        # Second call is pinChatMessage
        assert "pinChatMessage" in mock_call.call_args_list[1][0][0]
        assert mock_call.call_args_list[1][1]["payload"]["message_id"] == 9999

    # 5. When fewer than 3 deals are live, digest is skipped
    store.mark_expired("amz-B000000002")
    skipped = send_top_live_deals_digest(store, chat_id="-1001", token="fake_token", dry_run=False)
    assert skipped is False


# ── 6. GLOBAL MULTI-REGION ENGINE ARCHITECTURE ───────────────────────────────

def test_global_multi_region_architecture(monkeypatch):
    """Verify seamless switching across IN (default), US, UK, and AE regional configurations."""
    # Default IN
    assert get_market_region() == "IN"
    assert get_currency_symbol() == "₹"
    assert get_region_config()["domain"] == "amazon.in"
    assert get_keepa_url("B0TESTASIN1") == "https://keepa.com/#!product/10-B0TESTASIN1"
    assert get_price_history_url("B0TESTASIN1") == "https://pricehistoryapp.com/product/B0TESTASIN1"

    # US Profile
    monkeypatch.setenv("MARKET_REGION", "US")
    assert get_market_region() == "US"
    assert get_currency_symbol() == "$"
    assert get_region_config()["domain"] == "amazon.com"
    assert get_keepa_url("B0TESTASIN1") == "https://keepa.com/#!product/1-B0TESTASIN1"
    assert get_price_history_url("B0TESTASIN1") == "https://camelcamelcamel.com/product/B0TESTASIN1"
    clean_url, deal_id, ret = expand_and_convert_url("https://www.amazon.com/dp/B0TESTASIN1")
    assert "amazon.com" in clean_url
    assert ret == "Amazon"

    # UK Profile
    monkeypatch.setenv("MARKET_REGION", "UK")
    assert get_market_region() == "UK"
    assert get_currency_symbol() == "£"
    assert get_region_config()["domain"] == "amazon.co.uk"
    assert get_keepa_url("B0TESTASIN1") == "https://keepa.com/#!product/2-B0TESTASIN1"
    assert get_price_history_url("B0TESTASIN1") == "https://uk.camelcamelcamel.com/product/B0TESTASIN1"

    # AE Profile
    monkeypatch.setenv("MARKET_REGION", "AE")
    assert get_market_region() == "AE"
    assert get_currency_symbol() == "AED"
    assert get_region_config()["domain"] == "amazon.ae"
    assert get_keepa_url("B0TESTASIN1") == "https://keepa.com/#!product/9-B0TESTASIN1"

    # Impulse price tiers differ by region
    tiers_in = get_impulse_tiers("IN")
    tiers_us = get_impulse_tiers("US")
    assert tiers_in != tiers_us
    assert (49, 99) in tiers_in
    assert (5, 10) in tiers_us
