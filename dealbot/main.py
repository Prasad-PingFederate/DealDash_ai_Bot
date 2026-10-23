import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from dealbot.publisher import StateStore, send_telegram_deal
from dealbot.sources import collect_all_deals
from dealbot.ai_copywriter import AICopywriter

def setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

def run_once() -> int:
    setup_logging()
    logger = logging.getLogger("dealbot.main")

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    max_posts = int(os.getenv("MAX_POSTS_PER_CYCLE", "5"))
    post_delay = float(os.getenv("POST_DELAY_SECONDS", "2"))

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

    posted_count = 0
    for deal in deals:
        if posted_count >= max_posts:
            break
        if store.is_posted(deal.id):
            continue

        success = send_telegram_deal(deal, dry_run=dry_run, copywriter=copywriter)
        if success:
            posted_count += 1
            if not dry_run:
                store.mark_posted(deal.id, deal.retailer)
                time.sleep(post_delay)

    logger.info(f"Cycle complete. Posted {posted_count} new deal(s) (dry_run={dry_run}).")
    return posted_count

def main():
    load_dotenv()
    continuous = os.getenv("CONTINUOUS", "true").lower() == "true"
    poll_minutes = int(os.getenv("POLL_INTERVAL_MINUTES", "60"))

    if not continuous:
        run_once()
        return

    while True:
        run_once()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
