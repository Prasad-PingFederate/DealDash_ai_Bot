# dealbot/profit_ranker.py
"""Expected Profit & Conversion Ranking Engine for DealBot.

Handles:
1. Dynamic config-driven commission schedules, category caps, and 0%-commission exclusions
   (sourced from dealbot/commission_config.json).
2. Expected Profit calculation:
   effective_payout = min(price * commission_rate, category_cap)  (0 if excluded)
   profit_score = effective_payout * discount_quality_mult * price_tier_cvr_weight * freshness_weight
3. Deduplication cooldown (default 12 hours) with a >= 5% price-drop override.
4. Pre-posting profitability gating enforcing minimum payout and score thresholds.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("dealbot.profit_ranker")

IST = timezone(timedelta(hours=5, minutes=30))

# Fallback commission config in case JSON file is missing
_DEFAULT_CONFIG: Dict[str, Any] = {
    "categories": {
        "fashion": {"rate": 0.08, "cap_inr": 1000},
        "apparel": {"rate": 0.08, "cap_inr": 1000},
        "clothing": {"rate": 0.08, "cap_inr": 1000},
        "shoes": {"rate": 0.08, "cap_inr": 1000},
        "footwear": {"rate": 0.08, "cap_inr": 1000},
        "beauty": {"rate": 0.08, "cap_inr": 1000},
        "personal_care": {"rate": 0.08, "cap_inr": 1000},
        "grooming": {"rate": 0.08, "cap_inr": 1000},
        "luxury_beauty": {"rate": 0.08, "cap_inr": 1200},
        "home": {"rate": 0.06, "cap_inr": 1500},
        "kitchen": {"rate": 0.06, "cap_inr": 1500},
        "appliances": {"rate": 0.06, "cap_inr": 2000},
        "furniture": {"rate": 0.06, "cap_inr": 2000},
        "home_decor": {"rate": 0.06, "cap_inr": 1500},
        "toys": {"rate": 0.06, "cap_inr": 800},
        "kids": {"rate": 0.06, "cap_inr": 800},
        "baby": {"rate": 0.06, "cap_inr": 800},
        "grocery": {"rate": 0.05, "cap_inr": 400},
        "gourmet": {"rate": 0.05, "cap_inr": 400},
        "books": {"rate": 0.05, "cap_inr": 500},
        "fitness": {"rate": 0.04, "cap_inr": 1000},
        "sports": {"rate": 0.04, "cap_inr": 1000},
        "electronics": {"rate": 0.015, "cap_inr": 1500},
        "audio": {"rate": 0.015, "cap_inr": 1500},
        "headphones": {"rate": 0.015, "cap_inr": 1500},
        "earbuds": {"rate": 0.015, "cap_inr": 1500},
        "smartwatches": {"rate": 0.015, "cap_inr": 1500},
        "cameras": {"rate": 0.015, "cap_inr": 1500},
        "laptops": {"rate": 0.015, "cap_inr": 1500},
        "computers": {"rate": 0.015, "cap_inr": 1500},
        "mobiles": {"rate": 0.015, "cap_inr": 1000},
        "smartphones": {"rate": 0.015, "cap_inr": 1000},
        "default": {"rate": 0.03, "cap_inr": 1000},
    },
    "exclusions_zero_commission": [
        "gift card",
        "gift voucher",
        "gold coin",
        "silver coin",
        "bullion",
        "flight",
        "recharge",
        "bill payment",
        "precious jewelry",
        "gold bar",
        "e-gift",
        "voucher",
    ],
    "price_tier_cvr_weights": {
        "under_500": 1.4,
        "500_to_1500": 1.2,
        "1500_to_5000": 1.0,
        "5000_to_15000": 0.85,
        "above_15000": 0.65,
    },
}

_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def load_commission_config() -> Dict[str, Any]:
    """Load commission rules from dealbot/commission_config.json with hot-reload support."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    cfg_path = os.getenv("COMMISSION_CONFIG_PATH")
    if not cfg_path:
        root_dir = Path(__file__).resolve().parent
        cfg_path = str(root_dir / "commission_config.json")

    file_path = Path(cfg_path)
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f)
                logger.debug("Loaded commission config from %s", file_path)
                return _CONFIG_CACHE
        except Exception as e:
            logger.warning("Error reading %s, using fallback config: %s", file_path, e)

    _CONFIG_CACHE = _DEFAULT_CONFIG
    return _CONFIG_CACHE


