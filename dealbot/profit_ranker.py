# dealbot/profit_ranker.py
"""Affiliate Profitability and Conversion Ranking Engine for DealBot.

Handles:
1. Category-specific Indian affiliate commission rates (Amazon / Flipkart standard rates).
2. Estimated commission calculation (deal_price * category_rate).
3. Honest verdict multiplier applied to commission to compute profit_score.
4. Profitability gating (should_publish) enforcing minimum commission and score thresholds.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger("dealbot.profit_ranker")

# Standard Indian Affiliate Commission Rates by Category
# Sourced from Amazon Associates India & Flipkart Affiliate commission schedules
CATEGORY_COMMISSION_RATES: Dict[str, float] = {
    "fashion": 0.08,
    "apparel": 0.08,
    "clothing": 0.08,
    "shoes": 0.08,
    "footwear": 0.08,
    "beauty": 0.08,
    "personal_care": 0.08,
    "grooming": 0.08,
    "luxury_beauty": 0.08,
    "home": 0.06,
    "kitchen": 0.06,
    "appliances": 0.06,
    "furniture": 0.06,
    "home_decor": 0.06,
    "toys": 0.06,
    "kids": 0.06,
    "baby": 0.06,
    "grocery": 0.05,
    "gourmet": 0.05,
    "books": 0.05,
    "fitness": 0.04,
    "sports": 0.04,
    "electronics": 0.015,
    "audio": 0.015,
    "headphones": 0.015,
    "earbuds": 0.015,
    "smartwatches": 0.015,
    "cameras": 0.015,
    "laptops": 0.015,
    "computers": 0.015,
    "mobiles": 0.015,
    "smartphones": 0.015,
    "default": 0.03,
}

# Honest Verdict Multipliers
# Strong verified deals get boosted; fake/inflated or normal price drops score to 0
VERDICT_MULTIPLIERS: Dict[str, float] = {
    "screamer": 1.6,         # All-Time Low (>= 14 days, >= 12% drop vs 90d median)
    "all_time_low": 1.6,
    "good": 1.2,             # Good Buy (>= 7 days, >= 10% drop vs 90d median)
    "neutral": 0.9,          # Newly tracked (< 3 days)
    "wait": 0.0,             # Normal price / wait or set alert
    "fake": 0.0,             # Fake or inflated baseline MRP
    "inflated": 0.0,
}


def normalize_category(category: Optional[str]) -> str:
    """Normalize input category string to standard category key."""
    if not category:
        return "default"
    c = str(category).strip().lower().replace("&", " ").replace("-", " ")
    words = c.split()
    for word in words:
        if word in CATEGORY_COMMISSION_RATES:
            return word
    # Substring check
    for cat_key in CATEGORY_COMMISSION_RATES:
        if cat_key in c:
            return cat_key
    return "default"


def get_category_commission_rate(category: Optional[str]) -> float:
    """Lookup affiliate commission rate for Indian e-commerce categories."""
    norm = normalize_category(category)
    return CATEGORY_COMMISSION_RATES.get(norm, CATEGORY_COMMISSION_RATES["default"])


def compute_estimated_commission(
    deal_price: float,
    category: Optional[str] = "default",
    custom_rate: Optional[float] = None,
) -> float:
    """Compute estimated affiliate commission in INR.

    Formula: deal_price * category_commission_rate
    """
    price = max(0.0, float(deal_price or 0.0))
    if price <= 0.0:
        return 0.0
    rate = custom_rate if custom_rate is not None else get_category_commission_rate(category)
    return round(price * rate, 2)


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

    # Direct tier match
    if tier in VERDICT_MULTIPLIERS:
        return VERDICT_MULTIPLIERS[tier]

    # Keyword matching on text
    if "all-time low" in v_text or "screamer" in v_text:
        return 1.6
    if "good buy" in v_text:
        return 1.2
    if "newly tracked" in v_text:
        return 0.9
    if "normal price" in v_text or "wait" in v_text:
        return 0.0
    if "fake" in v_text or "inflated" in v_text:
        return 0.0

    return 1.0


def compute_profit_score(
    deal_price: float,
    category: Optional[str] = "default",
    verdict: Union[str, Dict[str, Any], None] = None,
    custom_rate: Optional[float] = None,
) -> float:
    """Compute overall profit score for deal ranking.

    Formula: estimated_commission_inr * verdict_multiplier
    """
    commission = compute_estimated_commission(deal_price, category, custom_rate=custom_rate)
    multiplier = get_verdict_multiplier(verdict)
    return round(commission * multiplier, 2)


def should_publish(
    deal_price: float,
    category: Optional[str] = "default",
    verdict: Union[str, Dict[str, Any], None] = None,
    min_commission: Optional[float] = None,
    min_profit_score: Optional[float] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Profitability gate checking whether deal meets publishing thresholds.

    Thresholds configurable via environment variables:
    - MIN_EST_COMMISSION_INR (default: 40.0)
    - MIN_PROFIT_SCORE (default: 50.0)

    Returns:
        (passes: bool, reason: str, metrics: Dict[str, Any])
    """
    min_comm = (
        min_commission
        if min_commission is not None
        else float(os.getenv("MIN_EST_COMMISSION_INR", "40.0"))
    )
    min_score = (
        min_profit_score
        if min_profit_score is not None
        else float(os.getenv("MIN_PROFIT_SCORE", "50.0"))
    )

    rate = get_category_commission_rate(category)
    commission = compute_estimated_commission(deal_price, category)
    multiplier = get_verdict_multiplier(verdict)
    profit_score = round(commission * multiplier, 2)

    metrics = {
        "deal_price": float(deal_price or 0.0),
        "category": normalize_category(category),
        "commission_rate": rate,
        "estimated_commission_inr": commission,
        "verdict_multiplier": multiplier,
        "profit_score": profit_score,
        "min_commission_threshold": min_comm,
        "min_profit_score_threshold": min_score,
    }

    # 1. Check minimum commission
    if commission < min_comm:
        reason = (
            f"Estimated commission ₹{commission:.2f} is below threshold ₹{min_comm:.2f} "
            f"({rate*100:.1f}% rate on ₹{deal_price:.2f})"
        )
        return False, reason, metrics

    # 2. Check minimum profit score
    if profit_score < min_score:
        reason = (
            f"Profit score {profit_score:.2f} is below threshold {min_score:.2f} "
            f"(verdict multiplier {multiplier})"
        )
        return False, reason, metrics

    reason = (
        f"Meets profitability thresholds (Commission: ₹{commission:.2f}, "
        f"Profit Score: {profit_score:.2f})"
    )
    return True, reason, metrics
