# dealbot/alerts.py
"""User Price-Drop & Keyword Alerts Engine for DealDash_ai_Bot.

Provides:
1. User commands:
   - /alert <keyword> <max_price> (e.g. /alert iphone 60000)
   - /myalerts (lists active alerts for the user)
   - /delalert <id> (removes an alert by ID)
2. Alert matching & deduplication:
   - Matches deals where all keyword tokens appear in deal title and price <= max_price.
   - Deduplicates deliveries so a user is not alerted twice for the same deal at the same price.
3. Instant DM dispatch with dedicated SubID:
   - SubID: bot_alert_<category>_<dealId>
   - Primary CTA: '🛒 Buy Now — ₹<price> (<discount>% OFF)'
   - Secondary CTA: '📊 Price History & Alternatives' (linking to BharatBazaro)
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from dealbot.monetizer import build_subid, monetize_url, slugify

logger = logging.getLogger("dealbot.alerts")

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "state.sqlite3"


class AlertStore:
    """Manages alert storage and delivery deduplication in SQLite."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        self.db_path = Path(db_path or os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    max_price REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_alerts_user ON user_alerts (user_id, is_active)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_alert_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER,
                    user_id TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    subid TEXT,
                    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, canonical_id, price)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_deliv ON user_alert_deliveries (user_id, canonical_id)")

    def add_alert(
        self,
        user_id: str,
        chat_id: str,
        keyword: str,
        max_price: float,
    ) -> Tuple[bool, str, Optional[int]]:
        """Register a new price-drop & keyword alert for a user."""
        clean_kw = " ".join(keyword.strip().lower().split())
        if not clean_kw or len(clean_kw) < 2:
            return False, "Keyword must be at least 2 characters long.", None

        if max_price <= 0:
            return False, "Max price must be greater than ₹0.", None

        with sqlite3.connect(self.db_path) as conn:
            # Check for existing duplicate active alert
            cur = conn.execute(
                """
                SELECT id FROM user_alerts
                WHERE user_id = ? AND keyword = ? AND max_price = ? AND is_active = 1
                """,
                (str(user_id), clean_kw, float(max_price)),
            )
            row = cur.fetchone()
            if row:
                return False, f"Alert already exists (ID: #{row[0]}).", row[0]

            cur = conn.execute(
                """
                INSERT INTO user_alerts (user_id, chat_id, keyword, max_price, is_active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (str(user_id), str(chat_id), clean_kw, float(max_price)),
            )
            alert_id = cur.lastrowid
            return True, f"Alert #{alert_id} activated for '{clean_kw}' up to ₹{int(max_price):,}!", alert_id

    def list_alerts(self, user_id: str) -> List[Dict[str, Any]]:
        """Retrieve all active alerts for a user."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                SELECT id, keyword, max_price, created_at
                FROM user_alerts
                WHERE user_id = ? AND is_active = 1
                ORDER BY created_at DESC
                """,
                (str(user_id),),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "keyword": r[1],
                    "max_price": r[2],
                    "created_at": r[3],
                }
                for r in rows
            ]

    def delete_alert(self, user_id: str, alert_id: int) -> Tuple[bool, str]:
        """Deactivate an alert by ID owned by user_id."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE user_alerts SET is_active = 0
                WHERE id = ? AND user_id = ?
                """,
                (int(alert_id), str(user_id)),
            )
            if cur.rowcount > 0:
                return True, f"Alert #{alert_id} deleted."
            return False, f"Alert #{alert_id} not found."

    def is_already_delivered(self, user_id: str, canonical_id: str, price: float) -> bool:
        """Check if user has already received an alert for this deal at this price."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                SELECT id FROM user_alert_deliveries
                WHERE user_id = ? AND canonical_id = ? AND price = ?
                """,
                (str(user_id), str(canonical_id), float(price)),
            )
            return cur.fetchone() is not None

    def record_delivery(
        self,
        alert_id: Optional[int],
        user_id: str,
        canonical_id: str,
        price: float,
        subid: str,
    ) -> None:
        """Record an alert delivery to prevent spamming."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_alert_deliveries (alert_id, user_id, canonical_id, price, subid)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (alert_id, str(user_id), str(canonical_id), float(price), str(subid)),
                )
        except Exception as e:
            logger.debug("Failed to record alert delivery: %s", e)

    def get_matching_alerts(self, title: str, price: float) -> List[Dict[str, Any]]:
        """Find all active alerts matching title tokens where deal_price <= max_price."""
        clean_title = (title or "").lower()
        title_tokens = set(re.findall(r"\w+", clean_title))

        matched = []
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                SELECT id, user_id, chat_id, keyword, max_price
                FROM user_alerts
                WHERE is_active = 1 AND max_price >= ?
                """,
                (float(price),),
            )
            for row in cur.fetchall():
                alert_id, u_id, c_id, kw, max_p = row
                kw_tokens = set(re.findall(r"\w+", kw.lower()))
                # Match if all keyword tokens appear in the deal title
                if kw_tokens and kw_tokens.issubset(title_tokens):
                    matched.append({
                        "alert_id": alert_id,
                        "user_id": u_id,
                        "chat_id": c_id,
                        "keyword": kw,
                        "max_price": max_p,
                    })
        return matched


def handle_user_command(
    text: str,
    user_id: str,
    chat_id: str,
    store: Optional[AlertStore] = None,
) -> Optional[str]:
    """Parse and execute Telegram user alert commands (/alert, /myalerts, /delalert)."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None

    parts = raw.split()
    cmd = parts[0].lower().split("@")[0]
    alert_store = store or AlertStore()

    # 0. /start [track_<category>_<price>_<kw>]
    if cmd == "/start":
        return handle_start_command(text, user_id=user_id, chat_id=chat_id, store=alert_store)

    # 1. /alert <keyword> <max_price>
    if cmd == "/alert":
        if len(parts) < 3:
            return (
                "<b>🔔 Set a Price-Drop Alert:</b>\n\n"
                "Usage: <code>/alert &lt;product_keyword&gt; &lt;max_price&gt;</code>\n\n"
                "<b>Examples:</b>\n"
                "• <code>/alert iphone 15 55000</code>\n"
                "• <code>/alert sony xm5 20000</code>\n"
                "• <code>/alert air fryer 3000</code>\n\n"
                "We will DM you the instant a verified deal hits your target price!"
            )
        try:
            max_price = float(parts[-1].replace(",", "").replace("₹", ""))
            keyword = " ".join(parts[1:-1])
            success, msg, _ = alert_store.add_alert(user_id, chat_id, keyword, max_price)
            return f"✅ {msg}" if success else f"⚠️ {msg}"
        except ValueError:
            return "❌ Invalid price. Example: <code>/alert iphone 15 55000</code>"

    # 2. /myalerts
    if cmd == "/myalerts":
        alerts = alert_store.list_alerts(user_id)
        if not alerts:
            return (
                "📭 <b>You have no active alerts.</b>\n\n"
                "Set one with <code>/alert &lt;keyword&gt; &lt;max_price&gt;</code>\n"
                "e.g. <code>/alert macbook air 65000</code>"
            )
        lines = ["<b>🔔 Your Active Price-Drop Alerts:</b>\n"]
        for a in alerts:
            lines.append(f"• <b>#{a['id']}</b>: <i>{html.escape(a['keyword'])}</i> up to <b>₹{int(a['max_price']):,}</b>")
        lines.append("\nTo delete an alert, send: <code>/delalert &lt;id&gt;</code>")
        return "\n".join(lines)

    # 3. /delalert <id>
    if cmd == "/delalert":
        if len(parts) < 2:
            return "Usage: <code>/delalert &lt;id&gt;</code>\n(Find ID via <code>/myalerts</code>)"
        try:
            alert_id = int(parts[1].replace("#", ""))
            success, msg = alert_store.delete_alert(user_id, alert_id)
            return f"🗑️ {msg}" if success else f"❌ {msg}"
        except ValueError:
            return "❌ Invalid alert ID. Example: <code>/delalert 3</code>"

    return None


