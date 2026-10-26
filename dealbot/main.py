import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from dealbot.publisher import StateStore, send_admin_note, send_telegram_deal
from dealbot.sources import collect_all_deals
from dealbot.ai_copywriter import AICopywriter
from dealbot.agent import DealAgent, classify_category
from dealbot.models import Deal
from dealbot.expiry_checker import check_deal_is_active, mark_telegram_deal_expired
from dealbot.pacing import get_recommended_sleep_seconds

def setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

def sweep_expired_deals(store: StateStore, dry_run: bool = True):
    """Periodically check posted active deals and mark expired/OOS ones on Telegram."""
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

        # Only check Amazon/Flipkart URLs
        if deal_id.startswith("amz-"):
            asin = deal_id.replace("amz-", "")
            url = f"https://www.amazon.in/dp/{asin}"
            is_active = check_deal_is_active(url)
            if not is_active:
                logger.info(f"Deal {deal_id} is OUT OF STOCK. Updating Telegram message...")
                if not dry_run and token:
                    mark_telegram_deal_expired(token, chat_id, msg_id, caption)
                store.mark_expired(deal_id)

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
    copywriter = AICopywriter()
    if copywriter.is_configured():
        logger.info(f"AI Copywriter initialized using model '{copywriter.model}'.")
    else:
        logger.info("AI Copywriter not configured (GEMINI_API_KEY empty). Using standard template.")

    deals = collect_all_deals()
    logger.info(f"Fetched {len(deals)} active deals from sources.")
    fresh = [deal for deal in deals if deal.is_valid() and not store.is_posted(deal.id)]

    plan = None
    if agent_mode:
        agent = DealAgent(copywriter=copywriter)
        plan = agent.plan(fresh, _recent_categories(store), max_posts=max_posts)
        queue = [item.deal for item in plan.posts]
        reasons = {item.deal.id: item for item in plan.posts}
    else:
        queue = fresh[:max_posts]
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
                )
                time.sleep(post_delay)

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
        send_admin_note(plan.summary(), dry_run=dry_run)

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
        time.sleep(sleep_sec)

if __name__ == "__main__":
    main()