def normalize_category(category: Optional[str]) -> str:
    """Normalize input category string to standard category key."""
    if not category:
        return "default"
    c = str(category).strip().lower().replace("&", " ").replace("-", " ")
    cfg = load_commission_config()
    cats = cfg.get("categories", _DEFAULT_CONFIG["categories"])

    words = c.split()
    for word in words:
        if word in cats:
            return word
    for cat_key in cats:
        if cat_key in c:
            return cat_key
    return "default"


def get_category_commission_rate(category: Optional[str]) -> float:
    """Lookup affiliate commission rate for Indian e-commerce categories."""
    norm = normalize_category(category)
    cfg = load_commission_config()
    cat_info = cfg.get("categories", {}).get(norm, cfg.get("categories", {}).get("default", {"rate": 0.03}))
    return float(cat_info.get("rate", 0.03))


def get_category_cap(category: Optional[str]) -> float:
    """Lookup maximum INR commission cap for a category."""
    norm = normalize_category(category)
    cfg = load_commission_config()
    cat_info = cfg.get("categories", {}).get(norm, cfg.get("categories", {}).get("default", {"cap_inr": 1000.0}))
    return float(cat_info.get("cap_inr", 1000.0))


def is_zero_commission_item(title: Optional[str], category: Optional[str] = "") -> bool:
    """Check if title matches 0%-commission exclusions (gift cards, gold/silver bullion, etc.)."""
    t = str(title or "").lower()
    c = str(category or "").lower()
    cfg = load_commission_config()
    exclusions = cfg.get("exclusions_zero_commission", _DEFAULT_CONFIG["exclusions_zero_commission"])

    for exc in exclusions:
        if exc in t or exc in c:
            return True
    return False


def get_price_tier_cvr_weight(price: float) -> float:
    """Get Conversion Rate (CVR) multiplier based on price tier elasticity."""
    p = float(price or 0.0)
    cfg = load_commission_config()
    weights = cfg.get("price_tier_cvr_weights", _DEFAULT_CONFIG["price_tier_cvr_weights"])

    if p < 500:
        return float(weights.get("under_500", 1.4))
    if p < 1500:
        return float(weights.get("500_to_1500", 1.2))
    if p < 5000:
        return float(weights.get("1500_to_5000", 1.0))
    if p < 15000:
        return float(weights.get("5000_to_15000", 0.85))
    return float(weights.get("above_15000", 0.65))


def get_verdict_multiplier(verdict: Union[str, Dict[str, Any], None]) -> float:
    """Extract multiplier based on honest verdict metadata or badge tier."""
    if not verdict:
        return 1.0

    tier = ""
    v_text = ""
    if isinstance(verdict, dict):
        tier = str(verdict.get("badge_tier") or "").strip().lower()
        v_text = str(verdict.get("verdict") or "").strip().lower()
    else:
        v_text = str(verdict).strip().lower()

    if tier == "screamer" or "all-time low" in v_text or "screamer" in v_text:
        return 1.6
    if tier == "good" or "good buy" in v_text:
        return 1.2
    if tier == "neutral" or "newly tracked" in v_text:
        return 0.9
    if tier == "wait" or "normal price" in v_text or "wait" in v_text:
        return 0.0
    if "fake" in v_text or "inflated" in v_text:
        return 0.0

    return 1.0


def compute_effective_payout(
    deal_price: float,
    category: Optional[str] = "default",
    title: Optional[str] = None,
    custom_rate: Optional[float] = None,
) -> float:
    """Compute capped affiliate payout in INR: min(price * rate, category_cap).

    Returns 0.0 if item is in 0%-commission exclusion list.
    """
    price = max(0.0, float(deal_price or 0.0))
    if price <= 0.0:
        return 0.0

    if is_zero_commission_item(title, category):
        return 0.0

    rate = custom_rate if custom_rate is not None else get_category_commission_rate(category)
    raw_payout = price * rate
    cap = get_category_cap(category)
    return round(min(raw_payout, cap), 2)