def handle_alert_command(user_id: str, chat_id: str, args: str, store: Optional[AlertStore] = None) -> str:
    """Convenience helper for /alert command execution."""
    res = handle_user_command(f"/alert {args}".strip(), user_id, chat_id, store=store)
    return (res[0] if isinstance(res, tuple) else res) or ""


def handle_myalerts_command(user_id: str, store: Optional[AlertStore] = None) -> str:
    """Convenience helper for /myalerts command execution."""
    res = handle_user_command("/myalerts", user_id, user_id, store=store)
    return (res[0] if isinstance(res, tuple) else res) or ""


def handle_delalert_command(user_id: str, alert_id: str, store: Optional[AlertStore] = None) -> str:
    """Convenience helper for /delalert command execution."""
    res = handle_user_command(f"/delalert {alert_id}".strip(), user_id, user_id, store=store)
    return (res[0] if isinstance(res, tuple) else res) or ""


def get_top_category_deals(category: str, limit: int = 2) -> List[Dict[str, Any]]:
    """Retrieve top active high-expected-profit deals in category for onboarding."""
    json_path = ROOT_DIR / "data" / "live_deals.json"
    matched: List[Dict[str, Any]] = []
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as jf:
                all_deals = json.load(jf)
                cat_clean = (category or "").lower().strip()
                for d in all_deals:
                    if d.get("is_expired") or d.get("status") == "expired":
                        continue
                    deal_cat = str(d.get("category") or "").lower()
                    if cat_clean and cat_clean in deal_cat:
                        matched.append(d)
                # If fewer than limit, fallback to any active deals
                if len(matched) < limit:
                    for d in all_deals:
                        if not (d.get("is_expired") or d.get("status") == "expired") and d not in matched:
                            matched.append(d)
                            if len(matched) >= limit:
                                break
        except Exception as e:
            logger.debug("Failed loading live_deals.json for category deals: %s", e)
    return matched[:limit]


