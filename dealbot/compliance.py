# dealbot/compliance.py
"""Amazon Associates Compliance & BharatBazaro Schema Exporter.

Handles:
1. IST timestamp disclaimer ("Price & availability accurate as of <DD MMM, HH:MM IST>")
   required by Amazon Associates India Operating Agreement.
2. Webhook / JSON exporter converting deals into the BharatBazaro live_deals.json schema.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import requests

logger = logging.getLogger("dealbot.compliance")

IST = timezone(timedelta(hours=5, minutes=30))


def format_compliance_disclaimer(now: Optional[datetime] = None) -> str:
    """Generate Amazon Associates compliance disclaimer with IST timestamp.

    Format: 'Price & availability accurate as of DD MMM, HH:MM IST'
    Example: 'Price & availability accurate as of 19 Nov, 22:00 IST'
    """
    dt = (now or datetime.now(IST)).astimezone(IST)
    ts_str = dt.strftime("%d %b, %H:%M IST")
    return f"<i>Price & availability accurate as of {ts_str}. Subject to change.</i>"


def append_compliance_disclaimer(caption: str, now: Optional[datetime] = None) -> str:
    """Append Amazon Associates compliance disclaimer to a deal message caption."""
    disclaimer = format_compliance_disclaimer(now)
    base = (caption or "").rstrip()
    if not base:
        return disclaimer
    return f"{base}\n\n{disclaimer}"


def to_bharatbazaro_schema(
    deal_or_dict: Any,
    source: str = "telegram",
    verdict: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert Deal object or raw dictionary into standard BharatBazaro schema."""
    raw: Dict[str, Any] = {}
    if hasattr(deal_or_dict, "__dict__"):
        raw = {k: v for k, v in deal_or_dict.__dict__.items() if not k.startswith("_")}
    elif isinstance(deal_or_dict, dict):
        raw = dict(deal_or_dict)

    asin = str(raw.get("asin") or "").strip().upper()
    if not asin and str(raw.get("id") or "").startswith("amz-"):
        asin = str(raw.get("id")).replace("amz-", "").strip().upper()

    title = str(raw.get("title") or "").strip()
    price = float(raw.get("effective_price") or raw.get("current_price") or raw.get("price") or 0.0)
    mrp = float(raw.get("mrp") or raw.get("original_price") or raw.get("list_price") or 0.0)
    discount = int(raw.get("discount_percent") or raw.get("discount_pct") or 0)
    if mrp > price > 0 and not discount:
        discount = int(round((mrp - price) / mrp * 100))

    now_ist = datetime.now(IST)
    verified_at = raw.get("verified_at") or now_ist.isoformat()

    v_info = verdict or {}
    item = {
        "id": f"amazon:{asin}" if asin else str(raw.get("id") or "deal-unknown"),
        "asin": asin or None,
        "title": title,
        "brand": raw.get("brand") or "Generic",
        "brand_tier": raw.get("brand_tier") or "tier2",
        "category": raw.get("category") or "general",
        "image_url": raw.get("image_url") or "",
        "affiliate_url": raw.get("affiliate_url") or raw.get("url") or "",
        "mrp": mrp or price,
        "list_price": price,
        "effective_price": price,
        "coupon_discount": raw.get("coupon_discount"),
        "coupon_text": raw.get("coupon_text"),
        "promo_code": raw.get("promo_code") or raw.get("coupon_code"),
        "bank_offer": raw.get("bank_offer"),
        "discount_pct": discount,
        "effective_discount_pct": discount,
        "savings_inr": max(0.0, mrp - price) if mrp > price else 0.0,
        "rating": float(raw.get("rating") or 4.0),
        "review_count": int(raw.get("review_count") or 100),
        "score": int(raw.get("score") or 75),
        "buy_confidence": int(raw.get("buy_confidence") or 90),
        "is_loot": bool(raw.get("is_loot", False)),
        "is_fresh": True,
        "verified_at": verified_at,
        "updated_at_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "campaign_badge": raw.get("campaign_badge") or "VERIFIED DROP",
        "campaign_cta": raw.get("campaign_cta") or "Grab Deal on Amazon",
        "verdict": v_info.get("verdict") or raw.get("verdict") or "Verified Deal",
        "badge_tier": v_info.get("badge_tier") or raw.get("badge_tier") or "good",
        "source": source,
    }
    return item


def export_deal_to_bharatbazaro(
    deal_or_dict: Any,
    output_dir: Optional[Union[str, Path]] = None,
    webhook_url: Optional[str] = None,
    source: str = "telegram",
    verdict: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Export deal to BharatBazaro live_deals schema (JSON file and/or webhook).

    Returns:
        The standardized BharatBazaro deal dictionary.
    """
    item = to_bharatbazaro_schema(deal_or_dict, source=source, verdict=verdict)

    # 1. File export if output directory or BHARATBAZARO_DATA_DIR is provided
    out_dir = output_dir or os.getenv("BHARATBAZARO_DATA_DIR")
    if out_dir:
        dir_path = Path(out_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        live_deals_path = dir_path / "live_deals.json"
        existing_deals: List[Dict[str, Any]] = []
        if live_deals_path.exists():
            try:
                with open(live_deals_path, "r", encoding="utf-8") as f:
                    existing_deals = json.load(f)
            except Exception as e:
                logger.warning("Could not read existing %s: %s", live_deals_path, e)

        # Prepend new deal and deduplicate by ASIN/id
        by_id = {item.get("asin") or item.get("id"): item}
        for d in existing_deals:
            key = d.get("asin") or d.get("id")
            if key not in by_id:
                by_id[key] = d

        with open(live_deals_path, "w", encoding="utf-8") as f:
            json.dump(list(by_id.values())[:300], f, indent=2, ensure_ascii=False)
        logger.info("Saved deal to BharatBazaro storefront: %s", live_deals_path)

    # 2. Webhook POST if webhook_url or BHARATBAZARO_WEBHOOK_URL is configured
    wh_url = webhook_url or os.getenv("BHARATBAZARO_WEBHOOK_URL", "").strip()
    if wh_url:
        try:
            resp = requests.post(wh_url, json=item, timeout=5)
            logger.info("BharatBazaro webhook POST %s -> status %d", wh_url, resp.status_code)
        except requests.RequestException as e:
            logger.warning("BharatBazaro webhook failed (%s): %s", wh_url, e)

    return item