# Backwards compatibility alias
compute_estimated_commission = compute_effective_payout


def compute_profit_score(
    deal_price: float,
    category: Optional[str] = "default",
    verdict: Union[str, Dict[str, Any], None] = None,
) -> float:
    """Compute base profit score = estimated commission * verdict multiplier."""
    comm = compute_estimated_commission(deal_price, category)
    v_mult = get_verdict_multiplier(verdict)
    return round(comm * v_mult, 2)


def compute_expected_profit(
    deal_price: float,
    category: Optional[str] = "default",
    title: Optional[str] = None,
    verdict: Union[str, Dict[str, Any], None] = None,
    is_loot: bool = False,
    is_fresh: bool = True,
    custom_rate: Optional[float] = None,
) -> float:
    """Compute overall expected profit score for deal ranking.

    Formula:
        effective_payout * verdict_multiplier * price_tier_cvr_weight * freshness_weight
    """
    payout = compute_effective_payout(deal_price, category, title=title, custom_rate=custom_rate)
    if payout <= 0.0:
        return 0.0

    v_mult = get_verdict_multiplier(verdict)
    if v_mult <= 0.0:
        return 0.0

    cvr_weight = get_price_tier_cvr_weight(deal_price)
    freshness_weight = 1.2 if is_loot else (1.1 if is_fresh else 1.0)

    score = payout * v_mult * cvr_weight * freshness_weight
    return round(score, 2)


