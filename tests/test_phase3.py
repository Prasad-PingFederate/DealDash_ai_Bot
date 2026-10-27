import pytest
from unittest.mock import patch, MagicMock
from dealbot.models import Deal, parse_rupees
from dealbot.publisher import StateStore, format_caption
from dealbot.agent import score_deal, DealAgent, get_brand_tier
from dealbot.expiry_checker import enrich_amazon_deal
from dealbot.sources import fetch_reddit_deals, fetch_amazon_movers_and_shakers, collect_all_deals
from dealbot.main import verify_and_enrich_deal, run_once


# ── 1. TWO-STAGE ENRICHMENT FUNNEL WITH BACKFILL ───────────────────────────────

def test_two_stage_funnel_backfill_on_rejected_candidate(tmp_path):
    db_file = tmp_path / "test_state.sqlite3"
    store = StateStore(str(db_file))

    deal_spike = Deal(
        id="amz-spike",
        retailer="Amazon",
        title="Branded Wireless Headphones",
        url="https://amazon.in/dp/B0SPIKE",
        current_price="₹1,000",
        original_price="₹2,000",
        discount_percent=50,
    )
    deal_good = Deal(
        id="amz-good",
        retailer="Amazon",
        title="Sony Portable Speaker",
        url="https://amazon.in/dp/B0GOOD",
        current_price="₹1,500",
        original_price="₹3,000",
        discount_percent=50,
    )

    # deal_spike will jump in price > 20%
    mock_spike = {
        "stock_status": "in_stock",
        "live_price": 1500,  # +50% jump from 1000
        "mrp": 2000,
        "rating": 4.5,
        "review_count": 100,
        "coupon": None,
        "coupon_discount": None,
        "effective_price": None,
        "bank_offer": None,
        "hi_res_image": None,
    }
    # deal_good is verified
    mock_good = {
        "stock_status": "in_stock",
        "live_price": 1499,
        "mrp": 3000,
        "rating": 4.5,
        "review_count": 500,
        "coupon": None,
        "coupon_discount": None,
        "effective_price": None,
        "bank_offer": None,
        "hi_res_image": None,
    }

    with patch("dealbot.main.inspect_stock", return_value="in_stock"):
        with patch("dealbot.main.enrich_amazon_deal", return_value=mock_spike):
            res_spike = verify_and_enrich_deal(deal_spike, store)
            assert res_spike is False  # Rejected because price jumped > 20%

        with patch("dealbot.main.enrich_amazon_deal", return_value=mock_good):
            res_good = verify_and_enrich_deal(deal_good, store)
            assert res_good is True  # Verified
            assert deal_good.checked is True
            assert deal_good.rating == 4.5


# ── 2. ON-PAGE COUPONS & EFFECTIVE PRICE CALCULATION ─────────────────────────

def test_enrich_amazon_flat_coupon_and_effective_price():
    mock_html = """
    <html>
      <body>
        <span class="a-price-whole">1,499</span>
        <span class="a-price a-text-price"><span class="a-offscreen">₹3,999</span></span>
        <span class="a-icon-alt">4.3 out of 5 stars</span>
        <span id="acrCustomerReviewText">1,200 ratings</span>
        <div>Apply ₹200 coupon</div>
        <div id="buyNow"><input value="Buy Now" /></div>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch("requests.get", return_value=mock_resp):
        info = enrich_amazon_deal("https://www.amazon.in/dp/B0COUPON01")
        assert info["stock_status"] == "in_stock"
        assert info["live_price"] == 1499
        assert info["coupon_discount"] == 200
        assert info["effective_price"] == 1299


def test_enrich_amazon_percent_coupon_and_effective_price():
    mock_html = """
    <html>
      <body>
        <span class="a-price-whole">2,000</span>
        <span class="a-price a-text-price"><span class="a-offscreen">₹5,000</span></span>
        <span class="a-icon-alt">4.4 out of 5 stars</span>
        <span id="acrCustomerReviewText">800 ratings</span>
        <div>Save 10% with coupon</div>
        <div id="buyNow"><input value="Buy Now" /></div>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch("requests.get", return_value=mock_resp):
        info = enrich_amazon_deal("https://www.amazon.in/dp/B0COUPON02")
        assert info["stock_status"] == "in_stock"
        assert info["live_price"] == 2000
        assert info["coupon_discount"] == 200
        assert info["effective_price"] == 1800


