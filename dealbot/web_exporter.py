"""
Web Feed Exporter for BharatBazaro.com and DealDash AI.
Exports high-value verified live deals and statistics to data/live_deals.json and data/stats.json.
"""

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from dealbot.models import Deal, parse_rupees
from dealbot.agent import (
    TIER_1_BRANDS,
    TIER_2_BRANDS,
    get_brand_tier,
    classify_category,
    compute_buy_confidence,
)

logger = logging.getLogger(__name__)

AMAZON_ASIN_REGEX = re.compile(r"/(?:dp|gp/product|exec/obidos/ASIN|aw/d)/([A-Z0-9]{10})", re.IGNORECASE)


def extract_asin(deal_or_url: Union[str, Deal]) -> Optional[str]:
    """Extract clean 10-char uppercase Amazon ASIN."""
    if isinstance(deal_or_url, Deal):
        if deal_or_url.id and deal_or_url.id.startswith("amz-"):
            clean = deal_or_url.id.replace("amz-", "").strip().upper()
            if len(clean) == 10:
                return clean
        if getattr(deal_or_url, "canonical_product_id", None) and deal_or_url.canonical_product_id.startswith("amazon:"):
            clean = deal_or_url.canonical_product_id.replace("amazon:", "").strip().upper()
            if len(clean) == 10:
                return clean
        raw_url = deal_or_url.url or ""
    else:
        raw_url = str(deal_or_url or "")

    m = AMAZON_ASIN_REGEX.search(raw_url)
    if m:
        return m.group(1).upper()
    m2 = re.search(r"\b([A-Z0-9]{10})\b", raw_url)
    if m2 and re.match(r"^[A-Z0-9]{10}$", m2.group(1)):
        return m2.group(1).upper()
    return None


def detect_brand(title: str) -> Optional[str]:
    """Extract brand name from product title."""
    if not title:
        return None
    lower_title = title.lower()
    # Check Tier-1 first
    for b in TIER_1_BRANDS:
        if re.search(r"\b" + re.escape(b) + r"\b", lower_title):
            return b.title()
    # Check Tier-2
    for b in TIER_2_BRANDS:
        if re.search(r"\b" + re.escape(b) + r"\b", lower_title):
            return b.title()
    # Fallback: first word of title if alphabetic
    first_word = title.split()[0].strip(",.-:()")
    if len(first_word) >= 3 and first_word.isalpha():
        return first_word.title()
    return None


def map_ui_category(deal_category: str, title: str) -> str:
    """Map internal classification category to consumer-facing BharatBazaro category pill."""
    t = (title or "").lower()
    c = (deal_category or "").lower()

    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("headphone", "headphones", "earphone", "earphones", "earbud", "earbuds", "audio", "speaker", "speakers", "soundbar", "tws", "neckband", "bluetooth speaker", "mic", "microphone")):
        return "Audio & Headphones"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("cooker", "fryer", "air fryer", "pan", "bottle", "purifier", "vacuum", "mixer", "grinder", "kitchen", "kettle", "bedsheet", "curtain", "furniture", "appliance", "blender", "iron", "toaster")):
        return "Home & Kitchen"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("laptop", "laptops", "macbook", "pc", "desktop", "monitor", "monitors", "keyboard", "mouse", "ssd", "ram", "hard drive", "processor", "gpu", "printer")):
        return "Laptops & Computing"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("phone", "phones", "mobile", "mobiles", "smartphone", "smartphones", "iphone", "galaxy", "case", "cases", "charger", "chargers", "cable", "cables", "tempered glass", "power bank")):
        return "Mobiles & Accessories"
    if c == "home":
        return "Home & Kitchen"
    if c == "fashion" or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("shirt", "t-shirt", "tshirt", "shoe", "shoes", "sneaker", "sneakers", "jeans", "saree", "kurta", "jacket", "sandals", "wallet", "backpack", "watch", "watches")):
        return "Fashion & Footwear"
    if c in ("beauty", "grocery") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("shampoo", "cream", "serum", "perfume", "soap", "tea", "coffee", "biscuit", "almond", "oil", "chocolate", "snack", "diaper", "face wash", "sunscreen", "lotion", "moisturizer")):
        return "Personal Care & Grocery"

    if c == "electronics":
        return "Electronics & Audio"
    return "Electronics & Audio"