def check_deal_cooldown(
    canonical_id: str,
    current_price: float,
    db_path: Optional[str] = None,
    cooldown_hours: float = 12.0,
    drop_override_pct: float = 5.0,
) -> Tuple[bool, str]:
    """Check canonical ASIN/PID deduplication cooldown with >= 5% price drop override.

    Returns:
        (can_post: bool, reason: str)
    """
    if not canonical_id or canonical_id in ("deal-unknown", "amz-unknown"):
        return True, "No canonical ID, cooldown check skipped"

    db_file = Path(db_path or os.getenv("DATABASE_PATH", "data/state.sqlite3"))
    if not db_file.exists():
        return True, "Database not present, cooldown bypassed"

    try:
        with sqlite3.connect(db_file, timeout=2.0) as conn:
            # Check deal_telemetry or posted_deals
            cur = conn.execute(
                """
                SELECT price, timestamp FROM deal_telemetry
                WHERE canonical_id = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (canonical_id,),
            )
            row = cur.fetchone()
            if not row:
                # Fallback to posted_deals table
                cur = conn.execute(
                    """
                    SELECT caption, posted_at FROM posted_deals
                    WHERE deal_id = ? OR caption LIKE ?
                    ORDER BY posted_at DESC LIMIT 1
                    """,
                    (canonical_id, f"%{canonical_id}%"),
                )
                row2 = cur.fetchone()
                if not row2:
                    return True, "Never posted previously"
                return True, "Previously posted without price telemetry"

            last_price, ts_str = row
            try:
                # Handle SQLite timestamp parsing
                dt_posted = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if dt_posted.tzinfo is None:
                    dt_posted = dt_posted.replace(tzinfo=timezone.utc)
            except Exception:
                dt_posted = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours + 1)

            age_hours = (datetime.now(timezone.utc) - dt_posted).total_seconds() / 3600.0

            # 1. Beyond cooldown window
            if age_hours >= cooldown_hours:
                return True, f"Cooldown expired ({age_hours:.1f}h >= {cooldown_hours:.1f}h)"

            # 2. Within cooldown: check >= 5% price drop override
            if last_price and last_price > 0:
                drop_pct = ((last_price - current_price) / last_price) * 100.0
                if drop_pct >= drop_override_pct:
                    return (
                        True,
                        f"Price drop override active: dropped {drop_pct:.1f}% "
                        f"(₹{last_price:.0f} -> ₹{current_price:.0f}) within {age_hours:.1f}h",
                    )
                return (
                    False,
                    f"In cooldown: posted {age_hours:.1f}h ago at ₹{last_price:.0f} "
                    f"(drop was only {drop_pct:.1f}%, requires >={drop_override_pct:.0f}%)",
                )

            return False, f"In cooldown: posted {age_hours:.1f}h ago"
    except Exception as e:
        logger.debug("Cooldown check error: %s", e)
        return True, f"Cooldown check error bypassed: {e}"


def should_publish(
    deal_price: float,
    category: Optional[str] = "default",
    title: Optional[str] = None,
    canonical_id: Optional[str] = None,
    verdict: Union[str, Dict[str, Any], None] = None,
    is_loot: bool = False,
    is_fresh: bool = True,
    min_commission: Optional[float] = None,
    min_profit_score: Optional[float] = None,
    cooldown_hours: Optional[float] = None,
    drop_override_pct: Optional[float] = None,
    db_path: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Comprehensive Expected Profit publishing gate."""
    # Handle signature flexibility if verdict was passed as 3rd positional argument
    if verdict is None and isinstance(title, dict):
        verdict = title
        title = None
    min_payout = (
        min_commission
        if min_commission is not None
        else float(os.getenv("MIN_EXPECTED_PAYOUT_INR", os.getenv("MIN_EST_COMMISSION_INR", "20.0")))
    )
    min_score = (
        min_profit_score
        if min_profit_score is not None
        else float(os.getenv("MIN_PROFIT_SCORE", "50.0"))
    )
    cd_hours = (
        cooldown_hours
        if cooldown_hours is not None
        else float(os.getenv("DEDUPE_COOLDOWN_HOURS", "12.0"))
    )
    drop_override = (
        drop_override_pct
        if drop_override_pct is not None
        else float(os.getenv("PRICE_DROP_OVERRIDE_PCT", "5.0"))
    )

    rate = get_category_commission_rate(category)
    cap = get_category_cap(category)
    payout = compute_effective_payout(deal_price, category, title=title)
    cvr_weight = get_price_tier_cvr_weight(deal_price)
    v_mult = get_verdict_multiplier(verdict)
    base_profit_score = compute_profit_score(deal_price, category, verdict)
    expected_profit = compute_expected_profit(
        deal_price,
        category=category,
        title=title,
        verdict=verdict,
        is_loot=is_loot,
        is_fresh=is_fresh,
    )

    metrics = {
        "deal_price": float(deal_price or 0.0),
        "category": normalize_category(category),
        "commission_rate": rate,
        "category_cap_inr": cap,
        "estimated_commission_inr": payout,
        "effective_payout_inr": payout,
        "price_tier_cvr_weight": cvr_weight,
        "verdict_multiplier": v_mult,
        "profit_score": base_profit_score,
        "expected_profit_score": expected_profit,
        "min_commission_threshold": min_payout,
        "min_payout_threshold": min_payout,
        "min_profit_score_threshold": min_score,
    }

    # 1. 0%-commission exclusion check
    if is_zero_commission_item(title, category):
        return False, f"Excluded 0%-commission item ({title})", metrics

    # 2. Cooldown check
    if canonical_id:
        can_post_cd, cd_reason = check_deal_cooldown(
            canonical_id=canonical_id,
            current_price=deal_price,
            db_path=db_path,
            cooldown_hours=cd_hours,
            drop_override_pct=drop_override,
        )
        if not can_post_cd:
            return False, cd_reason, metrics

    # 3. Minimum expected payout check
    if payout < min_payout and not is_loot:
        reason = (
            f"Expected payout ₹{payout:.2f} is below threshold ₹{min_payout:.2f} "
            f"({rate*100:.1f}% on ₹{deal_price:.2f}, cap ₹{cap:.0f})"
        )
        return False, reason, metrics

    # 4. Minimum profit score check
    if base_profit_score < min_score and not is_loot:
        reason = (
            f"Profit score {base_profit_score:.2f} is below threshold {min_score:.2f} "
            f"(verdict multiplier {v_mult})"
        )
        return False, reason, metrics

    reason = (
        f"Meets profitability thresholds (Expected Payout: ₹{payout:.2f}, "
        f"Profit Score: {base_profit_score:.2f})"
    )
    return True, reason, metrics
