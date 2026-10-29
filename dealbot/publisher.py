import html
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
import requests
from dealbot.models import Deal, parse_rupees
from dealbot.ai_copywriter import AICopywriter

logger = logging.getLogger(__name__)

class StateStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self):
        with self._connect() as conn:
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_seen (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id TEXT,
                    outcome TEXT,
                    detail TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS engagement (
                    deal_id TEXT PRIMARY KEY,
                    worth_taps INTEGER DEFAULT 0,
                    reaction_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_sightings (
                    canonical_id TEXT PRIMARY KEY,
                    min_price INTEGER NOT NULL,
                    max_price INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    last_price INTEGER NOT NULL,
                    sighting_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                "canonical_product_id TEXT",
                "price INTEGER",
                "consecutive_oos_count INTEGER DEFAULT 0",
                "consecutive_spike_count INTEGER DEFAULT 0",
            ]:
                try:
                    conn.execute(f"ALTER TABLE posted_deals ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

    def checkpoint(self) -> None:
        """
        Execute a deterministic WAL checkpoint to flush all WAL frames into the main database file.
        This prevents data loss when GitHub Actions runners are cancelled or stopped.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=15.0) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                logger.debug("SQLite WAL checkpoint completed (TRUNCATE).")
        except sqlite3.OperationalError as e:
            logger.warning(f"SQLite WAL checkpoint failed: {e}")

    def checkpoint_and_close(self) -> None:
        self.checkpoint()

    def is_posted(
        self,
        deal_id: str,
        canonical_product_id: Optional[str] = None,
        current_price: Optional[int] = None,
        original_price: Optional[int] = None,
        window_hours: int = 48,
        min_cooldown_hours: int = 0,
    ) -> bool:
        """
        Check if a deal or canonical product ID has been posted within window_hours.
        Allows re-triggering ONLY if:
        1. Valid current_price > 0 (strictly rejects <= 0 or None).
        2. Not an anomalous price (e.g. current_price < 50 when original_price > 2000).
        3. At least min_cooldown_hours (default 2 hours) have elapsed since the previous post.
        4. The new numeric price is >= 15% lower than the previously posted price.
        """
        with self._connect() as conn:
            # 1. Check direct match on deal_id
            cur = conn.execute(
                """
                SELECT deal_id, price, posted_at, datetime('now', ?) as cutoff, datetime('now', ?) as cooldown_cutoff
                FROM posted_deals
                WHERE deal_id = ?
                ORDER BY posted_at DESC LIMIT 1
                """,
                (f"-{window_hours} hours", f"-{min_cooldown_hours} hours", deal_id),
            )
            row = cur.fetchone()

            # 2. Check canonical_product_id within deduplication window
            if not row and canonical_product_id:
                cur = conn.execute(
                    """
                    SELECT deal_id, price, posted_at, datetime('now', ?) as cutoff, datetime('now', ?) as cooldown_cutoff
                    FROM posted_deals
                    WHERE canonical_product_id = ? AND posted_at >= datetime('now', ?)
                    ORDER BY posted_at DESC LIMIT 1
                    """,
                    (f"-{window_hours} hours", f"-{min_cooldown_hours} hours", canonical_product_id, f"-{window_hours} hours"),
                )
                row = cur.fetchone()

            if not row:
                return False

            prev_deal_id, prev_price, posted_at, cutoff, cooldown_cutoff = row
            # If posted older than window, allow re-posting
            if posted_at and cutoff and posted_at < cutoff:
                return False

            # Price-Drop Exception validation
            # 1. Current price must be valid positive integer
            if current_price is None or current_price <= 0:
                return True

            # 2. Reject anomalous price glitches (e.g. current_price < 50 when original/prev_price > 2000)
            if original_price and original_price > 2000 and current_price < 50:
                logger.warning(
                    f"Anomalous glitched price detected for {canonical_product_id or deal_id}: "
                    f"₹{current_price} with MRP ₹{original_price}. Disallowing price-drop re-post."
                )
                return True
            if prev_price and prev_price > 2000 and current_price < 50:
                logger.warning(
                    f"Anomalous glitched price detected for {canonical_product_id or deal_id}: "
                    f"₹{current_price} vs prev ₹{prev_price}. Disallowing price-drop re-post."
                )
                return True

            # 3. Minimum cooldown check (e.g. 2 hours): do not spam within cooldown even if price dropped further
            if min_cooldown_hours > 0 and posted_at and cooldown_cutoff and posted_at > cooldown_cutoff:
                logger.debug(
                    f"Cooldown active for {canonical_product_id or deal_id} (posted at {posted_at}). "
                    f"Suppression until {min_cooldown_hours}h elapsed."
                )
                return True

            # 4. Check if new price is at least 15% lower than previous price
            if prev_price is not None and prev_price > 0:
                if current_price <= int(prev_price * 0.85):
                    logger.info(
                        f"Price-drop exception for {canonical_product_id or deal_id}: "
                        f"new price ₹{current_price} is >=15% lower than previous ₹{prev_price}"
                    )
                    return False

            return True

    def is_price_drop(
        self,
        deal_id: str,
        canonical_product_id: Optional[str] = None,
        current_price: Optional[int] = None,
        window_hours: int = 48,
    ) -> bool:
        """Check if this deal qualifies as a genuine price drop over the previously posted price."""
        if current_price is None or current_price <= 0:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT price FROM posted_deals
                WHERE (deal_id = ? OR (canonical_product_id IS NOT NULL AND canonical_product_id = ?))
                  AND posted_at >= datetime('now', ?)
                ORDER BY posted_at DESC LIMIT 1
                """,
                (deal_id, canonical_product_id or "", f"-{window_hours} hours"),
            )
            row = cur.fetchone()
            if row and row[0] is not None and row[0] > 0:
                prev_price = int(row[0])
                return current_price <= int(prev_price * 0.85)
        return False

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
        canonical_product_id: Optional[str] = None,
        price: Optional[int] = None,
        effective_price: Optional[int] = None,
    ):
        valid_prices = [p for p in (price, effective_price) if p is not None and p > 0]
        stored_price = min(valid_prices) if valid_prices else price

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO posted_deals (
                    deal_id, retailer, chat_id, message_id, caption, status,
                    category, discount, agent_score, agent_reason,
                    canonical_product_id, price, posted_at, consecutive_oos_count, consecutive_spike_count
                )
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, 0)
                ON CONFLICT(deal_id) DO UPDATE SET
                    retailer=excluded.retailer,
                    chat_id=excluded.chat_id,
                    message_id=excluded.message_id,
                    caption=excluded.caption,
                    status='active',
                    category=excluded.category,
                    discount=excluded.discount,
                    agent_score=excluded.agent_score,
                    agent_reason=excluded.agent_reason,
                    canonical_product_id=COALESCE(excluded.canonical_product_id, posted_deals.canonical_product_id),
                    price=COALESCE(excluded.price, posted_deals.price),
                    posted_at=CURRENT_TIMESTAMP,
                    consecutive_oos_count=0,
                    consecutive_spike_count=0
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
                    canonical_product_id,
                    stored_price,
                ),
            )
            # When an item is re-posted, update the baseline price and posted_at for all records with the same canonical_product_id
            if canonical_product_id and stored_price is not None and stored_price > 0:
                conn.execute(
                    """
                    UPDATE posted_deals
                    SET price = ?, posted_at = CURRENT_TIMESTAMP
                    WHERE canonical_product_id = ?
                    """,
                    (stored_price, canonical_product_id),
                )
        self.checkpoint()

    def increment_oos_count(self, deal_id: str) -> int:
        """Increment consecutive OOS count for a deal and return the updated count."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE posted_deals
                SET consecutive_oos_count = COALESCE(consecutive_oos_count, 0) + 1,
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
                """,
                (deal_id,),
            )
            cur = conn.execute("SELECT consecutive_oos_count FROM posted_deals WHERE deal_id = ?", (deal_id,))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 1

    def reset_oos_count(self, deal_id: str):
        """Reset consecutive OOS count when item is verified back in stock."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE posted_deals SET consecutive_oos_count = 0, last_checked_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (deal_id,),
            )

    def increment_spike_count(self, deal_id: str) -> int:
        """Increment consecutive price spike count for a deal and return the updated count."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE posted_deals
                SET consecutive_spike_count = COALESCE(consecutive_spike_count, 0) + 1,
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
                """,
                (deal_id,),
            )
            cur = conn.execute("SELECT consecutive_spike_count FROM posted_deals WHERE deal_id = ?", (deal_id,))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 1

    def reset_spike_count(self, deal_id: str):
        """Reset consecutive price spike count when price is normal."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE posted_deals SET consecutive_spike_count = 0, last_checked_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (deal_id,),
            )

    def touch_checked(self, deal_id: str) -> None:
        """Update last_checked_at timestamp for a posted deal."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE posted_deals SET last_checked_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (deal_id,),
            )

    def get_recent_active_deals(self, limit: int = 6) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT deal_id, retailer, chat_id, message_id, caption, price
                FROM posted_deals
                WHERE status = 'active' AND message_id IS NOT NULL
                ORDER BY CASE WHEN last_checked_at IS NULL THEN 0 ELSE 1 END, last_checked_at ASC, posted_at ASC
                LIMIT ?
                """,
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    def get_top_live_deals_today(self, limit: int = 5, hours: int = 14) -> List[Dict[str, Any]]:
        """Query top still-live deals posted in the last N hours, ordered by discount and agent score."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT deal_id, retailer, chat_id, message_id, caption, price, discount, agent_score, posted_at, canonical_product_id
                FROM posted_deals
                WHERE status = 'active'
                  AND posted_at >= datetime('now', ?)
                ORDER BY COALESCE(discount, 0) DESC, COALESCE(agent_score, 0) DESC, posted_at DESC
                LIMIT ?
                """,
                (f"-{hours} hours", limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_recent_context(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._connect() as conn:
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
        with self._connect() as conn:
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
        with self._connect() as conn:
            conn.execute("UPDATE posted_deals SET status = 'expired' WHERE deal_id = ?", (deal_id,))

    def note_price(self, product_id: str, price: int) -> Tuple[Optional[int], Optional[int]]:
        """Remember a price. Returns the previous price and the lowest price before this one."""
        with self._connect() as conn:
            latest = conn.execute(
                "SELECT price FROM price_seen WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                (product_id,),
            ).fetchone()
            previous = int(latest[0]) if latest else None
            lowest_row = conn.execute(
                "SELECT MIN(price) FROM price_seen WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            lowest_before = int(lowest_row[0]) if lowest_row and lowest_row[0] is not None else None
            if previous != price:
                conn.execute(
                    "INSERT INTO price_seen (product_id, price) VALUES (?, ?)",
                    (product_id, price),
                )
            stale = conn.execute(
                "SELECT id FROM price_seen WHERE product_id = ? ORDER BY id DESC",
                (product_id,),
            ).fetchall()
            for row in stale[30:]:
                conn.execute("DELETE FROM price_seen WHERE id = ?", (row[0],))
            return previous, lowest_before

    def record_price_sighting(self, canonical_id: str, price: int) -> dict:
        """
        Track running price stats (min, max, avg, last, sighting_count) for canonical product IDs.
        Returns the price context evaluated BEFORE mutating price_sightings.
        
        To prevent 30-minute polling dilution:
        - If price changed by < 1% AND less than 12 hours (43,200 seconds) have elapsed since last_seen_at:
          Only updates last_seen_at; does NOT increment sighting_count or dilute avg_price.
        - Only increments sighting_count and updates avg_price if >= 12h elapsed OR price changed by >= 1%.
        """
        default_res = {
            "status": "normal",
            "score_adj": 0,
            "min_price": None,
            "avg_price": None,
            "sighting_count": 0,
        }
        if not canonical_id or price is None or price <= 0:
            return default_res

        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT min_price, max_price, avg_price, last_price, sighting_count,
                       (strftime('%s', 'now') - strftime('%s', last_seen_at)) as elapsed_sec
                FROM price_sightings WHERE canonical_id = ?
                """,
                (canonical_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO price_sightings (canonical_id, min_price, max_price, avg_price, last_price, sighting_count, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (canonical_id, price, price, float(price), price),
                )
                return default_res

            min_p, max_p, avg_p, last_p, count, elapsed_sec = row

            # Evaluate context BEFORE updating min_price / avg_price
            prior_context = {
                "status": "normal",
                "score_adj": 0,
                "min_price": min_p,
                "avg_price": avg_p,
                "sighting_count": count,
            }
            if count >= 3 and price < min_p:
                prior_context["status"] = "all_time_low"
                prior_context["score_adj"] = 10
            elif count >= 2 and price <= int(avg_p * 0.82):
                prior_context["status"] = "real_drop"
                prior_context["score_adj"] = 18
            elif count >= 4 and price >= int(avg_p * 0.98):
                prior_context["status"] = "stagnant"
                prior_context["score_adj"] = -10

            # Polling dilution guard:
            price_change_pct = abs(price - last_p) / max(last_p, 1)
            is_same_price = price_change_pct < 0.01
            is_recent = elapsed_sec is not None and elapsed_sec < 43200

            if is_same_price and is_recent:
                conn.execute(
                    "UPDATE price_sightings SET last_seen_at = CURRENT_TIMESTAMP WHERE canonical_id = ?",
                    (canonical_id,),
                )
            else:
                new_count = count + 1
                new_avg = ((avg_p * count) + price) / new_count
                new_min = min(min_p, price)
                new_max = max(max_p, price)
                conn.execute(
                    """
                    UPDATE price_sightings
                    SET min_price = ?, max_price = ?, avg_price = ?, last_price = ?,
                        sighting_count = ?, last_seen_at = CURRENT_TIMESTAMP
                    WHERE canonical_id = ?
                    """,
                    (new_min, new_max, new_avg, price, new_count, canonical_id),
                )

            return prior_context

    def get_price_context(self, canonical_id: str, current_price: int) -> dict:
        """
        Evaluate current price against historical sightings for this canonical product:
        - 'real_drop' (+18): sighting_count >= 2 and current_price <= avg_price * 0.82
        - 'all_time_low' (+10): sighting_count >= 3 and current_price < min_price
        - 'stagnant' (-10): sighting_count >= 4 and current_price >= avg_price * 0.98
        - 'normal' (0): default
        """
        default_res = {
            "status": "normal",
            "score_adj": 0,
            "min_price": None,
            "avg_price": None,
            "sighting_count": 0,
        }
        if not canonical_id or current_price is None or current_price <= 0:
            return default_res
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT min_price, max_price, avg_price, last_price, sighting_count FROM price_sightings WHERE canonical_id = ?",
                (canonical_id,),
            )
            row = cur.fetchone()
            if not row:
                return default_res
            min_p, max_p, avg_p, last_p, count = row
            res = {
                "status": "normal",
                "score_adj": 0,
                "min_price": min_p,
                "max_price": max_p,
                "avg_price": avg_p,
                "sighting_count": count,
            }
            if count >= 3 and current_price < min_p:
                res["status"] = "all_time_low"
                res["score_adj"] = 10
            elif count >= 2 and current_price <= int(avg_p * 0.82):
                res["status"] = "real_drop"
                res["score_adj"] = 18
            elif count >= 4 and current_price >= int(avg_p * 0.98):
                res["status"] = "stagnant"
                res["score_adj"] = -10
            return res

    def record_event(self, deal_id: str, outcome: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cycle_events (deal_id, outcome, detail) VALUES (?, ?, ?)",
                (deal_id, outcome, detail[:200]),
            )
            conn.execute(
                """
                DELETE FROM cycle_events
                WHERE id < (
                    SELECT MIN(id) FROM (
                        SELECT id FROM cycle_events ORDER BY id DESC LIMIT 400
                    )
                )
                """
            )

    def recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT deal_id, outcome, detail FROM cycle_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bot_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def add_worth_tap(self, deal_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO engagement (deal_id, worth_taps, reaction_count) VALUES (?, 1, 0)
                ON CONFLICT(deal_id) DO UPDATE SET worth_taps = worth_taps + 1
                """,
                (deal_id,),
            )

    def set_reaction_count(self, deal_id: str, total: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO engagement (deal_id, worth_taps, reaction_count) VALUES (?, 0, ?)
                ON CONFLICT(deal_id) DO UPDATE SET reaction_count = excluded.reaction_count
                """,
                (deal_id, max(0, int(total))),
            )

    def deal_id_for_message(self, message_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT deal_id FROM posted_deals WHERE message_id = ? ORDER BY posted_at DESC LIMIT 1",
                (message_id,),
            ).fetchone()
            return row[0] if row else None

    def engagement_line(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(worth_taps), 0), COALESCE(SUM(reaction_count), 0) FROM engagement"
            ).fetchone()
        taps, reactions = int(row[0] or 0), int(row[1] or 0)
        if taps == 0 and reactions == 0:
            return ""
        return f"Engagement on earlier posts: {taps} worth-it taps, {reactions} reactions."


def price_note_for(previous: Optional[int], lowest_before: Optional[int], price: Optional[int]) -> Optional[str]:
    """Only claim a drop we have actually seen. Never say lowest in 90 days."""
    if previous is None or price is None or price >= previous:
        return None
    if lowest_before is None or price <= lowest_before:
        return f"Lowest price we have seen. Last time it was ₹{previous:,}."
    return f"Lower than the last price we saw (₹{previous:,})."

def _ai_copy_keeps_price(ai_body: str, deal: Deal) -> bool:
    """Reject model copy that dropped the real deal price. Normalizes commas and .00."""
    if not deal.current_price and not getattr(deal, "effective_price", None):
        return True
    clean_body = re.sub(r"\.00\b", "", ai_body or "")
    digits_body = re.sub(r"\D", "", clean_body)
    price = parse_rupees(deal.current_price)
    if price and str(price) in digits_body:
        return True
    eff = getattr(deal, "effective_price", None)
    if eff and str(eff) in digits_body:
        return True
    return False


def offer_percent(deal: Deal) -> Optional[int]:
    """Percent to print. Trust the prices when the feed's percent is far off."""
    stated = int(deal.discount_percent or 0)
    price = parse_rupees(deal.current_price)
    mrp = parse_rupees(deal.original_price)
    implied = None
    if price and mrp and mrp > price:
        implied = int(round((mrp - price) / mrp * 100))
    if stated and implied is not None and abs(stated - implied) >= 35:
        return implied
    if stated:
        return stated
    return implied


def safe_telegram_post(endpoint: str, payload: dict, timeout: int = 20) -> requests.Response:
    """Execute a Telegram API POST with automatic HTTP 429 Too Many Requests retry."""
    import time
    resp = requests.post(endpoint, json=payload, timeout=timeout)
    if resp.status_code == 429:
        try:
            data = resp.json()
            retry_after = int(data.get("parameters", {}).get("retry_after", 3))
        except Exception:
            retry_after = 3
        wait_secs = min(max(retry_after + 1, 1), 60)
        logger.warning(f"Telegram returned 429 Too Many Requests. Sleeping for {wait_secs}s before retry...")
        time.sleep(wait_secs)
        resp = requests.post(endpoint, json=payload, timeout=timeout)
    return resp


def deal_badge(deal: Deal) -> str:
    """LOOT ALERT for loot items or steep drops, FURTHER PRICE DROP for drops. Otherwise DEAL."""
    if getattr(deal, "is_further_price_drop", False):
        return "FURTHER PRICE DROP"
    if getattr(deal, "is_loot", False):
        return "LOOT ALERT"
    percent = offer_percent(deal) or 0
    price = parse_rupees(deal.current_price)
    if percent >= 50 or (price is not None and price < 100 and percent >= 20):
        return "LOOT"
    return "DEAL"

def _coupon_code(deal: Deal) -> Optional[str]:
    raw = (deal.coupon_code or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,24}", raw):
        return raw
    return None


def _ai_tagline(ai_body: Optional[str], deal: Deal) -> Optional[str]:
    """One short line under the price. Never replaces the price block."""
    if not ai_body or not _ai_copy_keeps_price(ai_body, deal):
        return None
    text = re.sub(r"<[^>]+>", " ", ai_body)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 8:
        return None
    return html.escape(text[:160])


def format_caption(deal: Deal, copywriter: Optional[AICopywriter] = None) -> str:
    retailer = html.escape(deal.retailer or "Store")
    badge = deal_badge(deal)
    if badge == "LOOT ALERT":
        mark = "🚨"
    elif badge in ("LOOT", "FURTHER PRICE DROP"):
        mark = "🔥"
    else:
        mark = "⚡"

    ai_body = getattr(deal, "_cached_ai_copy", None)
    if ai_body is None and copywriter and copywriter.is_configured():
        ai_body = copywriter.craft_deal_copy(deal)
        if ai_body and not _ai_copy_keeps_price(ai_body, deal):
            logger.info("Agent critic rejected AI copy that dropped the deal price.")
            ai_body = None

    lines = [f"{mark} <b>{badge}</b> · {retailer}"]

    # 1. Multi-Source Viral Consensus
    if getattr(deal, "source_count", 1) >= 2:
        lines.append(f"🔥 Trending across {deal.source_count} deal communities")

    percent = offer_percent(deal)
    from dealbot.regions import get_currency_symbol
    curr_sym = get_currency_symbol()
    price_bits = []
    if getattr(deal, "effective_price", None) and getattr(deal, "coupon_discount", None):
        price_bits.append(f"💰 <b>Effective Price: {curr_sym}{deal.effective_price:,}</b>")
        if percent:
            price_bits.append(f"<i>({percent}% OFF)</i>")
        lines.append(" ".join(price_bits))
        waterfall_parts = []
        if deal.original_price:
            waterfall_parts.append(f"MRP: <del>{html.escape(deal.original_price, quote=False)}</del>")
        if deal.current_price:
            waterfall_parts.append(f"List: {html.escape(deal.current_price)}")
        waterfall_parts.append(f"Coupon: -{curr_sym}{deal.coupon_discount:,}")
        if getattr(deal, "bank_discount_amount", None):
            waterfall_parts.append(f"Bank: -{curr_sym}{deal.bank_discount_amount:,}")
        lines.append(f"🧾 {' | '.join(waterfall_parts)}")
        lines.append(f"List Price: {html.escape(deal.current_price or '')} (Apply {curr_sym}{deal.coupon_discount:,} on-page coupon)")
    else:
        if deal.current_price:
            price_bits.append(f"<b>{html.escape(deal.current_price)}</b>")
        if percent:
            price_bits.append(f"<b>{percent}% OFF</b>")
        if price_bits:
            lines.append(" · ".join(price_bits))

    lines.append("")
    lines.append(f"<b>{html.escape(deal.title or 'Deal', quote=False)}</b>")

    if not (getattr(deal, "effective_price", None) and getattr(deal, "coupon_discount", None)) and deal.original_price:
        lines.append(f"MRP <s>{html.escape(deal.original_price, quote=False)}</s>")
    savings = deal.savings_rupees()
    if savings and savings >= 1:
        lines.append(f"You save {curr_sym}{savings:,}")

    coupon = _coupon_code(deal)
    if coupon:
        lines.append(f"Coupon <b>{html.escape(coupon, quote=False)}</b>")

    # 1-Tap Copy Promo Code (GrabOn-Style)
    promo = getattr(deal, "promo_code", None)
    if promo and promo != coupon:
        lines.append(f"🎟️ Promo Code: <code>{html.escape(promo, quote=False)}</code> <i>(Tap to copy)</i>")
    elif promo:
        lines.append(f"🎟️ Promo Code: <code>{html.escape(promo, quote=False)}</code> <i>(Tap to copy)</i>")

    bank = (deal.bank_offer or "").strip()
    if bank and bank.lower() not in {"0", "none", "no", "na", "n/a"}:
        lines.append(f"Bank offer: {html.escape(bank[:90], quote=False)}")

    # Coupon Trick Checklist (Tick "Apply ₹... coupon" on Amazon page)
    c_text = getattr(deal, "coupon_text", None) or (f"Apply {curr_sym}{deal.coupon_discount:,} coupon" if getattr(deal, "coupon_discount", None) else None)
    if c_text:
        lines.append(f'☑️ <b>Coupon Trick:</b> Tick <i>"{html.escape(c_text, quote=False)}"</i> on Amazon page!')

    # Bonus Offers (UPI cashback, S&S, Combo)
    extra_offers = getattr(deal, "extra_offers", []) or []
    for off in extra_offers[:2]:
        lines.append(f"🎁 <b>Bonus Offer:</b> {html.escape(str(off), quote=False)}")
    if getattr(deal, "rating", None) is not None:
        reviews_part = f" ({deal.review_count:,} reviews)" if getattr(deal, "review_count", None) else ""
        lines.append(f"⭐ <b>{deal.rating}★</b>{reviews_part}")

    # BuyHatke-Style Buy-Now Confidence Meter
    if getattr(deal, "buy_confidence", None) is not None:
        from dealbot.agent import format_buy_confidence_meter
        lines.append(format_buy_confidence_meter(deal.buy_confidence))

    # Price Trajectory Trend
    if getattr(deal, "price_trend_text", None):
        lines.append(deal.price_trend_text)

    if deal.price_note:
        lines.append(html.escape(deal.price_note))
    if deal.checked:
        lines.append("Checked just now")

    tagline = _ai_tagline(ai_body, deal)
    if tagline:
        lines.append("")
        lines.append(f"<i>{tagline}</i>")

    lines.append("")
    lines.append(f'🛒 <a href="{html.escape(deal.url or "#")}"><b>Buy Now on {retailer}</b></a>')
    lines.append("🔁 Forward this if it is still live")
    lines.append("")
    tg_link = os.getenv("TELEGRAM_CHANNEL_LINK", "https://t.me/Flash_Deals_Flipkart_Amazon")
    wa_link = os.getenv("WHATSAPP_CHANNEL_LINK", "https://chat.whatsapp.com/GspHsCGTRm29orUszbuvh3?s=cl&p=a&mlu=4&ilr=4&iam=2")
    x_link = os.getenv("X_ACCOUNT_LINK", "https://x.com/PrasadDammai")
    rd_link = os.getenv("REDDIT_COMMUNITY_LINK", "https://www.reddit.com/r/Indiashopping/")
    lines.append(f'👉 <b>Follow DealDash AI:</b> <a href="{tg_link}">Telegram</a> · <a href="{wa_link}">WhatsApp</a> · <a href="{x_link}">X</a> · <a href="{rd_link}">Reddit</a>')
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
        resp = safe_telegram_post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            payload={"chat_id": admin_id, "text": text[:3500], "disable_web_page_preview": True},
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

    # Upgraded High-CTR inline keyboard
    from dealbot.regions import (
        get_currency_symbol,
        get_keepa_url,
        get_price_history_url,
        get_market_region,
    )
    curr_sym = get_currency_symbol()
    eff_p = getattr(deal, "effective_price", None)
    disp_price = f"{curr_sym}{eff_p:,}" if eff_p else deal.current_price
    disc_part = f" ({deal.discount_percent}% OFF)" if deal.discount_percent else ""
    buy_text = f"🛒 Grab Deal @ {disp_price}{disc_part}" if disp_price else f"🛒 Buy Now on {deal.retailer or 'Store'}"
    primary_row = [{"text": buy_text, "url": deal.url or "#"}]

    # Extract Amazon ASIN for Price Verification buttons (Keepa + PriceHistory/CamelCamelCamel)
    asin = None
    if deal.id and deal.id.startswith("amz-"):
        asin = deal.id.replace("amz-", "").strip().upper()
    elif getattr(deal, "canonical_product_id", None) and deal.canonical_product_id.startswith("amazon:"):
        asin = deal.canonical_product_id.replace("amazon:", "").strip().upper()

    row2 = []
    row3 = []
    if asin and len(asin) == 10:
        row2.append({"text": "📈 Keepa Chart", "url": get_keepa_url(asin)})
        row2.append({"text": "🌐 BharatBazaro.com", "url": "https://bharatbazaro.com"})
        ph_url = get_price_history_url(asin)
        if ph_url:
            provider_label = "Price History Check" if get_market_region() == "IN" else "CamelCamelCamel"
            row3.append({"text": f"🔍 {provider_label}", "url": ph_url})

    row_worth = [{"text": "👍 Worth it", "callback_data": f"worth:{(deal.id or '')[:40]}"}]

    inline_rows = [primary_row]
    if row2:
        inline_rows.append(row2)
    if row3:
        inline_rows.append(row3)
    inline_rows.append(row_worth)
    reply_markup = {"inline_keyboard": inline_rows}

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
            try:
                resp = safe_telegram_post(endpoint, payload=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    msg_id = data.get("result", {}).get("message_id")
                    logger.info(f"Successfully posted deal with photo: {deal.id} (message_id={msg_id})")
                    return True, msg_id, caption
                else:
                    logger.warning(f"sendPhoto failed ({resp.status_code}: {resp.text}). Falling back to text message...")
            except requests.RequestException as photo_err:
                logger.warning(f"sendPhoto request error: {photo_err}. Falling back to text message...")

        # Fallback or text-only sendMessage
        endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": reply_markup,
        }
        resp = safe_telegram_post(endpoint, payload=payload, timeout=20)
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


def send_top_live_deals_digest(
    store: StateStore,
    chat_id: str,
    token: str,
    dry_run: bool = False,
    limit: int = 5,
) -> bool:
    """Post and pin a curated digest of today's top still-live deals."""
    logger = logging.getLogger("dealbot.publisher")
    deals = store.get_top_live_deals_today(limit=limit)
    if len(deals) < 3:
        logger.info(f"Only {len(deals)} live deal(s) found for today (min 3 required for digest). Skipping.")
        return False

    from dealbot.regions import get_region_config, get_currency_symbol
    cfg = get_region_config()
    curr_sym = get_currency_symbol()
    partner_tag = (
        os.getenv("AMAZON_AFFILIATE_TAG")
        or os.getenv("AMAZON_PARTNER_TAG")
        or "dammaiprasad-21"
    ).strip()

    lines = [
        f"🏆 <b>TODAY'S TOP {len(deals)} STILL-LIVE MEGA DEALS</b> 🔥",
        "",
    ]
    for idx, d in enumerate(deals, start=1):
        deal_id = d["deal_id"]
        price = d.get("price")
        discount = d.get("discount")
        caption = d.get("caption") or ""

        # Extract title from caption (the bold product title)
        title = ""
        for line in caption.split("\n"):
            m = re.match(r"^<b>([^<]+)</b>$", line.strip())
            if m and "deal" not in m.group(1).lower() and "loot" not in m.group(1).lower():
                title = m.group(1).strip()
                break
        if not title:
            m = re.search(r"<b>([^<]{5,80})</b>", caption)
            title = m.group(1).strip() if m else deal_id

        # Build affiliate URL
        asin = None
        if deal_id.startswith("amz-"):
            asin = deal_id.replace("amz-", "")
        elif d.get("canonical_product_id") and d["canonical_product_id"].startswith("amazon:"):
            asin = d["canonical_product_id"].replace("amazon:", "")

        if asin:
            aff_url = f"https://www.{cfg.get('domain', 'amazon.in')}/dp/{asin}?tag={partner_tag}&linkCode=ll1"
        else:
            href_m = re.search(r'href="([^"]+)"', caption)
            aff_url = href_m.group(1) if href_m else "#"

        price_str = f"<b>{curr_sym}{price:,}</b>" if price else ""
        disc_str = f"({discount}% OFF)" if discount else ""
        lines.append(f"{idx}. <b>{html.escape(title[:65], quote=False)}</b> — {price_str} {disc_str} — <a href=\"{html.escape(aff_url)}\"><b>Grab Here</b></a>")

    lines.append("")
    tg_link = os.getenv("TELEGRAM_CHANNEL_LINK", "https://t.me/Flash_Deals_Flipkart_Amazon")
    wa_link = os.getenv("WHATSAPP_CHANNEL_LINK", "https://chat.whatsapp.com/GspHsCGTRm29orUszbuvh3?s=cl&p=a&mlu=4&ilr=4&iam=2")
    x_link = os.getenv("X_ACCOUNT_LINK", "https://x.com/PrasadDammai")
    rd_link = os.getenv("REDDIT_COMMUNITY_LINK", "https://www.reddit.com/r/Indiashopping/")
    lines.append("👉 <b>Follow DealDash AI Everywhere:</b>")
    lines.append(f'📲 <a href="{tg_link}">Telegram Channel</a> | 💬 <a href="{wa_link}">WhatsApp Group</a>')
    lines.append(f'🐦 <a href="{x_link}">X (Twitter) Profile</a> | 🌐 <a href="{rd_link}">Reddit Community</a> | ⚡ <a href="https://bharatbazaro.com">Live 24x7 Radar</a>')
    lines.append("")
    lines.append("<i>⚡ Prices and coupons fluctuate rapidly. Forward to friends before these deals expire!</i>")
    digest_html = "\n".join(lines)

    if dry_run:
        logger.info(f"[DRY RUN] Would send and pin Top Live Deals Digest:\n{digest_html}")
        return True

    if not token or not chat_id:
        logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID for digest")
        return False

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": digest_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = safe_telegram_post(endpoint, payload=payload, timeout=20)
    if resp.status_code == 200:
        msg_id = resp.json().get("result", {}).get("message_id")
        logger.info(f"Published Top Live Deals Digest (message_id={msg_id})")
        if msg_id:
            pin_endpoint = f"https://api.telegram.org/bot{token}/pinChatMessage"
            pin_payload = {
                "chat_id": chat_id,
                "message_id": msg_id,
                "disable_notification": True,
            }
            try:
                safe_telegram_post(pin_endpoint, payload=pin_payload, timeout=10)
            except Exception as e:
                logger.debug(f"pinChatMessage silent fallback: {e}")
        return True
    else:
        logger.warning(f"Failed to post Top Live Deals Digest: {resp.status_code} {resp.text}")
        return False