def deal_to_web_json(
    deal: Deal,
    score: int = 70,
    price_sightings: Optional[Dict[str, Any]] = None,
    partner_tag: str = "dammaiprasad-21",
    posted_to_telegram: bool = True,
    expired: bool = False,
    price_spiked: bool = False,
) -> Dict[str, Any]:
    """Transform an internal Deal object into the standard BharatBazaro live_deals.json item."""
    asin = extract_asin(deal)
    if not asin:
        asin = "B0UNKNOWN0"

    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    updated_at_epoch = int(time.time())
    updated_at_ist = now_ist.strftime("%Y-%m-%d %H:%M IST")

    mrp = parse_rupees(deal.original_price)
    list_p = parse_rupees(deal.current_price)
    eff_p = getattr(deal, "effective_price", None) or list_p or 0
    if list_p and not eff_p:
        eff_p = list_p

    coupon_disc = getattr(deal, "coupon_discount", None)
    coupon_text = f"Apply ₹{coupon_disc:,} on-page coupon" if coupon_disc else None
    promo_code = getattr(deal, "promo_code", None) or deal.coupon_code

    disc_pct = deal.discount_percent
    eff_disc_pct = getattr(deal, "effective_discount_pct", None) or disc_pct
    if mrp and eff_p and mrp > eff_p > 0 and not eff_disc_pct:
        eff_disc_pct = int(round((mrp - eff_p) / mrp * 100))

    savings_inr = (mrp - eff_p) if (mrp and eff_p and mrp > eff_p) else 0

    brand = detect_brand(deal.title or "")
    b_tier_int = get_brand_tier(deal.title or "")
    tier_map = {1: "tier1", 2: "tier2", 3: "tier3"}
    brand_tier = tier_map.get(b_tier_int, "unbranded")

    ui_category = map_ui_category(getattr(deal, "category", "") or "", deal.title or "")

    # Historical price context from price_sightings
    h_min, h_avg, h_max = None, None, None
    if price_sightings:
        h_min = price_sightings.get("min_price")
        h_avg = int(round(price_sightings.get("avg_price", 0))) or None
        h_max = price_sightings.get("max_price")

    price_ctx = getattr(deal, "price_context", None) or "normal"
    if not price_ctx or price_ctx == "normal":
        if h_min and eff_p <= h_min:
            price_ctx = "all_time_low"
        elif h_avg and eff_p < int(h_avg * 0.9):
            price_ctx = "real_drop"

    buy_conf = getattr(deal, "buy_confidence", None)
    if not buy_conf:
        buy_conf = compute_buy_confidence(deal, score=score)

    is_loot = bool(
        getattr(deal, "is_loot", False)
        or (score >= 80 and (eff_disc_pct or 0) >= 30)
        or score >= 90
    )

    clean_aff_url = f"https://www.amazon.in/dp/{asin}?tag={partner_tag}&linkCode=ll1"
    image_url = deal.image_url or f"https://m.media-amazon.com/images/P/{asin}.jpg"

    return {
        "id": f"amazon:{asin}",
        "asin": asin,
        "title": deal.title or "Amazon Special Deal",
        "brand": brand,
        "brand_tier": brand_tier,
        "category": ui_category,
        "image_url": image_url,
        "affiliate_url": clean_aff_url,
        "mrp": mrp,
        "list_price": list_p,
        "effective_price": eff_p,
        "coupon_discount": coupon_disc,
        "coupon_text": coupon_text,
        "promo_code": promo_code,
        "bank_offer": deal.bank_offer,
        "discount_pct": disc_pct,
        "effective_discount_pct": eff_disc_pct,
        "savings_inr": savings_inr,
        "rating": getattr(deal, "rating", None),
        "review_count": getattr(deal, "review_count", None),
        "score": score,
        "buy_confidence": buy_conf,
        "price_context": price_ctx,
        "historical_min": h_min,
        "historical_avg": h_avg,
        "historical_max": h_max,
        "is_loot": is_loot,
        "is_further_drop": getattr(deal, "is_further_price_drop", False),
        "source_count": getattr(deal, "source_count", 1),
        "expired": expired,
        "price_spiked": price_spiked,
        "posted_to_telegram": posted_to_telegram,
        "updated_at": updated_at_epoch,
        "updated_at_ist": updated_at_ist,
    }


