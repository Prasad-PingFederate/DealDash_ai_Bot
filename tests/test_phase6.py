import html
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from dealbot.agent import DealAgent, ScoredDeal
from dealbot.models import Deal
from dealbot.publisher import StateStore, format_caption
from dealbot.ai_copywriter import validate_critic
from dealbot.main import (
    deduplicate_deals_intra_cycle,
    sweep_expired_deals,
)


# ── 1. INTRA-CYCLE DEDUPLICATION TESTS ─────────────────────────────────────────

def test_intra_cycle_deduplication_collapses_duplicates_and_merges_metadata():
    """Verify that multiple feed entries for the same ASIN merge into one candidate keeping richer metadata."""
    deal_reddit = Deal(
        id="reddit-1",
        retailer="Amazon",
        title="Sony WH-CH520 Wireless Headphones",
        url="https://www.amazon.in/dp/B0CH520XYZ",
        current_price="₹3,499",
        original_price=None,  # Missing MRP
        discount_percent=None,
        category="reddit",
        image_url=None,  # Missing image
        canonical_product_id="amazon:B0CH520XYZ",
    )

    deal_desidime = Deal(
        id="dd-1",
        retailer="Amazon",
        title="Sony WH-CH520 Wireless On-Ear Bluetooth Headphones",
        url="https://www.amazon.in/dp/B0CH520XYZ",
        current_price="₹3,499",
        original_price="₹4,990",  # Has MRP
        discount_percent=30,
        category="electronics",
        image_url="https://m.media-amazon.com/images/I/sony.jpg",  # Has image
        canonical_product_id="amazon:B0CH520XYZ",
    )

    deduped = deduplicate_deals_intra_cycle([deal_reddit, deal_desidime])
    assert len(deduped) == 1
    winner = deduped[0]
    assert winner.canonical_product_id == "amazon:B0CH520XYZ"
    assert winner.original_price == "₹4,990"
    assert winner.image_url == "https://m.media-amazon.com/images/I/sony.jpg"
    assert winner.discount_percent == 30


# ── 2. VALIDATE CRITIC ACCEPTS EFFECTIVE PRICE ─────────────────────────────────

def test_validate_critic_accepts_effective_price_in_caption():
    """Verify that validate_critic accepts captions mentioning effective_price after coupon."""
    deal = Deal(
        id="amz-coupon-deal",
        retailer="Amazon",
        title="OnePlus Nord Buds 2r Wireless Earbuds",
        url="https://www.amazon.in/dp/B0ONEPLUS",
        current_price="₹1,999",
        original_price="₹2,999",
        discount_percent=33,
        effective_price=1599,  # Effective price after ₹400 coupon
        coupon_discount=400,
    )

    # Valid copy mentioning effective price ₹1,599
    copy_eff = "<b>OnePlus Nord Buds 2r</b> grab them now at just ₹1,599 with on-page coupon!"
    assert validate_critic(copy_eff, deal) is True

    # Valid copy mentioning list price ₹1,999
    copy_list = "<b>OnePlus Nord Buds 2r</b> listed at ₹1,999 with additional coupon savings."
    assert validate_critic(copy_list, deal) is True

    # Invalid copy mentioning hallucinated price ₹999
    copy_hallucinated = "<b>OnePlus Nord Buds 2r</b> steal deal at ₹999 only!"
    assert validate_critic(copy_hallucinated, deal) is False


# ── 3. STATESTORE UNIFIES BASELINE PRICE AGAINST EFFECTIVE PRICE ───────────────

def test_is_posted_coupon_effective_price_prevents_false_drop_repost(tmp_path):
    """Verify that storing effective_price prevents the next cycle from triggering a fake 15% price drop."""
    db_file = tmp_path / "test_drop_baseline.sqlite3"
    store = StateStore(str(db_file))

    deal_id = "amz-headphone-1"
    canon_id = "amazon:B0HEADPHONE1"

    # Cycle 1 posts item with list_price=1999 and coupon effective_price=1599
    store.mark_posted(
        deal_id=deal_id,
        retailer="Amazon",
        canonical_product_id=canon_id,
        price=1999,
        effective_price=1599,
    )

    # Check that SQLite stored 1599 as the baseline price
    with store._connect() as conn:
        row = conn.execute("SELECT price FROM posted_deals WHERE deal_id = ?", (deal_id,)).fetchone()
        assert row[0] == 1599

    # Cycle 2 sees the same ASIN on another feed at ₹1,599 (post-coupon price)
    # This must NOT trigger a further price drop re-post!
    is_posted_same = store.is_posted(deal_id, canonical_product_id=canon_id, current_price=1599)
    assert is_posted_same is True  # True = already posted, do NOT re-post

    # But if price legitimately drops >= 15% below ₹1,599 (e.g. to ₹1,299 <= 1599 * 0.85 = 1359)
    is_posted_real_drop = store.is_posted(deal_id, canonical_product_id=canon_id, current_price=1299)
    assert is_posted_real_drop is False  # False = allowed to re-post as genuine price drop!


