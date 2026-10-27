import logging
import os
import time
from datetime import datetime, timezone, timedelta
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

def sweep_expired_deals(store: StateStore, dry_run: bool = True, max_checks: int = 6, sleep_sec: float = 1.5):
    """Periodically check posted active deals and mark expired/OOS/Price-Spiked ones on Telegram after consecutive confirmation."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    active_deals = store.get_recent_active_deals(limit=max_checks)
    if not active_deals:
        return

    logger = logging.getLogger("dealbot.main")
    for idx, row in enumerate(active_deals[:max_checks]):
        if idx > 0 and sleep_sec > 0:
            time.sleep(sleep_sec)

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
        else:
            store.touch_checked(deal_id)

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


def _deal_richness(d: Deal) -> int:
    score = 0
    if d.image_url and d.image_url.startswith("http"):
        score += 10
    if d.original_price and parse_rupees(d.original_price):
        score += 5
    if d.current_price and parse_rupees(d.current_price):
        score += 5
    if d.discount_percent and d.discount_percent > 0:
        score += 3
    if getattr(d, "rating", None) and d.rating > 0:
        score += 3
    if getattr(d, "review_count", None) and d.review_count > 0:
        score += 2
    if len(d.title or "") > 20:
        score += 2
    return score


def deduplicate_deals_intra_cycle(deals: list) -> list:
    """Deduplicate candidates by canonical_id before Stage 1, keeping the richer metadata candidate
    and aggregating distinct source communities for viral consensus detection."""
    by_canon: dict = {}
    for d in deals:
        canon_id = getattr(d, "canonical_product_id", None) or d.id
        if not canon_id:
            continue
        if canon_id not in by_canon:
            if not getattr(d, "matched_sources", None):
                d.matched_sources = [d.source_platform or d.category or "feed"]
            d.source_count = max(1, len(d.matched_sources))
            by_canon[canon_id] = d
        else:
            existing = by_canon[canon_id]
            # Accumulate distinct source platforms
            sources = set(existing.matched_sources or [])
            sources.update(d.matched_sources or [])
            if getattr(d, "source_platform", None):
                sources.add(d.source_platform)
            if getattr(existing, "source_platform", None):
                sources.add(existing.source_platform)

            if _deal_richness(d) > _deal_richness(existing):
                winner = d
                donor = existing
            else:
                winner = existing
                donor = d

            if not winner.image_url and donor.image_url:
                winner.image_url = donor.image_url
            if not winner.original_price and donor.original_price:
                winner.original_price = donor.original_price
            if not winner.coupon_code and donor.coupon_code:
                winner.coupon_code = donor.coupon_code
            if not winner.bank_offer and donor.bank_offer:
                winner.bank_offer = donor.bank_offer
            if not getattr(winner, "rating", None) and getattr(donor, "rating", None):
                winner.rating = donor.rating
                winner.review_count = donor.review_count

            winner.matched_sources = sorted(list(sources))
            winner.source_count = max(len(winner.matched_sources), 1)
            by_canon[canon_id] = winner
    return list(by_canon.values())


def verify_and_enrich_deal(deal: Deal, store: StateStore, skip_tracker: dict | None = None) -> bool:
    """Check stock and enrich Amazon deals with live price, MRP, ratings, coupons, and images.
    Returns True if valid for posting, False if rejected (OOS, price jumped >20%, or poor rating)."""
    logger = logging.getLogger("dealbot.main")
    status = inspect_stock(deal.url)
    if status == "out_of_stock":
        logger.info(f"Skipping {deal.id}: out of stock or gone.")
        if skip_tracker is not None:
            skip_tracker["oos"] = skip_tracker.get("oos", 0) + 1
        deal._verify_skip_reason = "oos"
        store.record_event(deal.id, "skipped_oos")
        return False
    if status == "in_stock":
        deal.checked = True
        if "amazon." in (deal.url or "").lower():
            enrichment = enrich_amazon_deal(deal.url)
            live_price = enrichment.get("live_price")
            feed_price = parse_rupees(deal.current_price)
            feed_mrp = parse_rupees(deal.original_price)
            is_unverified_source = (
                getattr(deal, "category", "") == "reddit"
                or (deal.id and "reddit" in deal.id.lower())
                or not feed_price
            )
            if live_price and feed_price and not is_unverified_source:
                if live_price > int(feed_price * 1.20):
                    # Smart live price discrepancy: if live_price matches feed MRP,
                    # treat as inconclusive due to datacenter IP showing non-deal price
                    if feed_mrp and abs(live_price - feed_mrp) / max(feed_mrp, 1) < 0.05:
                        logger.info(
                            f"Deal {deal.id}: live price ₹{live_price} matches feed MRP ₹{feed_mrp} "
                            f"(inconclusive check due to datacenter IP). Applying -8 penalty instead of discarding."
                        )
                        deal.score_penalty = getattr(deal, "score_penalty", 0) + 8
                    else:
                        logger.info(
                            f"Skipping {deal.id}: price jumped from ₹{feed_price} to ₹{live_price} (>20% increase)."
                        )
                        if skip_tracker is not None:
                            skip_tracker["jump"] = skip_tracker.get("jump", 0) + 1
                        deal._verify_skip_reason = "jump"
                        store.record_event(deal.id, "skipped_price_jumped")
                        return False
                else:
                    deal.current_price = f"₹{live_price:,}"
            elif live_price and is_unverified_source:
                deal.current_price = f"₹{live_price:,}"

            live_mrp = enrichment.get("mrp")
            if live_mrp:
                deal.original_price = f"₹{live_mrp:,}"

            rating = enrichment.get("rating")
            reviews = enrichment.get("review_count")
            if rating is not None and rating > 0:
                deal.rating = rating
                deal.review_count = reviews
                if rating < 3.6 or (reviews is not None and reviews > 0 and reviews < 15 and rating < 4.0):
                    logger.info(f"Skipping {deal.id}: low rating {rating}★ ({reviews} reviews).")
                    if skip_tracker is not None:
                        skip_tracker["low_rating"] = skip_tracker.get("low_rating", 0) + 1
                    deal._verify_skip_reason = "low_rating"
                    store.record_event(deal.id, "skipped_low_rating")
                    return False
            else:
                deal.rating = None
                deal.review_count = None

            if enrichment.get("coupon") and not deal.coupon_code:
                deal.coupon_code = enrichment["coupon"]
            if enrichment.get("coupon_discount") is not None:
                deal.coupon_discount = enrichment["coupon_discount"]
            if enrichment.get("effective_price") is not None:
                deal.effective_price = enrichment["effective_price"]
            if enrichment.get("bank_offer") and not deal.bank_offer:
                deal.bank_offer = enrichment["bank_offer"]
            if enrichment.get("promo_code"):
                deal.promo_code = enrichment["promo_code"]
            if enrichment.get("bank_discount_amount"):
                deal.bank_discount_amount = enrichment["bank_discount_amount"]
            if enrichment.get("hi_res_image"):
                deal.image_url = enrichment["hi_res_image"]

            eff_price = deal.effective_price if deal.effective_price is not None else live_price
            effective_mrp = live_mrp or feed_mrp
            if effective_mrp and live_price and effective_mrp > live_price > 0:
                deal.discount_percent = int(round(((effective_mrp - live_price) / effective_mrp) * 100))
                deal.effective_discount_pct = int(round(((effective_mrp - eff_price) / effective_mrp) * 100))
            elif enrichment.get("discount_pct") is not None:
                deal.discount_percent = enrichment["discount_pct"]
                deal.effective_discount_pct = enrichment.get("effective_discount_pct", deal.discount_percent)
            elif deal.discount_percent is not None:
                deal.effective_discount_pct = deal.discount_percent

            # Enforce minimum discount threshold after enrichment
            min_disc_env = int(os.getenv("MIN_DISCOUNT_PERCENT", "30"))
            from dealbot.agent import brand_tier
            b_tier = brand_tier(deal.title)
            required_disc = 25 if b_tier == 1 else min_disc_env
            eff_disc = getattr(deal, "effective_discount_pct", None)
            if eff_disc is None:
                eff_disc = deal.discount_percent
            if eff_disc is not None and eff_disc < required_disc:
                logger.info(
                    f"Skipping {deal.id}: effective discount {eff_disc}% is below required {required_disc}% "
                    f"(Tier-{b_tier} brand)."
                )
                if skip_tracker is not None:
                    skip_tracker["low_discount"] = skip_tracker.get("low_discount", 0) + 1
                deal._verify_skip_reason = "low_discount"
                store.record_event(deal.id, "skipped_low_discount")
                return False

            # Record verified price sighting into historical tracker
            canon_id = getattr(deal, "canonical_product_id", None) or deal.id
            verified_price = deal.effective_price if deal.effective_price is not None else live_price
            if verified_price:
                ctx = store.record_price_sighting(canon_id, verified_price)
                deal.price_context = ctx.get("status")
                avg_p = ctx.get("avg_price")
                max_p = ctx.get("max_price")
                if (avg_p and avg_p > verified_price) or (max_p and max_p > verified_price):
                    baseline = int(avg_p if avg_p and avg_p > verified_price else max_p)
                    from dealbot.regions import get_currency_symbol
                    curr_sym = get_currency_symbol()
                    deal.price_trend_text = f"📉 Price Trend: {curr_sym}{baseline:,} (Avg) ➔ {curr_sym}{verified_price:,} (Now!)"
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
        avg_p = ctx.get("avg_price")
        max_p = ctx.get("max_price")
        if (avg_p and avg_p > seen) or (max_p and max_p > seen):
            baseline = int(avg_p if avg_p and avg_p > seen else max_p)
            from dealbot.regions import get_currency_symbol
            curr_sym = get_currency_symbol()
            deal.price_trend_text = f"📉 Price Trend: {curr_sym}{baseline:,} (Avg) ➔ {curr_sym}{seen:,} (Now!)"

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

    # Intra-cycle deduplication by canonical_id before Stage 1 pre-scoring
    fresh = deduplicate_deals_intra_cycle(fresh)

    recent_cats = _recent_categories(store)
    check_stock = (not dry_run) and os.getenv("CHECK_STOCK_BEFORE_POST", "true").lower() != "false"

    # Stage 1: Pre-score all feed candidates fast without network calls
    from dealbot.agent import score_deal
    fresh.sort(key=lambda d: score_deal(d, recent_cats).score, reverse=True)

    # Stage 2: Two-stage funnel with backfill loop
    # Iterate through Stage 1 ranked candidates until either max(max_posts * 2, 6) verified pass
    # OR at most 12 enrichment fetches attempted.
    target_verified = max(max_posts * 2, 6)
    max_fetches = 12

    verified_candidates = []
    enriched_tried = 0
    enriched_ok = 0
    skip_tracker = {"oos": 0, "jump": 0, "low_rating": 0, "low_discount": 0}

    if check_stock:
        for candidate in fresh:
            if len(verified_candidates) >= target_verified or enriched_tried >= max_fetches:
                break
            enriched_tried += 1
            if verify_and_enrich_deal(candidate, store, skip_tracker=skip_tracker):
                verified_candidates.append(candidate)
                enriched_ok += 1
    else:
        verified_candidates = fresh
        enriched_tried = len(verified_candidates)
        enriched_ok = len(verified_candidates)

    # Stage 3: Re-score enriched survivors and pick final posts via DealAgent
    plan = None
    soft_floor_used = False
    if agent_mode:
        agent = DealAgent(copywriter=copywriter)
        plan = agent.plan(verified_candidates, recent_cats, max_posts=max_posts)
        queue = [item.deal for item in plan.posts]
        reasons = {item.deal.id: item for item in plan.posts}
        soft_floor_used = plan.soft_floor_used
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
                post_price = parse_rupees(deal.current_price)
                eff_price = getattr(deal, "effective_price", None)
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
                    effective_price=eff_price,
                )
                time.sleep(post_delay)
                store.record_event(deal.id, "posted", "checked" if deal.checked else "unverified")
        elif not dry_run:
            store.record_event(deal.id, "send_failed")

    # Record last cycle posted count in SQLite metadata
    store.set_meta("last_cycle_posted_count", str(posted_count))
    if posted_count == 0:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.set_meta("last_cycle_empty_at", now_iso)
    store.checkpoint()

    # Update hourly pacing lock ONLY when deals were actually posted
    from dealbot.session import mark_cycle
    mark_cycle(posted_count=posted_count)

    funnel_summary = (
        f"[FUNNEL] Raw: {len(deals)} | Unposted: {len(fresh)} | "
        f"Enriched: {enriched_ok}/{enriched_tried} (OOS:{skip_tracker['oos']}, "
        f"PriceJump:{skip_tracker['jump']}, LowRating:{skip_tracker['low_rating']}, "
        f"LowDisc:{skip_tracker.get('low_discount', 0)}) | "
        f"Selected: {len(queue)} (SoftFloor:{soft_floor_used}) | Posted: {posted_count}"
    )
    logger.info(funnel_summary)

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
        note = f"{plan.summary()}\n\n{funnel_summary}"
        extra = store.engagement_line()
        if extra:
            note = f"{note}\n{extra}"
        send_admin_note(note, dry_run=dry_run)
    else:
        note = funnel_summary
        send_admin_note(note, dry_run=dry_run)

    # Sweep and update previously posted deals that may have gone Out of Stock
    sweep_expired_deals(store, dry_run=dry_run)

    # Automated Nightly Top 5 Live Mega-Deals Digest (runs once per calendar day in 21:00-22:15 IST window)
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    is_digest_window = (now_ist.hour == 21) or (now_ist.hour == 22 and now_ist.minute <= 15)
    today_str = now_ist.strftime("%Y-%m-%d")
    last_digest_date = store.get_meta("last_daily_digest_date")
    if is_digest_window and last_digest_date != today_str:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        from dealbot.publisher import send_top_live_deals_digest
        sent = send_top_live_deals_digest(store, chat_id=chat_id, token=token, dry_run=dry_run)
        if sent:
            store.set_meta("last_daily_digest_date", today_str)
            store.checkpoint()
            logger.info("Sent and pinned nightly Top Live Mega-Deals Digest for %s.", today_str)

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