def export_web_feed(
    store,
    current_cycle_deals: Optional[List[Deal]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    max_items: int = 120,
) -> Dict[str, Any]:
    """Export current live verified deals to live_deals.json and stats.json."""
    if output_dir is None:
        output_dir = Path("data")
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    partner_tag = (
        os.getenv("AMAZON_AFFILIATE_TAG")
        or os.getenv("AMAZON_PARTNER_TAG")
        or "dammaiprasad-21"
    ).strip()

    # Load price sightings for historical stats lookup
    sightings: Dict[str, Dict[str, Any]] = {}
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT canonical_id, min_price, max_price, avg_price FROM price_sightings")
            for row in cur.fetchall():
                sightings[row["canonical_id"]] = dict(row)
        except sqlite3.OperationalError:
            pass

    deals_by_asin: Dict[str, Dict[str, Any]] = {}

    # 1. Ingest deals from SQLite posted_deals (active, within last 48 hours)
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT deal_id, retailer, chat_id, message_id, caption, status, posted_at,
                   category, discount, agent_score, agent_reason, canonical_product_id, price
            FROM posted_deals
            WHERE status = 'active'
              AND posted_at >= datetime('now', '-48 hours')
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (max_items * 2,),
        )
        for row in cur.fetchall():
            d_dict = dict(row)
            deal_id = d_dict["deal_id"]
            canon_id = d_dict.get("canonical_product_id") or ""
            asin = None
            if deal_id.startswith("amz-"):
                asin = deal_id.replace("amz-", "").strip().upper()
            elif canon_id.startswith("amazon:"):
                asin = canon_id.replace("amazon:", "").strip().upper()

            if not asin or len(asin) != 10:
                continue

            # Extract title from caption
            caption = d_dict.get("caption") or ""
            title = ""
            for line in caption.split("\n"):
                m = re.match(r"^<b>([^<]+)</b>$", line.strip())
                if m and "deal" not in m.group(1).lower() and "loot" not in m.group(1).lower():
                    title = m.group(1).strip()
                    break
            if not title:
                m_sub = re.search(r"<b>([^<]{5,80})</b>", caption)
                title = m_sub.group(1).strip() if m_sub else f"Amazon Deal ({asin})"

            # Extract coupon discount if present in caption
            coupon_m = re.search(r"Apply ₹([\d,]+) on-page coupon", caption)
            coupon_disc = parse_rupees(coupon_m.group(1)) if coupon_m else None

            # Extract promo code if present
            promo_m = re.search(r"<code>([A-Za-z0-9_-]+)</code>", caption)
            promo_code = promo_m.group(1) if promo_m else None

            # Extract price, mrp, bank offer from caption if missing from database columns
            m_price = re.search(r"<b>₹([\d,]+)</b>", caption)
            m_eff = re.search(r"Effective Price:\s*₹?([\d,]+)", caption)
            m_mrp = re.search(r"MRP\s*<(?:s|del)>₹?([\d,]+)</(?:s|del)>", caption)
            m_disc = re.search(r"<b>(\d{1,2})%\s*OFF</b>", caption)
            m_bank = re.search(r"Bank offer:\s*([^\n<]+)", caption)

            extracted_price = parse_rupees(m_price.group(1)) if m_price else None
            extracted_eff = parse_rupees(m_eff.group(1)) if m_eff else None
            mrp_price = parse_rupees(m_mrp.group(1)) if m_mrp else None
            discount_pct = d_dict.get("discount") or (int(m_disc.group(1)) if m_disc else None)
            bank_offer = m_bank.group(1).strip() if m_bank else None
            final_price = d_dict.get("price") or extracted_eff or extracted_price

            deal_obj = Deal(
                id=deal_id,
                retailer="Amazon",
                title=title,
                url=f"https://www.amazon.in/dp/{asin}?tag={partner_tag}&linkCode=ll1",
                current_price=f"₹{final_price}" if final_price else None,
                original_price=f"₹{mrp_price}" if mrp_price else None,
                discount_percent=discount_pct,
                canonical_product_id=f"amazon:{asin}",
                category=d_dict.get("category"),
                coupon_code=promo_code,
                bank_offer=bank_offer,
            )
            if coupon_disc:
                deal_obj.coupon_discount = coupon_disc
                deal_obj.effective_price = max(1, (final_price or 0) - coupon_disc)
            elif extracted_eff:
                deal_obj.effective_price = extracted_eff

            score = d_dict.get("agent_score") or 70
            s_ctx = sightings.get(f"amazon:{asin}") or sightings.get(deal_id)
            w_json = deal_to_web_json(
                deal_obj,
                score=score,
                price_sightings=s_ctx,
                partner_tag=partner_tag,
                posted_to_telegram=bool(d_dict.get("message_id")),
                expired=(d_dict.get("status") == "expired"),
                price_spiked=(d_dict.get("status") == "price_spiked"),
            )
            deals_by_asin[asin] = w_json

    # 2. Layer in freshly verified deals from the current cycle (taking precedence with freshest data)
    if current_cycle_deals:
        for d in current_cycle_deals:
            asin = extract_asin(d)
            if not asin or len(asin) != 10:
                continue
            s_ctx = sightings.get(f"amazon:{asin}") or sightings.get(d.id)
            score = getattr(d, "agent_score", None) or 75
            w_json = deal_to_web_json(
                d,
                score=score,
                price_sightings=s_ctx,
                partner_tag=partner_tag,
                posted_to_telegram=False,  # default until posted
            )
            deals_by_asin[asin] = w_json

    # Sort final items by score DESC, then buy_confidence DESC, then updated_at DESC
    deal_list = list(deals_by_asin.values())
    deal_list.sort(key=lambda x: (x.get("score") or 0, x.get("buy_confidence") or 0, x.get("updated_at") or 0), reverse=True)
    final_deals = deal_list[:max_items]

    # Write data/live_deals.json
    live_deals_path = output_dir / "live_deals.json"
    with open(live_deals_path, "w", encoding="utf-8") as f:
        json.dump(final_deals, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported {len(final_deals)} deals to {live_deals_path}")

    # Compute stats
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    stats = {
        "last_updated_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "last_updated_epoch": int(time.time()),
        "total_live_deals": len(final_deals),
        "loot_count": sum(1 for d in final_deals if d.get("is_loot")),
        "coupon_deals_count": sum(1 for d in final_deals if d.get("coupon_discount") or d.get("promo_code")),
        "max_discount_pct": max((d.get("effective_discount_pct") or d.get("discount_pct") or 0 for d in final_deals), default=0),
        "total_savings_available": sum(d.get("savings_inr") or 0 for d in final_deals),
    }

    stats_path = output_dir / "stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported stats to {stats_path}")

    return {"deals_count": len(final_deals), "stats": stats}