def test_format_caption_displays_effective_price():
    deal = Deal(
        id="amz-eff-01",
        retailer="Amazon",
        title="Logitech MX Master 3S Mouse",
        url="https://amazon.in/dp/B0LOGI",
        current_price="₹7,999",
        original_price="₹10,995",
        coupon_discount=500,
        effective_price=7499,
    )
    caption = format_caption(deal)
    assert "Effective Price: ₹7,499" in caption
    assert "Apply ₹500 on-page coupon" in caption


# ── 3. HISTORICAL PRICE TRACKER (price_sightings) ─────────────────────────────

def test_price_sightings_running_average_and_context(tmp_path):
    db_file = tmp_path / "test_sightings.sqlite3"
    store = StateStore(str(db_file))
    canon_id = "amazon:B0TESTASIN"

    # Sighting 1: ₹1,000
    store.record_price_sighting(canon_id, 1000)
    ctx1 = store.get_price_context(canon_id, 1000)
    assert ctx1["status"] == "normal"
    assert ctx1["sighting_count"] == 1
    assert ctx1["min_price"] == 1000
    assert ctx1["avg_price"] == 1000.0

    # Sighting 2: ₹1,000
    store.record_price_sighting(canon_id, 1000)
    # Check drop to ₹800 (which is <= 1000 * 0.82)
    ctx2 = store.get_price_context(canon_id, 800)
    assert ctx2["status"] == "real_drop"
    assert ctx2["score_adj"] == 18

    # Sighting 3: ₹1,000
    store.record_price_sighting(canon_id, 1000)
    # Check all-time low (price < 1000 with count >= 3)
    ctx3 = store.get_price_context(canon_id, 750)
    assert ctx3["status"] == "all_time_low"
    assert ctx3["score_adj"] == 10

    # Sighting 4: ₹1,000
    store.record_price_sighting(canon_id, 1000)
    # Check stagnant price (current price ₹1,000 >= avg ₹1,000 * 0.98 with count >= 4)
    ctx4 = store.get_price_context(canon_id, 1000)
    assert ctx4["status"] == "stagnant"
    assert ctx4["score_adj"] == -10


def test_agent_scores_with_historical_price_signals():
    # Deal with real_drop signal
    deal_drop = Deal(
        id="deal-drop",
        retailer="Amazon",
        title="Sony Wireless Noise Cancelling Headphones",
        url="https://amazon.in/dp/SONYDROP",
        current_price="₹7,999",
        original_price="₹14,999",
        discount_percent=46,
        price_context="real_drop",
    )
    scored_drop = score_deal(deal_drop, recent_categories=[])
    assert "historical real drop (+18)" in scored_drop.reason

    # Deal with stagnant signal
    deal_stagnant = Deal(
        id="deal-stag",
        retailer="Amazon",
        title="Sony Wireless Noise Cancelling Headphones",
        url="https://amazon.in/dp/SONYSTAG",
        current_price="₹7,999",
        original_price="₹14,999",
        discount_percent=46,
        price_context="stagnant",
    )
    scored_stag = score_deal(deal_stagnant, recent_categories=[])
    assert "stagnant price (-10)" in scored_stag.reason
    assert scored_drop.score > scored_stag.score + 25


# ── 4. TIER-2 & UNBRANDED FAKE-MRP LOOPHOLE CLOSURE ───────────────────────────

def test_unbranded_fake_mrp_expanded_penalty_up_to_799():
    # Unbranded generic item at ₹499 with ₹2,999 MRP (83% discount)
    deal_generic = Deal(
        id="generic-tool",
        retailer="Amazon",
        title="Multipurpose Stainless Steel Kitchen Scissors",
        url="https://amazon.in/dp/GEN01",
        current_price="₹499",
        original_price="₹2,999",
        discount_percent=83,
    )
    scored = score_deal(deal_generic, recent_categories=[])
    assert "fake-mrp generic junk penalty" in scored.reason
    assert scored.score < 40  # Well below posting floor


