from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from dealbot.agent import DealAgent, score_deal, current_slot, SLOT_BOOSTS
from dealbot.models import Deal
from dealbot.publisher import StateStore
from dealbot.session import mark_cycle, load_last_cycle, plan, IST
from dealbot.expiry_checker import enrich_amazon_deal
from dealbot.main import verify_and_enrich_deal, run_once


# ── 1. ADAPTIVE SOFT-FLOOR BACKFILL TESTS ─────────────────────────────────────

def test_soft_floor_backfill_when_all_scores_between_16_and_31():
    """When all candidates score between 16 and 31, soft-floor guarantees at least 1-2 deals."""
    deals = [
        Deal(
            id="cand-1",
            retailer="Amazon",
            title="Everyday Kitchen Knife",
            url="https://amazon.in/dp/CAND01",
            current_price="₹740",
            original_price="₹1,000",
            discount_percent=26,
            category="home",
        ),
        Deal(
            id="cand-2",
            retailer="Amazon",
            title="Standard Yoga Mat",
            url="https://amazon.in/dp/CAND02",
            current_price="₹1,100",
            original_price="₹1,500",
            discount_percent=27,
            category="fitness",
        ),
        Deal(
            id="cand-3",
            retailer="Amazon",
            title="Ceramic Coffee Mug",
            url="https://amazon.in/dp/CAND03",
            current_price="₹1,450",
            original_price="₹2,000",
            discount_percent=28,
            category="home",
        ),
    ]

    agent = DealAgent(min_score=32)
    # Ensure they are below score floor 32
    for d in deals:
        s = score_deal(d, recent_categories=[], slot="night")
        assert 16 <= s.score < 32, f"Deal {d.id} score {s.score} not in 16..31"

    plan_result = agent.plan(deals, recent_categories=[], max_posts=3)
    assert plan_result.soft_floor_used is True
    assert len(plan_result.posts) >= 1
    assert any("soft-floor backfill" in item.reason for item in plan_result.posts)


def test_soft_floor_backfill_does_not_trigger_when_qualified_exist():
    """When qualified candidates (>= min_score) exist, soft-floor is not activated."""
    deals = [
        Deal(
            id="high-1",
            retailer="Amazon",
            title="Sony WH-1000XM4 Wireless Active Noise Cancelling Headphones",
            url="https://amazon.in/dp/HIGH01",
            current_price="₹19,999",
            original_price="₹29,999",
            discount_percent=33,
            category="electronics",
            rating=4.6,
            review_count=1200,
        ),
        Deal(
            id="high-2",
            retailer="Amazon",
            title="Apple iPad 10th Gen 64GB WiFi",
            url="https://amazon.in/dp/HIGH02",
            current_price="₹29,999",
            original_price="₹39,900",
            discount_percent=25,
            category="electronics",
            rating=4.7,
            review_count=3500,
        ),
    ]

    agent = DealAgent(min_score=32)
    plan_result = agent.plan(deals, recent_categories=[], max_posts=2)
    assert plan_result.soft_floor_used is False
    assert len(plan_result.posts) == 2


def test_soft_floor_backfill_rejects_poorly_rated_items():
    """Soft-floor backfill never selects items explicitly rated < 3.6★."""
    deals = [
        Deal(
            id="low-rated-1",
            retailer="Amazon",
            title="Budget Wireless Earphones With Mic",
            url="https://amazon.in/dp/LOWR01",
            current_price="₹399",
            original_price="₹999",
            discount_percent=60,
            rating=3.2,
            review_count=50,
        ),
    ]
    agent = DealAgent(min_score=32)
    plan_result = agent.plan(deals, recent_categories=[], max_posts=2)
    assert len(plan_result.posts) == 0


# ── 2. ENRICHMENT DOM HANDLING & RATING IS STRICTLY NONE ───────────────────────