def handle_start_command(
    text: str,
    user_id: str,
    chat_id: str,
    store: Optional[AlertStore] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Handle /start command, including 1-click web deep-link onboarding payload:
    Format: /start track_<category>_<targetPrice>_<encodedKeyword>
    """
    parts = (text or "").strip().split()
    alert_store = store or AlertStore()

    if len(parts) > 1 and parts[1].startswith("track_"):
        payload = parts[1]
        subparts = payload.split("_", 3)
        if len(subparts) >= 4:
            category = subparts[1].lower().strip()
            try:
                target_price = float(subparts[2].replace(",", "").replace("₹", ""))
            except ValueError:
                target_price = 0.0
            keyword = subparts[3].replace("-", " ").replace("+", " ").strip()

            if keyword and target_price > 0:
                alert_store.add_alert(user_id=user_id, chat_id=chat_id, keyword=keyword, max_price=target_price)
                top_deals = get_top_category_deals(category=category, limit=2)

                lines = [
                    "🔔 <b>PRICE-DROP TRACKER ACTIVATED!</b>\n",
                    f"We are monitoring <b>{html.escape(keyword.title())}</b> for you.",
                    f"🎯 <b>Your Target Price:</b> <b>₹{int(target_price):,}</b>",
                    "<i>You will receive an instant Telegram DM the second the price hits your target!</i>\n",
                ]

                buttons = []
                if top_deals:
                    lines.append(f"🔥 <b>Top Active Deals in {category.title()} Right Now:</b>")
                    for idx, d in enumerate(top_deals, start=1):
                        d_id = str(d.get("id") or d.get("asin") or f"deal{idx}")
                        d_title = str(d.get("title") or "Deal")[:50]
                        d_price = int(d.get("effective_price") or d.get("current_price") or d.get("price") or 0)
                        d_disc = int(d.get("discount_pct") or d.get("discount_percent") or 0)
                        raw_url = str(d.get("affiliate_url") or d.get("url") or f"https://www.amazon.in/dp/{d_id}")

                        # SubID: bot_onboard_<category>_<dealId>
                        cat_slug = slugify(category)[:8]
                        did_slug = slugify(d_id.replace("amz-", ""))[:16]
                        onboard_subid = f"bot_onboard_{cat_slug}_{did_slug}"

                        monetized_res = monetize_url(raw_url, channel="onboard", category=category, deal_id=d_id, allow_unshorten=False)
                        m_url = monetized_res.monetized_url
                        if "ascsubtag=" in m_url:
                            m_url = re.sub(r"ascsubtag=[^&]+", f"ascsubtag={onboard_subid}", m_url)
                        elif "?" in m_url:
                            m_url += f"&ascsubtag={onboard_subid}"

                        lines.append(f"{idx}. <a href=\"{m_url}\"><b>{html.escape(d_title)}</b></a> — <b>₹{d_price:,}</b> ({d_disc}% OFF)")
                        buttons.append([{"text": f"🛒 Buy {d_title[:24]} @ ₹{d_price:,}", "url": m_url}])

                lines.append(f"\n🌐 <a href=\"https://bharatbazaro.com/{category}.html\"><b>Browse All {category.title()} Deals on BharatBazaro →</b></a>")
                buttons.append([{"text": "🔔 View My Active Alerts (/myalerts)", "callback_data": "myalerts"}])

                return "\n".join(lines), {"inline_keyboard": buttons}

    # Default /start message
    greeting = (
        "⚡ <b>Welcome to DealDash AI!</b>\n\n"
        "24/7 AI-Powered Deal Radar tracking loot deals, price drops & hidden Amazon/Flipkart coupons.\n\n"
        "<b>Available Commands:</b>\n"
        "• <code>/alert &lt;keyword&gt; &lt;max_price&gt;</code> — Set price-drop alert (e.g. <code>/alert iphone 16 55000</code>)\n"
        "• <code>/myalerts</code> — View your active tracking alerts\n"
        "• <code>/delalert &lt;id&gt;</code> — Delete an active alert\n\n"
        "🌐 <a href=\"https://bharatbazaro.com\"><b>Explore All Live Deals on BharatBazaro.com</b></a>"
    )
    return greeting, None



def match_and_dispatch_alerts(
    deal_or_dict: Any,
    store: Optional[AlertStore] = None,
    token: Optional[str] = None,
    dry_run: bool = True,
) -> List[Dict[str, Any]]:
    """Match newly ranked deal against user alerts and dispatch instant DMs.

    Returns list of dispatched alerts with details.
    """
    raw: Dict[str, Any] = {}
    if hasattr(deal_or_dict, "__dict__"):
        raw = {k: v for k, v in deal_or_dict.__dict__.items() if not k.startswith("_")}
    elif isinstance(deal_or_dict, dict):
        raw = dict(deal_or_dict)

    canonical_id = str(raw.get("id") or raw.get("asin") or raw.get("canonical_id") or "deal").strip()
    title = str(raw.get("title") or "")
    price = float(raw.get("current_price") or raw.get("effective_price") or raw.get("price") or 0.0)
    mrp = float(raw.get("original_price") or raw.get("mrp") or price)
    discount = int(raw.get("discount_percent") or raw.get("discount_pct") or 0)
    if mrp > price > 0 and not discount:
        discount = int(round((mrp - price) / mrp * 100))

    category = str(raw.get("category") or "general")
    deal_url = str(raw.get("url") or raw.get("affiliate_url") or "")
    web_url = str(raw.get("canonical_web_url") or "")
    if not web_url and canonical_id:
        clean_asin = canonical_id.replace("amz-", "").replace("amazon:", "")
        web_url = f"https://bharatbazaro.com/deal/{clean_asin}.html"

    alert_store = store or AlertStore()
    matches = alert_store.get_matching_alerts(title, price)
    if not matches:
        return []

    dispatched = []
    bot_token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    for m in matches:
        user_id = str(m["user_id"])
        chat_id = str(m["chat_id"])
        alert_id = m["alert_id"]

        # Deduplication check
        if alert_store.is_already_delivered(user_id, canonical_id, price):
            logger.debug("Alert already delivered to user %s for deal %s @ ₹%s", user_id, canonical_id, price)
            continue

        # Generate alert-specific SubID: bot_alert_<category>_<dealId>
        cat_slug = slugify(category)[:8]
        did_slug = slugify(canonical_id.replace("amz-", ""))[:16]
        alert_subid = f"bot_alert_{cat_slug}_{did_slug}"

        # Monetize with alert SubID
        monetized_res = monetize_url(
            deal_url,
            channel="alert",
            category=category,
            deal_id=canonical_id,
            allow_unshorten=False,
        )
        monetized_buy_url = monetized_res.monetized_url
        if "ascsubtag=" in monetized_buy_url:
            monetized_buy_url = re.sub(r"ascsubtag=[^&]+", f"ascsubtag={alert_subid}", monetized_buy_url)
        elif "?" in monetized_buy_url:
            monetized_buy_url += f"&ascsubtag={alert_subid}"

        # Build notification caption
        caption = (
            f"🔔 <b>PRICE-DROP ALERT MATCHED!</b>\n\n"
            f"<b>{html.escape(title[:160])}</b>\n\n"
            f"🎯 <b>Your Target:</b> ₹{int(m['max_price']):,}\n"
            f"🔥 <b>Current Deal Price:</b> <b>₹{int(price):,}</b> (<s>₹{int(mrp):,}</s>, {discount}% OFF)\n\n"
            f"<i>Price dropped just now! Limited stock at this price.</i>"
        )

        # Build 2 Inline CTA buttons
        inline_keyboard = [
            [
                {
                    "text": f"🛒 Buy Now — ₹{int(price):,} ({discount}% OFF)",
                    "url": monetized_buy_url,
                }
            ],
            [
                {
                    "text": "📊 Price History & Alternatives",
                    "url": web_url,
                }
            ],
        ]

        if not dry_run and bot_token and bot_token != "replace_me":
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": caption,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                        "reply_markup": {"inline_keyboard": inline_keyboard},
                    },
                    timeout=8,
                )
                if resp.status_code == 200:
                    logger.info("Alert DM sent to user %s for deal %s", user_id, canonical_id)
                else:
                    logger.warning("Failed to send alert DM (%s): %s", resp.status_code, resp.text[:120])
            except requests.RequestException as e:
                logger.warning("Alert dispatch request failed for user %s: %s", user_id, e)

        alert_store.record_delivery(alert_id, user_id, canonical_id, price, alert_subid)
        dispatched.append({
            "alert_id": alert_id,
            "user_id": user_id,
            "canonical_id": canonical_id,
            "price": price,
            "subid": alert_subid,
            "buy_url": monetized_buy_url,
            "web_url": web_url,
        })

    return dispatched
