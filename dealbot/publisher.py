import html
import logging
import os
import re
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id TEXT,
                    action TEXT,
                    score INTEGER,
                    category TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Auto-migrate older schemas if missing columns
            for col in [
                "chat_id TEXT",
                "message_id INTEGER",
                "caption TEXT",
                "status TEXT DEFAULT 'active'",
                "last_checked_at TIMESTAMP",
                "category TEXT",
                "discount INTEGER",
                "agent_score INTEGER",
                "agent_reason TEXT",
            ]:
                try:
                    conn.execute(f"ALTER TABLE posted_deals ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

    def is_posted(self, deal_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT 1 FROM posted_deals WHERE deal_id = ?", (deal_id,))
            return cur.fetchone() is not None

    def mark_posted(
        self,
        deal_id: str,
        retailer: str,
        chat_id: Optional[str] = None,
        message_id: Optional[int] = None,
        caption: Optional[str] = None,
        category: Optional[str] = None,
        discount: Optional[int] = None,
        agent_score: Optional[int] = None,
        agent_reason: Optional[str] = None,
    ):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO posted_deals (
                    deal_id, retailer, chat_id, message_id, caption, status,
                    category, discount, agent_score, agent_reason
                )
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                ON CONFLICT(deal_id) DO UPDATE SET
                    retailer=excluded.retailer,
                    chat_id=excluded.chat_id,
                    message_id=excluded.message_id,
                    caption=excluded.caption,
                    category=excluded.category,
                    discount=excluded.discount,
                    agent_score=excluded.agent_score,
                    agent_reason=excluded.agent_reason
                """,
                (
                    deal_id,
                    retailer,
                    chat_id,
                    message_id,
                    caption,
                    category,
                    discount,
                    agent_score,
                    agent_reason,
                ),
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

    def get_recent_context(self, limit: int = 8) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT deal_id, retailer, caption, category, discount, posted_at
                FROM posted_deals
                ORDER BY posted_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def record_decisions(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO agent_decisions (deal_id, action, score, category, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (row["deal_id"], row["action"], row.get("score"), row.get("category"), row.get("reason"))
                    for row in rows
                ],
            )
            conn.execute(
                """
                DELETE FROM agent_decisions
                WHERE id < (
                    SELECT MIN(id) FROM (
                        SELECT id FROM agent_decisions ORDER BY id DESC LIMIT 400
                    )
                )
                """
            )

    def mark_expired(self, deal_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE posted_deals SET status = 'expired' WHERE deal_id = ?", (deal_id,))

def _ai_copy_keeps_price(ai_body: str, deal: Deal) -> bool:
    """Reject model copy that dropped the real deal price."""
    if not deal.current_price:
        return True
    digits = re.sub(r"\D", "", deal.current_price)
    if len(digits) < 2:
        return True
    return digits in re.sub(r"\D", "", ai_body or "")


def format_caption(deal: Deal, copywriter: Optional[AICopywriter] = None) -> str:
    retailer = html.escape(deal.retailer or "Store")

    ai_body = None
    if copywriter and copywriter.is_configured():
        ai_body = copywriter.craft_deal_copy(deal)
        if ai_body and not _ai_copy_keeps_price(ai_body, deal):
            logger.info("Agent critic rejected AI copy that dropped the deal price. Using the template.")
            ai_body = None

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
        savings = deal.savings_rupees()
        if savings and savings >= 100:
            price_parts.append(f"✅ <b>You save:</b> ₹{savings:,}")

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

def send_admin_note(text: str, dry_run: bool = True) -> bool:
    """Private briefing for the channel admin. Never posted to the public channel."""
    if dry_run or not text:
        logger.info("[agent briefing]\n%s", text)
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin_id = os.getenv("ADMIN_CHAT_ID", "").strip()
    if not token or token == "replace_me" or not admin_id:
        logger.info("[agent briefing]\n%s", text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": admin_id, "text": text[:3500], "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Admin briefing failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Admin briefing request failed: %s", exc)
        return False


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