def test_enrich_amazon_deal_strictly_none_when_dom_missing():
    """When DOM rating/reviews are absent, fields must be strictly None (never 0 or 0.0)."""
    minimal_html = """
    <html>
      <body>
        <span class="a-price-whole">1,499</span>
        <span class="basisPrice"><span class="a-offscreen">₹2,999</span></span>
        <span id="submit.add-to-cart">Add to Cart</span>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = minimal_html

    with patch("requests.get", return_value=mock_resp):
        res = enrich_amazon_deal("https://www.amazon.in/dp/B0TESTNONE")
        assert res["rating"] is None
        assert res["review_count"] is None
        assert res["live_price"] == 1499
        assert res["mrp"] == 2999


def test_verify_and_enrich_deal_allows_unrated_items(tmp_path):
    """Unrated items (rating=None) must NOT be discarded by verify_and_enrich_deal."""
    db_file = tmp_path / "test_unrated.sqlite3"
    store = StateStore(str(db_file))

    deal = Deal(
        id="unrated-deal",
        retailer="Amazon",
        title="Pigeon Multi-Purpose Kettle 1.5L",
        url="https://www.amazon.in/dp/B0UNRATED",
        current_price="₹699",
        original_price="₹1,295",
        discount_percent=46,
    )

    enrichment_data = {
        "stock_status": "in_stock",
        "live_price": 699,
        "mrp": 1295,
        "rating": None,  # unrated
        "review_count": None,
        "coupon": None,
        "coupon_discount": None,
        "effective_price": None,
        "bank_offer": None,
        "hi_res_image": None,
    }

    with patch("dealbot.main.inspect_stock", return_value="in_stock"), \
         patch("dealbot.main.enrich_amazon_deal", return_value=enrichment_data):
        valid = verify_and_enrich_deal(deal, store)
        assert valid is True
        assert deal.rating is None


# ── 3. SMART LIVE PRICE DISCREPANCY HANDLING ──────────────────────────────────

def test_smart_live_price_discrepancy_datacenter_mrp_match(tmp_path):
    """When Amazon live price matches feed MRP (datacenter IP issue), do not discard; apply -8 penalty."""
    db_file = tmp_path / "test_discrepancy.sqlite3"
    store = StateStore(str(db_file))

    deal = Deal(
        id="amz-discrepancy-1",
        retailer="Amazon",
        title="boAt Rockerz 450 Bluetooth On Ear Headphones",
        url="https://www.amazon.in/dp/B0BOAT450",
        current_price="₹1,499",
        original_price="₹3,990",
        discount_percent=62,
    )

    # Amazon returns the non-discounted MRP as live_price
    enrichment_data = {
        "stock_status": "in_stock",
        "live_price": 3990,
        "mrp": 3990,
        "rating": 4.1,
        "review_count": 800,
        "coupon": None,
        "coupon_discount": None,
        "effective_price": None,
        "bank_offer": None,
        "hi_res_image": None,
    }

    with patch("dealbot.main.inspect_stock", return_value="in_stock"), \
         patch("dealbot.main.enrich_amazon_deal", return_value=enrichment_data):
        valid = verify_and_enrich_deal(deal, store)
        assert valid is True
        assert getattr(deal, "score_penalty", 0) == 8
        # Feed price preserved rather than overwritten by inflated MRP
        assert deal.current_price == "₹1,499"


# ── 4. PACING GATE LOCKING ON EMPTY CYCLES ─────────────────────────────────────

def test_mark_cycle_skipped_when_posted_count_zero(tmp_path, monkeypatch):
    """mark_cycle() must not update last_cycle_at when posted_count == 0."""
    db_file = tmp_path / "pacing_zero.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    store = StateStore(str(db_file))

    assert store.get_meta("last_cycle_at") is None
    mark_cycle(posted_count=0)
    assert store.get_meta("last_cycle_at") is None

    mark_cycle(posted_count=1)
    assert store.get_meta("last_cycle_at") is not None


def test_empty_cycle_retry_sleep_15_minutes(tmp_path, monkeypatch):
    """If last cycle posted 0 deals, load_last_cycle schedules a 15-minute retry rather than 60m."""
    db_file = tmp_path / "empty_retry.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    store = StateStore(str(db_file))

    store.set_meta("last_cycle_posted_count", "0")
    # 5 minutes ago empty cycle
    empty_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.set_meta("last_cycle_empty_at", empty_time)
    store.checkpoint()

    last_cycle = load_last_cycle()
    assert last_cycle is not None

    now = datetime.now(IST)
    deadline = now + timedelta(hours=2)
    action, wait_sec = plan(now, last_cycle, deadline)
    assert action == "sleep"
    # Should wait ~10 minutes (600s), definitely < 15 minutes (900s)
    assert 500 <= wait_sec <= 650


# ── 5. TIME-OF-DAY AND PSYCHOLOGICAL PRICE BOOSTS ─────────────────────────────

def test_ist_time_of_day_boosts():
    """Verify morning, afternoon, and evening prime time psychological boosts."""
    home_deal = Deal(
        id="home-boost",
        retailer="Amazon",
        title="Prestige Induction Cooktop 1200W",
        url="https://amazon.in/dp/PREST01",
        current_price="₹1,899",
        original_price="₹3,295",
        discount_percent=42,
        category="home",
    )
    scored_morning = score_deal(home_deal, recent_categories=[], slot="morning")
    assert "fits morning shopping psychology (+8)" in scored_morning.reason

    fashion_deal = Deal(
        id="fashion-boost",
        retailer="Amazon",
        title="Puma Men Running Shoes",
        url="https://amazon.in/dp/PUMA01",
        current_price="₹1,499",
        original_price="₹3,999",
        discount_percent=62,
        category="fashion",
    )
    scored_afternoon = score_deal(fashion_deal, recent_categories=[], slot="afternoon")
    assert "fits afternoon shopping psychology (+8)" in scored_afternoon.reason

    tech_deal = Deal(
        id="tech-boost",
        retailer="Amazon",
        title="boAt Airdopes 141 Bluetooth True Wireless Earbuds",
        url="https://amazon.in/dp/BOAT141",
        current_price="₹999",
        original_price="₹4,490",
        discount_percent=78,
        category="electronics",
    )
    scored_evening = score_deal(tech_deal, recent_categories=[], slot="evening")
    assert "fits evening shopping psychology (+10)" in scored_evening.reason


def test_psychological_price_point_boost():
    """Verify +6 pts for impulse sweet spot prices with discount >= 35%."""
    sweet_deal = Deal(
        id="sweet-spot",
        retailer="Amazon",
        title="Portronics Wireless Optical Mouse",
        url="https://amazon.in/dp/PORT01",
        current_price="₹199",
        original_price="₹499",
        discount_percent=60,
    )
    scored_sweet = score_deal(sweet_deal, recent_categories=[], slot="night")
    assert "psychological price point (₹199) (+6)" in scored_sweet.reason

    # Not in sweet spot (₹120) -> no boost
    non_sweet_deal = Deal(
        id="non-sweet",
        retailer="Amazon",
        title="Portronics Mouse Pad",
        url="https://amazon.in/dp/PORT02",
        current_price="₹120",
        original_price="₹299",
        discount_percent=60,
    )
    scored_non_sweet = score_deal(non_sweet_deal, recent_categories=[], slot="night")
    assert "psychological price point" not in scored_non_sweet.reason

    # In sweet spot price (₹199) but discount < 35% (20%) -> no boost
    low_disc_deal = Deal(
        id="low-disc",
        retailer="Amazon",
        title="Portronics Cable",
        url="https://amazon.in/dp/PORT03",
        current_price="₹199",
        original_price="₹249",
        discount_percent=20,
    )
    scored_low_disc = score_deal(low_disc_deal, recent_categories=[], slot="night")
    assert "psychological price point" not in scored_low_disc.reason


# ── 6. FULL CYCLE FUNNEL TELEMETRY ─────────────────────────────────────────────

def test_cycle_funnel_telemetry(tmp_path, monkeypatch, caplog):
    """Verify [FUNNEL] telemetry is formatted and logged accurately in run_once()."""
    db_file = tmp_path / "funnel_test.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MAX_POSTS_PER_CYCLE", "2")
    monkeypatch.setenv("AGENT_MODE", "true")

    sample_deals = [
        Deal(
            id="f-1",
            retailer="Amazon",
            title="Philips Multigroom Series 3000 Trimmer",
            url="https://amazon.in/dp/PHIL01",
            current_price="₹1,299",
            original_price="₹2,195",
            discount_percent=41,
        ),
    ]

    import logging
    with caplog.at_level(logging.INFO), \
         patch("dealbot.main.collect_all_deals", return_value=sample_deals), \
         patch("dealbot.main.send_telegram_deal", return_value=(True, 101, "caption")):
        run_once()
        assert any("[FUNNEL] Raw:" in record.message for record in caplog.records)
        funnel_record = next(r for r in caplog.records if "[FUNNEL]" in r.message)
        msg = funnel_record.message
        assert "Raw: 1" in msg
        assert "Unposted: 1" in msg
        assert "Selected:" in msg
        assert "Posted:" in msg
