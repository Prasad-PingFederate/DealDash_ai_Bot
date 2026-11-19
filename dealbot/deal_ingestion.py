# dealbot/deal_ingestion.py
"""Secure Real-Time Deal Ingestion Engine for BharatBazaro.

Handles:
1. Authentication of internal webhook payloads via INTERNAL_SYNC_SECRET.
2. Comprehensive payload validation:
   - canonical_id, title, merchant, category, price, mrp, discount_pct,
     bank_offer, coupon_code, affiliate_url, image_url, expected_payout,
     score, subid, expires_at.
3. Outbound URL canonicalization with web SubID attribution ('web_deal_<category>_<id>').
4. SQLite & JSON upsert with price history tracking:
   - Deduplicates by canonical_id + merchant.
   - Appends to deal_price_history.
   - Calculates 'All-Time Low' and 'Lowest in 30 days' badges.
5. Returns { deal_id, canonical_web_url, lowest_badge, price_status }.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from dealbot.compliance import IST, to_bharatbazaro_schema
from dealbot.monetizer import build_subid, monetize_url, slugify

logger = logging.getLogger("dealbot.deal_ingestion")

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "state.sqlite3"
DEFAULT_JSON_PATH = ROOT_DIR / "data" / "live_deals.json"


def authenticate_sync_request(
    auth_header_or_secret: Optional[str],
    configured_secret: Optional[str] = None,
) -> bool:
    """Verify internal sync secret using constant-time comparison."""
    secret = (configured_secret or os.getenv("INTERNAL_SYNC_SECRET", "")).strip()
    if not secret:
        logger.warning("[Auth] INTERNAL_SYNC_SECRET is not configured on server")
        return False

    if not auth_header_or_secret:
        return False

    candidate = str(auth_header_or_secret).strip()
    # Strip optional "Bearer " prefix if provided
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()

    return hmac.compare_digest(candidate, secret)


def validate_deal_payload(payload: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate incoming deal payload structure and data types.

    Required fields:
    - canonical_id, title, merchant, price
    Optional fields:
    - category, mrp, discount_pct, bank_offer, coupon_code,
      affiliate_url, image_url, expected_payout, score, subid, expires_at
    """
    if not isinstance(payload, dict):
        return False, "Payload must be a JSON object", {}

    # 1. Check required primary fields
    canonical_id = str(payload.get("canonical_id") or payload.get("asin") or payload.get("id") or "").strip()
    if not canonical_id:
        return False, "Missing required field: 'canonical_id'", {}

    title = str(payload.get("title") or "").strip()
    if not title:
        return False, "Missing required field: 'title'", {}

    merchant = str(payload.get("merchant") or payload.get("store") or payload.get("retailer") or "amazon").strip().lower()
    if not merchant:
        return False, "Missing required field: 'merchant'", {}

    raw_price = payload.get("price") if payload.get("price") is not None else payload.get("effective_price")
    try:
        price = float(raw_price)
        if price <= 0:
            return False, "Price must be greater than zero", {}
    except (ValueError, TypeError):
        return False, f"Invalid price value: {raw_price}", {}

    # 2. Extract and cast optional fields
    category = str(payload.get("category") or "general").strip().lower()

    raw_mrp = payload.get("mrp") or payload.get("original_price")
    try:
        mrp = float(raw_mrp) if raw_mrp is not None else price
    except (ValueError, TypeError):
        mrp = price

    raw_disc = payload.get("discount_pct") or payload.get("discount_percent")
    try:
        discount_pct = int(raw_disc) if raw_disc is not None else 0
    except (ValueError, TypeError):
        discount_pct = 0

    if mrp > price and not discount_pct:
        discount_pct = int(round((mrp - price) / mrp * 100))

    bank_offer = payload.get("bank_offer")
    coupon_code = payload.get("coupon_code") or payload.get("promo_code")
    affiliate_url = str(payload.get("affiliate_url") or payload.get("deal_url") or payload.get("url") or "").strip()
    image_url = str(payload.get("image_url") or "").strip()

    try:
        expected_payout = float(payload.get("expected_payout") or 0.0)
    except (ValueError, TypeError):
        expected_payout = 0.0

    try:
        score = float(payload.get("score") or payload.get("profit_score") or 70.0)
    except (ValueError, TypeError):
        score = 70.0

    subid = str(payload.get("subid") or "").strip()
    expires_at = str(payload.get("expires_at") or "").strip() or None

    clean_data = {
        "canonical_id": canonical_id,
        "title": title,
        "merchant": merchant,
        "category": category,
        "price": price,
        "mrp": mrp,
        "discount_pct": discount_pct,
        "bank_offer": str(bank_offer).strip() if bank_offer else None,
        "coupon_code": str(coupon_code).strip() if coupon_code else None,
        "affiliate_url": affiliate_url,
        "image_url": image_url,
        "expected_payout": expected_payout,
        "score": score,
        "subid": subid,
        "expires_at": expires_at,
    }
    return True, "Valid payload", clean_data


