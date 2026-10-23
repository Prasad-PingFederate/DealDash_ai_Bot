import html
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
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
                    chat_id TEXT,
                    message_id INTEGER,
                    caption TEXT,
                    status TEXT DEFAULT 'active',
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked_at TIMESTAMP
                )
            """)
            # Auto-migrate older schemas if missing columns
            for col in ["chat_id TEXT", "message_id INTEGER", "caption TEXT", "status TEXT DEFAULT 'active'", "last_checked_at TIMESTAMP"]:
                try:
                    conn.execute(f"ALTER TABLE posted_deals ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

    def is_posted(self, deal_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT 1 FROM posted_deals WHERE deal_id = ?", (deal_id,))
            return cur.fetchone() is not None

    def mark_posted(self, deal_id: str, retailer: str, chat_id: Optional[str] = None, message_id: Optional[int] = None, caption: Optional[str] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO posted_deals (deal_id, retailer, chat_id, message_id, caption, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT(deal_id) DO UPDATE SET
                    retailer=excluded.retailer,
                    chat_id=excluded.chat_id,
                    message_id=excluded.message_id,
                    caption=excluded.caption
                """,
                (deal_id, retailer, chat_id, message_id, caption)
            )

    def get_recent_active_deals(self, limit: int = 15) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT deal_id, retailer, chat_id, message_id, caption
                FROM posted_deals
                WHERE status = 'active' AND message_id IS NOT NULL
                ORDER BY posted_at DESC LIMIT ?
                """,
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_expired(self, deal_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE posted_deals SET status = 'expired' WHERE deal_id = ?", (deal_id,))

def format_caption(deal: Deal, copywriter: Optional[AICopywriter] = None) -> str:
    retailer = html.escape(deal.retailer or "Store")

    ai_body = None
    if copywriter and copywriter.is_configured():
        ai_body = copywriter.craft_deal_copy(deal)

    if ai_body:
        lines = [ai_body]
    else:
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

def send_telegram_deal(deal: Deal, dry_run: bool = True, copywriter: Optional[AICopywriter] = None) -> Tuple[bool, Optional[int], str]:
    caption = format_caption(deal, copywriter=copywriter)
    if dry_run:
        img_info = f" [Image: {deal.image_url}]" if deal.image_url else ""
        logger.info(f"[DRY RUN] Would post deal '{deal.id}'{img_info}:\n{caption}\n" + "-" * 40)
        return True, 999999, caption

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or token == "replace_me" or not chat_id:
        logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return False, None, caption

    reply_markup = {
        "inline_keyboard": [[
            {"text": f"🛒 Buy Now on {deal.retailer}", "url": deal.url}
        ]]
    }

    # Guard against Telegram length limits (4096 char limit for text)
    if len(caption) > 4000:
        caption = caption[:3950] + "\n..."

    try:
        # If image is present and caption fits in Telegram's 1024 char limit for photos, try sendPhoto
        if deal.image_url and len(caption) <= 1024:
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
                data = resp.json()
                msg_id = data.get("result", {}).get("message_id")
                logger.info(f"Successfully posted deal with photo: {deal.id} (message_id={msg_id})")
                return True, msg_id, caption
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
            data = resp.json()
            msg_id = data.get("result", {}).get("message_id")
            logger.info(f"Successfully posted deal: {deal.id} (message_id={msg_id})")
            return True, msg_id, caption
        else:
            logger.error(f"Telegram API error ({resp.status_code}): {resp.text}")
            return False, None, caption
    except requests.RequestException as e:
        logger.error(f"Telegram request failed: {e}")
        return False, None, caption