# ── 4. HTML ESCAPING OF SCRAPED AMAZON METADATA ───────────────────────────────

def test_html_escaping_in_metadata_caption():
    """Verify that raw <, >, and & in scraped Amazon metadata are safely HTML escaped in format_caption."""
    deal = Deal(
        id="amz-escape-deal",
        retailer="Amazon",
        title="Fast GaN Charger <65W> & Braided Type-C Cable",
        url="https://www.amazon.in/dp/B0CHARGER",
        current_price="₹1,299",
        original_price="₹2,499",
        discount_percent=48,
        bank_offer="10% Instant Discount on HDFC & ICICI Cards on orders > ₹5,000",
    )

    caption = format_caption(deal)

    # Title escaping: <65W> must become &lt;65W&gt;
    assert "&lt;65W&gt;" in caption
    assert "<65W>" not in caption
    assert "Fast GaN Charger &lt;65W&gt; &amp; Braided" in caption

    # Bank offer escaping: & and > must be escaped
    assert "HDFC &amp; ICICI Cards" in caption
    assert "orders &gt; ₹5,000" in caption
    assert "orders > ₹5,000" not in caption


# ── 5. DETERMINISTIC LLM BACKFILL IN AGENT ─────────────────────────────────────

def test_deterministic_llm_backfill_guarantees_target_post_count():
    """When _ask_llm returns fewer deals than needed, DealAgent deterministically backfills from shortlist."""
    deals = [
        Deal(
            id="deal-1",
            retailer="Amazon",
            title="Sony WH-1000XM4 Wireless Active Noise Cancelling Headphones",
            url="https://amazon.in/dp/D1",
            current_price="₹19,999",
            original_price="₹29,999",
            discount_percent=33,
            category="electronics",
        ),
        Deal(
            id="deal-2",
            retailer="Amazon",
            title="Philips Digital Air Fryer with Rapid Air Technology",
            url="https://amazon.in/dp/D2",
            current_price="₹6,999",
            original_price="₹12,995",
            discount_percent=46,
            category="home",
        ),
        Deal(
            id="deal-3",
            retailer="Amazon",
            title="boAt Rockerz 450 Bluetooth On Ear Headphones",
            url="https://amazon.in/dp/D3",
            current_price="₹1,499",
            original_price="₹3,990",
            discount_percent=62,
            category="electronics",
        ),
    ]

    mock_copywriter = MagicMock()
    mock_copywriter.is_configured.return_value = True

    # LLM picks ONLY 1 deal ("deal-1") even though max_posts is 3
    mock_llm_response = '{"posts":[{"id":"deal-1","why":"flagship headphones","copy":"Sony XM4 at ₹19,999"}]}'
    mock_copywriter.generate_text.return_value = mock_llm_response

    agent = DealAgent(copywriter=mock_copywriter, min_score=30)
    agent.use_llm = True

    plan = agent.plan(deals, recent_categories=[], max_posts=3)

    # Must contain 3 posts (1 from LLM + 2 from deterministic backfill)
    assert len(plan.posts) == 3
    posted_ids = [item.deal.id for item in plan.posts]
    assert "deal-1" in posted_ids
    assert "deal-2" in posted_ids
    assert "deal-3" in posted_ids


# ── 6. OOS SWEEP CAPPED AT 6 AND PRIORITIZES OLDEST ────────────────────────────

def test_sweep_expired_deals_capped_at_six_and_prioritizes_oldest(tmp_path):
    """Verify that sweep_expired_deals checks at most 6 deals and prioritizes deals not checked recently."""
    db_file = tmp_path / "test_sweep_cap.sqlite3"
    store = StateStore(str(db_file))

    # Insert 10 active posted deals
    for i in range(10):
        deal_id = f"amz-sweep-{i}"
        store.mark_posted(
            deal_id=deal_id,
            retailer="Amazon",
            message_id=100 + i,
            price=1000 + i * 50,
        )

    # Mark the first 4 as checked recently
    with store._connect() as conn:
        for i in range(4):
            conn.execute(
                "UPDATE posted_deals SET last_checked_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (f"amz-sweep-{i}",),
            )

    checked_deals = []

    def mock_inspect(url):
        deal_id = url.split("/")[-1]
        checked_deals.append(f"amz-{deal_id}")
        return "in_stock"

    with patch("dealbot.main.inspect_stock", side_effect=mock_inspect):
        sweep_expired_deals(store, dry_run=True, max_checks=6, sleep_sec=0)

    # Exactly 6 deals must have been checked
    assert len(checked_deals) == 6
    # The 6 deals with NULL last_checked_at (sweep-4 through sweep-9) must be checked first!
    assert all(d in checked_deals for d in ["amz-sweep-4", "amz-sweep-5", "amz-sweep-6", "amz-sweep-7", "amz-sweep-8", "amz-sweep-9"])