class DealIngestionEngine:
    """Manages deal upserts, database persistence, and price history."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        json_path: Optional[Union[str, Path]] = None,
    ):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.json_path = Path(json_path or DEFAULT_JSON_PATH)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite schema for live_deals and deal_price_history."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS live_deals (
                    canonical_id TEXT,
                    merchant TEXT,
                    title TEXT NOT NULL,
                    category TEXT,
                    price REAL NOT NULL,
                    mrp REAL,
                    discount_pct INTEGER,
                    bank_offer TEXT,
                    coupon_code TEXT,
                    affiliate_url TEXT,
                    image_url TEXT,
                    expected_payout REAL,
                    score REAL,
                    bot_subid TEXT,
                    web_subid TEXT,
                    canonical_web_url TEXT,
                    expires_at TEXT,
                    all_time_low REAL,
                    lowest_30d REAL,
                    lowest_badge TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (canonical_id, merchant)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deal_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    price REAL NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hist_lookup
                ON deal_price_history (canonical_id, merchant, recorded_at)
            """)

    def record_price_history_and_get_badges(
        self,
        canonical_id: str,
        merchant: str,
        current_price: float,
    ) -> Tuple[Optional[float], Optional[float], str, str]:
        """Record price point into price history and compute price drop badges.

        Returns:
            (all_time_low, lowest_30d, lowest_badge, price_status)
        """
        now_utc = datetime.now(timezone.utc)
        thirty_days_ago = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.db_path) as conn:
            # 1. Fetch existing all-time low and 30-day low
            cur = conn.execute(
                """
                SELECT MIN(price) FROM deal_price_history
                WHERE canonical_id = ? AND merchant = ?
                """,
                (canonical_id, merchant),
            )
            row = cur.fetchone()
            prev_all_time_low = float(row[0]) if row and row[0] is not None else None

            cur30 = conn.execute(
                """
                SELECT MIN(price) FROM deal_price_history
                WHERE canonical_id = ? AND merchant = ? AND recorded_at >= ?
                """,
                (canonical_id, merchant, thirty_days_ago),
            )
            row30 = cur30.fetchone()
            prev_lowest_30d = float(row30[0]) if row30 and row30[0] is not None else None

            # 2. Check latest recorded price to avoid redundant entries
            cur_last = conn.execute(
                """
                SELECT price FROM deal_price_history
                WHERE canonical_id = ? AND merchant = ?
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (canonical_id, merchant),
            )
            last_row = cur_last.fetchone()
            last_price = float(last_row[0]) if last_row and last_row[0] is not None else None

            # Insert new point if price changed or first seen
            if last_price is None or abs(last_price - current_price) > 0.01:
                conn.execute(
                    """
                    INSERT INTO deal_price_history (canonical_id, merchant, price)
                    VALUES (?, ?, ?)
                    """,
                    (canonical_id, merchant, current_price),
                )
                price_status = "price_updated" if last_price is not None else "new_deal"
            else:
                price_status = "price_unchanged"

        # 3. Compute updated badges
        all_time_low = min(prev_all_time_low, current_price) if prev_all_time_low else current_price
        lowest_30d = min(prev_lowest_30d, current_price) if prev_lowest_30d else current_price

        if prev_all_time_low is not None and current_price < prev_all_time_low:
            badge = "ALL-TIME LOW"
        elif prev_lowest_30d is not None and current_price < prev_lowest_30d:
            badge = "LOWEST IN 30 DAYS"
        elif prev_all_time_low is not None and abs(current_price - prev_all_time_low) < 0.01:
            badge = "MATCHES ALL-TIME LOW"
        else:
            badge = ""

        return all_time_low, lowest_30d, badge, price_status

    def upsert_deal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Canonicalize URL with web SubID attribution, record history, and upsert."""
        valid, msg, clean = validate_deal_payload(payload)
        if not valid:
            raise ValueError(msg)

        canonical_id = clean["canonical_id"]
        clean_id = canonical_id.replace("amz-", "").replace("amazon:", "").strip().upper()
        merchant = clean["merchant"]
        category = clean["category"]
        price = clean["price"]
        raw_aff_url = clean["affiliate_url"]

        # 1. Generate Web SubID & Canonicalize Outbound URL
        web_subid = f"web_deal_{slugify(category)[:8]}_{slugify(clean_id)[:16]}"
        if raw_aff_url:
            monetize_res = monetize_url(
                raw_aff_url,
                channel="web",
                category=category,
                deal_id=clean_id,
                allow_unshorten=False,
            )
            web_affiliate_url = monetize_res.monetized_url
        else:
            web_affiliate_url = f"https://www.amazon.in/dp/{clean_id}?tag=dammaiprasad-21&ascsubtag={web_subid}"

        # 2. Canonical Web URL for BharatBazaro storefront
        base_domain = os.getenv("BHARATBAZARO_BASE_URL", "https://bharatbazaro.com").rstrip("/")
        canonical_web_url = f"{base_domain}/deal/{clean_id}.html"

        # 3. Price History Tracking & Badges
        atl, low30, badge, price_status = self.record_price_history_and_get_badges(
            canonical_id=canonical_id,
            merchant=merchant,
            current_price=price,
        )

        # 4. Upsert into SQLite
        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO live_deals (
                    canonical_id, merchant, title, category, price, mrp, discount_pct,
                    bank_offer, coupon_code, affiliate_url, image_url, expected_payout,
                    score, bot_subid, web_subid, canonical_web_url, expires_at,
                    all_time_low, lowest_30d, lowest_badge, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id, merchant) DO UPDATE SET
                    title = excluded.title,
                    price = excluded.price,
                    mrp = excluded.mrp,
                    discount_pct = excluded.discount_pct,
                    bank_offer = excluded.bank_offer,
                    coupon_code = excluded.coupon_code,
                    affiliate_url = excluded.affiliate_url,
                    image_url = excluded.image_url,
                    expected_payout = excluded.expected_payout,
                    score = excluded.score,
                    bot_subid = excluded.bot_subid,
                    web_subid = excluded.web_subid,
                    canonical_web_url = excluded.canonical_web_url,
                    expires_at = excluded.expires_at,
                    all_time_low = excluded.all_time_low,
                    lowest_30d = excluded.lowest_30d,
                    lowest_badge = excluded.lowest_badge,
                    last_updated_at = excluded.last_updated_at
                """,
                (
                    canonical_id,
                    merchant,
                    clean["title"],
                    category,
                    price,
                    clean["mrp"],
                    clean["discount_pct"],
                    clean["bank_offer"],
                    clean["coupon_code"],
                    web_affiliate_url,
                    clean["image_url"],
                    clean["expected_payout"],
                    clean["score"],
                    clean["subid"],
                    web_subid,
                    canonical_web_url,
                    clean["expires_at"],
                    atl,
                    low30,
                    badge,
                    now_str,
                ),
            )

        # 5. Sync to live_deals.json
        self._sync_live_deals_json(
            canonical_id=canonical_id,
            clean=clean,
            web_affiliate_url=web_affiliate_url,
            web_subid=web_subid,
            canonical_web_url=canonical_web_url,
            atl=atl,
            low30=low30,
            badge=badge,
        )

        return {
            "status": "success",
            "deal_id": canonical_id,
            "merchant": merchant,
            "price": price,
            "price_status": price_status,
            "lowest_badge": badge,
            "all_time_low": atl,
            "lowest_30d": low30,
            "web_subid": web_subid,
            "canonical_web_url": canonical_web_url,
        }

    def _sync_live_deals_json(
        self,
        canonical_id: str,
        clean: Dict[str, Any],
        web_affiliate_url: str,
        web_subid: str,
        canonical_web_url: str,
        atl: Optional[float],
        low30: Optional[float],
        badge: str,
    ) -> None:
        """Sync the upserted deal into data/live_deals.json for SSG and API readers."""
        try:
            self.json_path.parent.mkdir(parents=True, exist_ok=True)
            deals: List[Dict[str, Any]] = []
            if self.json_path.exists():
                try:
                    with open(self.json_path, "r", encoding="utf-8") as f:
                        deals = json.load(f)
                except Exception as e:
                    logger.debug("Could not read %s: %s", self.json_path, e)

            item = {
                "id": canonical_id,
                "asin": clean["canonical_id"].replace("amz-", "").replace("amazon:", "").strip().upper(),
                "title": clean["title"],
                "merchant": clean["merchant"],
                "category": clean["category"],
                "effective_price": clean["price"],
                "mrp": clean["mrp"],
                "discount_pct": clean["discount_pct"],
                "bank_offer": clean["bank_offer"],
                "coupon_code": clean["coupon_code"],
                "affiliate_url": web_affiliate_url,
                "image_url": clean["image_url"],
                "expected_payout": clean["expected_payout"],
                "score": clean["score"],
                "web_subid": web_subid,
                "canonical_web_url": canonical_web_url,
                "all_time_low": atl,
                "lowest_30d": low30,
                "lowest_badge": badge,
                "updated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
            }

            by_id = {canonical_id: item}
            for d in deals:
                k = d.get("id") or d.get("asin")
                if k and k not in by_id:
                    by_id[k] = d

            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(list(by_id.values())[:500], f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Could not sync live_deals.json: %s", exc)
