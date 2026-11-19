# dealbot/sanitizers.py
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Strip forum/RSS noise like "Rs.99 @ Amazon", "@Amazon.in", "[Loot]", etc.
NOISE_PATTERNS = [
    re.compile(r"\bRs\.?\s*\d[\d,]*(?:\.\d+)?\s*@\s*Amazon(?:\.in)?\b", re.IGNORECASE),
    re.compile(r"\s*@\s*Amazon(?:\.in)?\b", re.IGNORECASE),
    re.compile(r"^\s*(?:\[[^\]]+\]\s*)+"),
]

# Prevent obvious cross-category leakage into electronics/mobiles
CATEGORY_BLOCKLIST: Dict[str, tuple[str, ...]] = {
    "mobiles": (
        "hdmi", "fairy light", "string light", "mountain cycle", "bicycle",
        "perfume", "deo", "face wash", "body wash", "keyboard", "mouse",
        "monitor", "gaming chair", "curtain", "bedsheet",
    ),
    "electronics": (
        "mountain cycle", "bicycle", "t-shirt", "kurta", "saree",
        "face wash", "body wash", "perfume", "almonds", "grocery",
        "fairy light", "string light",
    ),
}

# Date-bounded seasonal campaigns (IST). Outside these windows, fallback is neutral.
CAMPAIGNS = [
    {
        "id": "republic_day",
        "start": "2026-01-13T00:00:00+05:30",
        "end": "2026-01-26T23:59:59+05:30",
        "badge": "REPUBLIC DAY DEAL",
        "cta": "Grab Republic Day Deal on Amazon",
    },
    {
        "id": "prime_day",
        "start": "2026-07-10T00:00:00+05:30",
        "end": "2026-07-16T23:59:59+05:30",
        "badge": "PRIME DEAL",
        "cta": "Grab Prime Deal on Amazon",
    },
    {
        "id": "diwali",
        "start": "2026-10-15T00:00:00+05:30",
        "end": "2026-11-12T23:59:59+05:30",
        "badge": "DIWALI STEAL",
        "cta": "Grab Diwali Deal on Amazon",
    },
]


def get_active_campaign(now: Optional[datetime] = None) -> Dict[str, str]:
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    for camp in CAMPAIGNS:
        start = datetime.fromisoformat(camp["start"])
        end = datetime.fromisoformat(camp["end"])
        if start <= now_ist <= end:
            return {"id": camp["id"], "badge": camp["badge"], "cta": camp["cta"]}
    return {
        "id": "evergreen",
        "badge": "VERIFIED DROP",
        "cta": "Grab Deal on Amazon",
    }


def clean_title(raw_title: str) -> str:
    title = (raw_title or "").strip()
    for pattern in NOISE_PATTERNS:
        title = pattern.sub("", title)
    # Collapse extra spaces & strip dangling unclosed brackets from bad truncation
    title = re.sub(r"\s{2,}", " ", title).strip(" -|:,")
    if title.count("(") > title.count(")"):
        title = re.sub(r"\s*\([^)]*$", "", title).strip()
    return title


def compute_discount_pct(effective_price: float, baseline_price: float) -> int:
    if effective_price <= 0 or baseline_price <= effective_price:
        return 0
    pct = round(((baseline_price - effective_price) / baseline_price) * 100)
    return max(0, min(99, pct))


def compute_honest_verdict(
    effective_price: float,
    median_90d: Optional[float],
    days_tracked: int,
    is_historical_min: bool,
) -> Dict[str, Any]:
    """
    Avoids fake '95% Confidence / All-Time Low' on every card.
    Requires >= 14 days of price history before calling All-Time Low.
    """
    if median_90d and median_90d > 0:
        drop_vs_median = round(((median_90d - effective_price) / median_90d) * 100)
    else:
        drop_vs_median = 0

    if days_tracked >= 14 and is_historical_min and drop_vs_median >= 12:
        return {
            "verdict": f"All-Time Low ({drop_vs_median}% below 90d median)",
            "badge_tier": "screamer",
            "drop_vs_median_pct": drop_vs_median,
        }
    if days_tracked >= 7 and drop_vs_median >= 10:
        return {
            "verdict": f"{drop_vs_median}% below avg price — Good Buy",
            "badge_tier": "good",
            "drop_vs_median_pct": drop_vs_median,
        }
    if days_tracked < 3:
        return {
            "verdict": "Newly tracked — verify price chart",
            "badge_tier": "neutral",
            "drop_vs_median_pct": drop_vs_median,
        }
    return {
        "verdict": "Normal price — Wait or set alert",
        "badge_tier": "wait",
        "drop_vs_median_pct": drop_vs_median,
    }


def is_valid_category_item(category: str, title: str) -> bool:
    cat = (category or "").strip().lower()
    lower_title = (title or "").lower()
    blocked_terms = CATEGORY_BLOCKLIST.get(cat, ())
    return not any(term in lower_title for term in blocked_terms)


def sanitize_and_dedupe_deals(
    deals: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    campaign = get_active_campaign(now)
    by_asin: Dict[str, Dict[str, Any]] = {}

    for raw in deals:
        asin = str(raw.get("asin") or "").strip().upper()
        if not asin:
            continue

        title = clean_title(str(raw.get("title") or ""))
        if not title:
            continue

        price = float(raw.get("effective_price") or raw.get("price") or 0)
        mrp = float(raw.get("mrp") or raw.get("list_price") or 0)
        median_90d = float(raw.get("median_90d") or 0) or None
        if price <= 0:
            continue

        # Prefer real median baseline when available; never trust mismatched scraped %
        baseline = median_90d if (median_90d and median_90d > price) else mrp
        real_discount = compute_discount_pct(price, mrp if mrp > price else baseline)

        category = str(raw.get("category") or "general").strip().lower()
        if not is_valid_category_item(category, title):
            # Re-route leaked items out of strict tech categories instead of polluting them
            category = "general"

        verdict_info = compute_honest_verdict(
            effective_price=price,
            median_90d=median_90d,
            days_tracked=int(raw.get("days_tracked") or 0),
            is_historical_min=bool(raw.get("is_historical_min")),
        )

        cleaned = {
            **raw,
            "asin": asin,
            "title": title,
            "category": category,
            "effective_price": price,
            "mrp": mrp,
            "discount_pct": real_discount,
            "campaign_badge": campaign["badge"],
            "campaign_cta": campaign["cta"],
            **verdict_info,
        }

        # Deduplicate strictly by ASIN, keeping newest verified record
        existing = by_asin.get(asin)
        if not existing or str(cleaned.get("verified_at", "")) > str(existing.get("verified_at", "")):
            by_asin[asin] = cleaned

    return list(by_asin.values())
