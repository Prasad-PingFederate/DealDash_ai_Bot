import html
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional
import requests
from dealbot.models import Deal
from dealbot.ai_copywriter import AICopywriter

logger = logging.getLogger(__name__)

class StateStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS posted_deals (
                    deal_id TEXT PRIMARY KEY,
                    retailer TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def is_posted(self, deal_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT 1 FROM posted_deals WHERE deal_id = ?", (deal_id,))
            return cur.fetchone() is not None

    def mark_posted(self, deal_id: str, retailer: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO posted_deals (deal_id, retailer) VALUES (?, ?)",
                (deal_id, retailer)
            )

def format_caption(deal: Deal, copywriter: Optional[AICopywriter] = None) -> str:
    retailer = html.escape(deal.retailer or "Store")

    # 1. Try AI-generated copy if copywriter is provided and configured
    ai_body = None
    if copywriter and copywriter.is_configured():
        ai_body = copywriter.craft_deal_copy(deal)

    if ai_body:
        lines = [ai_body]
    else:
        # Standard resilient template fallback
        title = html.escape(deal.title or "Deal")
        lines = [f"🔥 <b>{retailer} Deal Alert!</b>", "", f"🏷️ <b>{title}</b>"]

        if deal.description:
            lines.append(f"<i>{html.escape(deal.description)}</i>")

        price_parts = []
        if deal.current_price:
            price_parts.append(f"💰 <b>Deal Price:</b> {html.escape(deal.current_price)}")
        if deal.original_price:
            price_parts.append(f"❌ <b>MRP:</b> <s>{html.escape(deal.original_price)}</s>")
        if deal.discount_percent:
            price_parts.append(f"⚡ <b>Discount:</b> {deal.discount_percent}% OFF")

        if price_parts:
            lines.append("")
            lines.extend(price_parts)

    lines.append("")
    lines.append(f'🛒 <a href="{html.escape(deal.url or "#")}"><b>Buy Now on {retailer}</b></a>')
    lines.append("")
    if "amazon" in (deal.retailer or "").lower():
        lines.append("<i>#ad Affiliate link. As an Amazon Associate I earn from qualifying purchases.</i>")
    else:
        lines.append("<i>#ad Affiliate link - prices & stock may change quickly.</i>")

    return "\n".join(lines)

# Alias for backwards compatibility and test suite
render_message = format_caption

def send_telegram_deal(deal: Deal, dry_run: bool = True, copywriter: Optional[AICopywriter] = None) -> bool:
    caption = format_caption(deal, copywriter=copywriter)
    if dry_run:
        img_info = f" [Image: {deal.image_url}]" if deal.image_url else ""
        logger.info(f"[DRY RUN] Would post deal '{deal.id}'{img_info}:\n{caption}\n" + "-" * 40)
        return True

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or token == "replace_me" or not chat_id:
        logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return False

    reply_markup = {
        "inline_keyboard": [[
            {"text": f"🛒 Buy Now on {deal.retailer}", "url": deal.url}
        ]]
    }

    try:
        # If image is present, try sendPhoto first
        if deal.image_url:
            endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": deal.image_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            }
            resp = requests.post(endpoint, json=payload, timeout=20)
            if resp.status_code == 200:
                logger.info(f"Successfully posted deal with photo: {deal.id}")
                return True
            else:
                logger.warning(f"sendPhoto failed ({resp.status_code}: {resp.text}). Falling back to text message...")

        # Fallback or text-only sendMessage
        endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": reply_markup,
        }
        resp = requests.post(endpoint, json=payload, timeout=20)
        if resp.status_code == 200:
            logger.info(f"Successfully posted deal: {deal.id}")
            return True
        else:
            logger.error(f"Telegram API error ({resp.status_code}): {resp.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Telegram request failed: {e}")
        return False