def test_mrp_inflated_category_discount_cap():
    # Tier-2 boAt Smartwatch claiming 82% discount
    boat_watch = Deal(
        id="boat-watch-01",
        retailer="Amazon",
        title="boAt Wave Sigma Smartwatch with Bluetooth Calling",
        url="https://amazon.in/dp/BOAT01",
        current_price="₹1,299",
        original_price="₹7,499",
        discount_percent=82,
    )
    scored = score_deal(boat_watch, recent_categories=[])
    assert "mrp-inflated category cap" in scored.reason


def test_stricter_loot_alert_gating():
    # Tier-1 item with >= 60% discount but POOR rating -> NO Loot Alert
    poor_sony = Deal(
        id="sony-poor",
        retailer="Amazon",
        title="Sony Wired Earphones",
        url="https://amazon.in/dp/SONYPOOR",
        current_price="₹399",
        original_price="₹1,299",
        discount_percent=69,
        rating=3.4,  # Poor rating
        review_count=50,
    )
    scored_poor = score_deal(poor_sony, recent_categories=[])
    assert poor_sony.is_loot is False
    assert "🚨 LOOT ALERT 🔥" not in scored_poor.reason

    # Tier-1 item with >= 60% discount and GOOD rating -> Loot Alert active
    good_sony = Deal(
        id="sony-good",
        retailer="Amazon",
        title="Sony Wired Earphones",
        url="https://amazon.in/dp/SONYGOOD",
        current_price="₹399",
        original_price="₹1,299",
        discount_percent=69,
        rating=4.2,  # Good rating
        review_count=500,
    )
    scored_good = score_deal(good_sony, recent_categories=[])
    assert good_sony.is_loot is True
    assert "🚨 LOOT ALERT 🔥" in scored_good.reason


# ── 5. MULTI-SOURCE SCOUTING (REDDIT & MOVERS) ────────────────────────────────

def test_fetch_reddit_deals_parses_json():
    mock_reddit_json = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Sony WH-CH520 Wireless Headphones at ₹3,499 (was ₹4,990)",
                        "url": "https://www.amazon.in/dp/B0CH520XYZ",
                        "selftext": "Great discount on Amazon India today!",
                    }
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_reddit_json

    with patch("requests.get", return_value=mock_resp):
        deals = fetch_reddit_deals(min_discount=20)
        assert len(deals) >= 1
        assert "B0CH520XYZ" in deals[0].url
        assert "amazon" in deals[0].retailer.lower()


def test_fetch_amazon_movers_and_shakers_handles_gracefully():
    # Test blocked page returns [] without throwing error
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    with patch("requests.get", return_value=mock_resp):
        deals = fetch_amazon_movers_and_shakers()
        assert deals == []


def test_run_once_two_stage_funnel_end_to_end(tmp_path, monkeypatch):
    db_file = tmp_path / "e2e_state.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MAX_POSTS_PER_CYCLE", "2")
    monkeypatch.setenv("AGENT_MODE", "true")
    monkeypatch.setenv("AGENT_LLM", "false")

    sample_deals = [
        Deal(
            id="amz-deal-1",
            retailer="Amazon",
            title="Sony WH-1000XM4 Wireless Noise Cancelling Headphones",
            url="https://www.amazon.in/dp/B000000001",
            current_price="₹19,999",
            original_price="₹29,999",
            discount_percent=33,
        ),
        Deal(
            id="amz-deal-2",
            retailer="Amazon",
            title="Philips Digital Air Fryer with Rapid Air Technology",
            url="https://www.amazon.in/dp/B000000002",
            current_price="₹6,999",
            original_price="₹12,995",
            discount_percent=46,
        ),
    ]

    with patch("dealbot.main.collect_all_deals", return_value=sample_deals):
        posted = run_once()
        assert posted == 2
        store = StateStore(str(db_file))
        # Sightings should have been recorded
        ctx = store.get_price_context("amz-deal-1", 19999)
        assert ctx["sighting_count"] >= 1
