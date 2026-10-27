import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from dealbot.publisher import StateStore, price_note_for, send_admin_note, send_telegram_deal
from dealbot.sources import collect_all_deals
from dealbot.ai_copywriter import AICopywriter
from dealbot.agent import DealAgent, classify_category
from dealbot.models import Deal, parse_rupees
from dealbot.expiry_checker import (
    check_deal_is_active,
    inspect_stock,
    mark_telegram_deal_expired,
    enrich_amazon_deal,
    mark_telegram_deal_price_spike,
)
from dealbot.pacing import get_recommended_sleep_seconds
from dealbot.listen import drain_updates, listen

def setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

def sweep_expired_deals(store: StateStore, dry_run: bool = True):
    """Periodically check posted active deals and mark expired/OOS/Price-Spiked ones on Telegram after consecutive confirmation."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    active_deals = store.get_recent_active_deals(limit=10)
    if not active_deals:
        return

    logger = logging.getLogger("dealbot.main")
    for row in active_deals:
        deal_id = row["deal_id"]
        chat_id = row["chat_id"]
        msg_id = row["message_id"]
        caption = row.get("caption") or ""
        posted_price = row.get("price")

        url = None
        if deal_id.startswith("amz-"):
            asin = deal_id.replace("amz-", "")
            url = f"https://www.amazon.in/dp/{asin}"
        elif deal_id.startswith("fk-"):
            pid = deal_id.replace("fk-", "")
            url = f"https://www.flipkart.com/p/itm?pid={pid}"

        if not url:
            continue

        stock_status = inspect_stock(url)
        if stock_status == "out_of_stock":
            consecutive = store.increment_oos_count(deal_id)
            # Require 2 consecutive checks to prevent transient page glitches
            if consecutive >= 2:
                logger.info(f"Deal {deal_id} confirmed OUT OF STOCK ({consecutive} consecutive checks). Updating Telegram message...")
                if not dry_run and token:
                    mark_telegram_deal_expired(token, chat_id, msg_id, caption)
                store.mark_expired(deal_id)
            else:
                logger.info(f"Deal {deal_id} first OOS signal detected. Awaiting 2nd confirmation before editing Telegram.")
        elif stock_status == "in_stock":
            store.reset_oos_count(deal_id)
            # In-Stock Price Spike detection: if effective price increased >= 25% while item remains in stock
            if "amazon." in url.lower():
                enrichment = enrich_amazon_deal(url)
                live_price = enrichment.get("live_price")
                live_effective = enrichment.get("effective_price") or live_price
                if live_effective and posted_price and posted_price > 0 and live_effective >= int(posted_price * 1.25):
                    spikes = store.increment_spike_count(deal_id)
                    if spikes >= 2:
                        logger.info(
                            f"Deal {deal_id} confirmed PRICE SPIKE from ₹{posted_price:,} to ₹{live_effective:,} "
                            f"({spikes} consecutive checks). Updating Telegram message..."
                        )
                        if not dry_run and token:
                            mark_telegram_deal_price_spike(token, chat_id, msg_id, caption, live_effective)
                        store.mark_expired(deal_id)
                    else:
                        logger.info(
                            f"Deal {deal_id} first price spike signal (₹{posted_price:,} -> ₹{live_effective:,}). "
                            f"Awaiting 2nd confirmation."
                        )
                else:
                    store.reset_spike_count(deal_id)

def _recent_categories(store: StateStore) -> list:
    categories = []
    for row in store.get_recent_context(limit=8):
        category = (row.get("category") or "").strip().lower()
        if not category and row.get("caption"):
            category = classify_category(
                Deal(id=row["deal_id"], retailer=row.get("retailer") or "Amazon", title=row["caption"], url="https://example.com")
            )
        if category:
            categories.append(category)
    return categories


def verify_and_enrich_deal(deal: Deal, store: StateStore) -> bool:
    """Check stock and enrich Amazon deals with live price, MRP, ratings, coupons, and images.
    Returns True if valid for posting, False if rejected (OOS, price jumped >20%, or poor rating)."""
    logger = logging.getLogger("dealbot.main")
    status = inspect_stock(deal.url)
    if status == "out_of_stock":
        logger.info(f"Skipping {deal.id}: out of stock or gone.")
        store.record_event(deal.id, "skipped_oos")
        return False
    if status == "in_stock":
        deal.checked = True
        if "amazon." in (deal.url or "").lower():
            enrichment = enrich_amazon_deal(deal.url)
            live_price = enrichment.get("live_price")
            feed_price = parse_rupees(deal.current_price)
            is_unverified_source = (
                getattr(deal, "category", "") == "reddit"
                or (deal.id and "reddit" in deal.id.lower())
                or not feed_price
            )
            if live_price and feed_price and not is_unverified_source:
                if live_price > int(feed_price * 1.20):
                    logger.info(
                        f"Skipping {deal.id}: price jumped from ₹{feed_price} to ₹{live_price} (>20% increase)."
                    )
                    store.record_event(deal.id, "skipped_price_jumped")
                    return False
            if live_price:
                deal.current_price = f"₹{live_price:,}"

            live_mrp = enrichment.get("mrp")
            if live_mrp and not deal.original_price:
                deal.original_price = f"₹{live_mrp:,}"

            rating = enrichment.get("rating")
            reviews = enrichment.get("review_count")
            if rating is not None:
                deal.rating = rating
                deal.review_count = reviews
                if rating < 3.6 or (reviews is not None and reviews < 15 and rating < 4.0):
                    logger.info(f"Skipping {deal.id}: low rating {rating}★ ({reviews} reviews).")
                    store.record_event(deal.id, "skipped_low_rating")
                    return False

            if enrichment.get("coupon") and not deal.coupon_code:
                deal.coupon_code = enrichment["coupon"]
            if enrichment.get("coupon_discount") is not None:
                deal.coupon_discount = enrichment["coupon_discount"]
            if enrichment.get("effective_price") is not None:
                deal.effective_price = enrichment["effective_price"]
            if enrichment.get("bank_offer") and not deal.bank_offer:
                deal.bank_offer = enrichment["bank_offer"]
            if enrichment.get("hi_res_image"):
                deal.image_url = enrichment["hi_res_image"]

            # Record verified price sighting into historical tracker
            canon_id = getattr(deal, "canonical_product_id", None) or deal.id
            verified_price = deal.effective_price if deal.effective_price is not None else live_price
            if verified_price:
                ctx = store.record_price_sighting(canon_id, verified_price)
                deal.price_context = ctx.get("status")
        return True
    else:
        store.record_event(deal.id, "stock_unknown")
        return True


def run_once() -> int:
    setup_logging()
    logger = logging.getLogger("dealbot.main")

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    max_posts = int(os.getenv("MAX_POSTS_PER_CYCLE", "5"))
    post_delay = float(os.getenv("POST_DELAY_SECONDS", "2"))
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    agent_mode = os.getenv("AGENT_MODE", "true").lower() != "false"

    project_root = Path(__file__).resolve().parent.parent
    db_env = os.getenv("DATABASE_PATH", "data/state.sqlite3")
    db_path = Path(db_env)
    if not db_path.is_absolute():
        db_path = project_root / db_path

    store = StateStore(str(db_path))
    if (
        not dry_run
        and os.getenv("LISTEN_FOR_ENGAGEMENT", "false").lower() == "true"
    ):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        caught = drain_updates(store, token)
        if caught:
            logger.info("Recorded %s Telegram engagement update(s).", caught)
    copywriter = AICopywriter()
    if copywriter.is_configured():
        logger.info("AI Copywriter ready: %s.", copywriter.describe())
    else:
        logger.info("AI Copywriter not configured. Using the caption template.")

    deals = collect_all_deals()
    logger.info(f"Fetched {len(deals)} active deals from sources.")
    for deal in deals:
        seen = parse_rupees(deal.current_price)
        if seen is None:
            continue
        previous, lowest_before = store.note_price(deal.id, seen)
        deal.price_note = price_note_for(previous, lowest_before, seen)
        canon_id = getattr(deal, "canonical_product_id", None) or deal.id
        ctx = store.get_price_context(canon_id, seen)
        deal.price_context = ctx.get("status")

    fresh = []
    for deal in deals:
        if not deal.is_valid():
            continue
        price_num = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
        canon_id = getattr(deal, "canonical_product_id", None) or deal.id
        if not store.is_posted(deal.id, canonical_product_id=canon_id, current_price=price_num):
            if store.is_price_drop(deal.id, canonical_product_id=canon_id, current_price=price_num):
                deal.is_further_price_drop = True
            fresh.append(deal)

    recent_cats = _recent_categories(store)
    check_stock = (not dry_run) and os.getenv("CHECK_STOCK_BEFORE_POST", "true").lower() != "false"

    # Stage 1: Pre-score all feed candidates fast without network calls
    from dealbot.agent import score_deal
    fresh.sort(key=lambda d: score_deal(d, recent_cats).score, reverse=True)

    # Stage 2: Two-stage funnel with backfill - enrich top candidates before final selection
    top_n = max(max_posts * 3, 9)
    to_enrich = fresh[:top_n]
    remaining_fresh = fresh[top_n:]

    verified_candidates = []
    if check_stock:
        for candidate in to_enrich:
            if verify_and_enrich_deal(candidate, store):
                verified_candidates.append(candidate)
        # If any candidates discarded, backfill from remaining_fresh
        backfill_idx = 0
        while len(verified_candidates) < top_n and backfill_idx < len(remaining_fresh):
            candidate = remaining_fresh[backfill_idx]
            backfill_idx += 1
            if verify_and_enrich_deal(candidate, store):
                verified_candidates.append(candidate)
    else:
        verified_candidates = fresh

    # Stage 3: Re-score enriched survivors and pick final posts via DealAgent
    plan = None
    if agent_mode:
        agent = DealAgent(copywriter=copywriter)
        plan = agent.plan(verified_candidates, recent_cats, max_posts=max_posts)
        queue = [item.deal for item in plan.posts]
        reasons = {item.deal.id: item for item in plan.posts}
    else:
        queue = verified_candidates[:max_posts]
        reasons = {}

    posted_count = 0
    for deal in queue:
        if posted_count >= max_posts:
            break
        success, msg_id, caption = send_telegram_deal(deal, dry_run=dry_run, copywriter=copywriter)
        if success:
            posted_count += 1
            if not dry_run:
                picked = reasons.get(deal.id)
                post_price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
                store.mark_posted(
                    deal.id,
                    deal.retailer,
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=caption,
                    category=picked.category if picked else classify_category(deal),
                    discount=deal.discount_percent,
                    agent_score=picked.score if picked else None,
                    agent_reason=picked.reason if picked else None,
                    canonical_product_id=getattr(deal, "canonical_product_id", None),
                    price=post_price,
                )
                time.sleep(post_delay)
                store.record_event(deal.id, "posted", "checked" if deal.checked else "unverified")
        elif not dry_run:
            store.record_event(deal.id, "send_failed")

    if plan is not None:
        decision_rows = [
            {
                "deal_id": item.deal.id,
                "action": "post" if item in plan.posts else "skip",
                "score": item.score,
                "category": item.category,
                "reason": item.reason,
            }
            for item in [*plan.posts, *plan.skips]
        ]
        if not dry_run:
            store.record_decisions(decision_rows)
        note = plan.summary()
        extra = store.engagement_line()
        if extra:
            note = f"{note}\n{extra}"
        send_admin_note(note, dry_run=dry_run)

    # Sweep and update previously posted deals that may have gone Out of Stock
    sweep_expired_deals(store, dry_run=dry_run)

    logger.info(f"Cycle complete. Posted {posted_count} new deal(s) (dry_run={dry_run}, agent={agent_mode}).")
    return posted_count

def main():
    load_dotenv()
    continuous = os.getenv("CONTINUOUS", "true").lower() == "true"
    poll_minutes = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))

    if not continuous:
        run_once()
        return

    while True:
        run_once()
        sleep_sec = get_recommended_sleep_seconds(poll_minutes=poll_minutes)
        if os.getenv("LISTEN_FOR_ENGAGEMENT", "false").lower() == "true":
            listen(sleep_sec)
        else:
            time.sleep(sleep_sec)

if __name__ == "__main__":
    main()
