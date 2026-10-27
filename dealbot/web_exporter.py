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
from typing import Any, Dict, List, Optional, Tuple, Union

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


def resolve_telegram_url() -> str:
    """
    Resolve the real Telegram group/channel URL dynamically.
    Checks env vars first, then queries Telegram Bot API getChat/exportChatInviteLink,
    falls back to verified @Flash_Deals_Flipkart_Amazon channel.
    """
    direct_env = (
        os.getenv("TELEGRAM_CHANNEL_URL")
        or os.getenv("TELEGRAM_INVITE_LINK")
        or os.getenv("TELEGRAM_GROUP_URL")
    )
    if direct_env and direct_env.strip():
        return direct_env.strip()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if token and token != "replace_me" and chat_id:
        try:
            import requests
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": chat_id},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json().get("result", {})
                if data.get("username"):
                    return f"https://t.me/{data['username']}"
                if data.get("invite_link"):
                    return data["invite_link"]
                # Try exportChatInviteLink if private
                resp2 = requests.get(
                    f"https://api.telegram.org/bot{token}/exportChatInviteLink",
                    params={"chat_id": chat_id},
                    timeout=5,
                )
                if resp2.status_code == 200:
                    link = resp2.json().get("result")
                    if link:
                        return link
        except Exception as e:
            logger.debug(f"Failed to query Telegram Bot API for chat URL: {e}")

    if chat_id and chat_id.startswith("@"):
        return f"https://t.me/{chat_id.lstrip('@')}"

    return "https://t.me/Flash_Deals_Flipkart_Amazon"


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
        if b == "apple" and re.search(r"\b(green\s+apple|apple\s+cider|apple\s+juice|apple\s+flavo?ur)\b", lower_title):
            continue
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


def extract_deal_price(caption: str, db_price: Optional[int] = None) -> Optional[int]:
    """Robustly extract deal price from DB column or various caption formats."""
    if db_price and db_price > 0:
        return db_price
    if not caption:
        return None
    for pat in [
        r"Effective\s+Price:\s*₹?([\d,]+)",
        r"Deal\s+Price:\s*</b>?\s*₹?([\d,]+)",
        r"💰\s*(?:<b>)?(?:Deal\s+Price:\s*)?₹?([\d,]+)",
        r"<b>₹([\d,]+)</b>",
        r"(?:Price|List\s+Price):\s*₹?([\d,]+)",
        r"₹([\d,]+)",
    ]:
        m = re.search(pat, caption, re.I)
        if m:
            val = parse_rupees(m.group(1))
            if val and val > 0:
                return val
    return None


def extract_mrp(caption: str) -> Optional[int]:
    """Robustly extract MRP from various caption formats."""
    if not caption:
        return None
    for pat in [
        r"MRP[:\s]*</b>?\s*<(?:s|del)>₹?([\d,]+)</(?:s|del)>",
        r"<(?:s|del)>\s*MRP[:\s]*₹?([\d,]+)</(?:s|del)>",
        r"<(?:s|del)>₹?([\d,]+)</(?:s|del)>",
        r"MRP[:\s]*₹?([\d,]+)",
    ]:
        m = re.search(pat, caption, re.I)
        if m:
            val = parse_rupees(m.group(1))
            if val and val > 0:
                return val
    return None


def extract_discount_pct(caption: str, price: Optional[int], mrp: Optional[int]) -> Optional[int]:
    """Robustly extract discount percent from caption or calculate from price/MRP."""
    if caption:
        m = re.search(r"(\d{1,2})%\s*OFF", caption, re.I)
        if m:
            return int(m.group(1))
    if mrp and price and mrp > price > 0:
        return int(round((mrp - price) / mrp * 100))
    return None


def extract_bank_offer(text: str) -> Optional[str]:
    """Extract clean bank offer descriptions from title, caption, or deal text."""
    if not text:
        return None
    # Check for bracketed bank tags e.g. [ICICI CC], [AXIS/ICICI CC EMI], [HDFC Bank]
    m_bracket = re.search(r"\[([^\]]*(?:ICICI|HDFC|SBI|AXIS|KOTAK|BOB|ONE\s*CARD|FEDERAL|BANK|CC|DC|EMI)[^\]]*)\]", text, re.I)
    if m_bracket:
        raw_tag = m_bracket.group(1).strip()
        return f"Instant Bank Discount with {raw_tag}"
    m_offer = re.search(r"\b(?:Bank offer|Instant Discount|Bank Discount):\s*([^\n<]+)", text, re.I)
    if m_offer:
        return m_offer.group(1).strip()
    m_named = re.search(r"\b((?:ICICI|HDFC|SBI|AXIS|Kotak|BOB|OneCard)\s+(?:Credit|Debit|CC|Cards?|Bank)\b[^\n<,.]*)", text, re.I)
    if m_named:
        return f"Instant Discount with {m_named.group(1).strip()}"
    return None


def extract_coupon_info(text: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Extract coupon discount amount, coupon text, and promo code from text/caption."""
    if not text:
        return None, None, None
    coupon_disc = None
    coupon_text = None
    promo_code = None

    # On-page coupon amount
    m_disc = re.search(r"(?:Apply|Clip|Use|Includes)\s+₹?([\d,]+)\s+(?:on-page\s+)?coupon", text, re.I)
    if not m_disc:
        m_disc = re.search(r"(?:extra|flat|save)\s+₹?([\d,]+)\s+off\s+coupon", text, re.I)
    if m_disc:
        coupon_disc = parse_rupees(m_disc.group(1))
        if coupon_disc:
            coupon_text = f"Apply ₹{coupon_disc:,} on-page coupon"

    # Promo code
    m_code = re.search(r"<code>([A-Za-z0-9_-]+)</code>", text)
    if not m_code:
        m_code = re.search(r"(?:Promo Code|Coupon Code|Use Code)\s*:\s*<b>?([A-Za-z0-9_-]+)</b>?", text, re.I)
    if m_code:
        promo_code = m_code.group(1).strip()

    return coupon_disc, coupon_text, promo_code


def map_ui_category(deal_category: str, title: str) -> str:
    """Map internal classification category to consumer-facing BharatBazaro category pill."""
    t = (title or "").lower()
    c = (deal_category or "").lower()

    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("headphone", "headphones", "earphone", "earphones", "earbud", "earbuds", "audio", "speaker", "speakers", "soundbar", "tws", "neckband", "bluetooth speaker", "mic", "microphone")):
        return "Audio & Headphones"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("cooker", "fryer", "air fryer", "pan", "bottle", "purifier", "vacuum", "mixer", "grinder", "kitchen", "kettle", "bedsheet", "curtain", "furniture", "appliance", "blender", "iron", "toaster", "lunch box", "geyser")):
        return "Home & Kitchen"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("laptop", "laptops", "macbook", "pc", "desktop", "monitor", "monitors", "keyboard", "mouse", "ssd", "ram", "hard drive", "processor", "gpu", "printer", "chromebook")):
        return "Laptops & Computing"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("phone", "phones", "mobile", "mobiles", "smartphone", "smartphones", "iphone", "galaxy", "case", "cases", "charger", "chargers", "cable", "cables", "tempered glass", "power bank")):
        return "Mobiles & Accessories"
    if c == "home":
        return "Home & Kitchen"
    if c == "fashion" or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("shirt", "t-shirt", "tshirt", "shoe", "shoes", "sneaker", "sneakers", "jeans", "saree", "kurta", "jacket", "sandals", "wallet", "backpack", "watch", "watches", "sling bag", "toiletry bag")):
        return "Fashion & Footwear"
    if c in ("beauty", "grocery") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("shampoo", "cream", "serum", "perfume", "soap", "tea", "coffee", "biscuit", "almond", "oil", "chocolate", "snack", "diaper", "face wash", "sunscreen", "lotion", "moisturizer")):
        return "Personal Care & Grocery"

    if c == "electronics":
        return "Electronics & Audio"
    return "Electronics & Audio"


def parse_posted_epoch(posted_at: Union[str, int, float, None]) -> int:
    """Safely convert SQLite datetime string or epoch timestamp to epoch seconds."""
    if not posted_at:
        return int(time.time())
    if isinstance(posted_at, (int, float)):
        return int(posted_at)
    val = str(posted_at).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(val, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        pass
    return int(time.time())


def deal_to_web_json(
    deal: Deal,
    score: int = 70,
    price_sightings: Optional[Dict[str, Any]] = None,
    partner_tag: str = "dammaiprasad-21",
    posted_to_telegram: bool = True,
    expired: bool = False,
    price_spiked: bool = False,
    posted_epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Transform an internal Deal object into the standard BharatBazaro live_deals.json item."""
    asin = extract_asin(deal)
    if not asin:
        asin = "B0UNKNOWN0"

    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    updated_at_epoch = int(time.time())
    updated_at_ist = now_ist.strftime("%Y-%m-%d %H:%M IST")

    # Time-decay freshness ranking (Requirement 2)
    now_epoch = updated_at_epoch
    post_time = posted_epoch or updated_at_epoch
    age_hours = round(max(0.0, (now_epoch - post_time) / 3600.0), 1)
    decay = max(0.0, (age_hours - 2.0) * 2.5)
    trending_score = round(max(10.0, float(score or 70) - decay), 1)
    is_new = bool(age_hours <= 2.0)

    mrp = parse_rupees(deal.original_price)
    list_p = parse_rupees(deal.current_price)
    eff_p = getattr(deal, "effective_price", None) or list_p or 0
    if list_p and not eff_p:
        eff_p = list_p

    coupon_disc = getattr(deal, "coupon_discount", None)
    coupon_text = getattr(deal, "coupon_text", None)
    promo_code = getattr(deal, "promo_code", None) or deal.coupon_code
    bank_offer = deal.bank_offer or extract_bank_offer(deal.title or "")

    # Coupon extraction fallback
    if not coupon_disc:
        extracted_disc, extracted_txt, extracted_promo = extract_coupon_info(deal.title or "")
        if extracted_disc:
            coupon_disc = extracted_disc
            coupon_text = extracted_txt
            if not eff_p or eff_p == list_p:
                eff_p = max(1, (list_p or 0) - coupon_disc)
        if not promo_code and extracted_promo:
            promo_code = extracted_promo

    if coupon_disc and not coupon_text:
        coupon_text = f"Apply ₹{coupon_disc:,} on-page coupon"

    disc_pct = deal.discount_percent
    eff_disc_pct = getattr(deal, "effective_discount_pct", None) or disc_pct
    if mrp and eff_p and mrp > eff_p > 0 and not eff_disc_pct:
        eff_disc_pct = int(round((mrp - eff_p) / mrp * 100))

    brand = detect_brand(deal.title or "")
    b_tier_int = get_brand_tier(deal.title or "")
    tier_map = {1: "tier1", 2: "tier2", 3: "tier3"}
    brand_tier = tier_map.get(b_tier_int, "unbranded")

    # Sanity cap for discount percentage to eliminate fake-MRP unbranded outliers (Requirement 5)
    if brand_tier == "unbranded" and (eff_disc_pct or 0) > 85:
        eff_disc_pct = 85
        if mrp and eff_p:
            mrp = int(round(eff_p / (1 - 0.85)))
    elif (eff_disc_pct or 0) > 89:
        eff_disc_pct = 89
        if mrp and eff_p:
            mrp = int(round(eff_p / (1 - 0.89)))

    if disc_pct and disc_pct > 89 and brand_tier == "unbranded":
        disc_pct = 85

    savings_inr = (mrp - eff_p) if (mrp and eff_p and mrp > eff_p) else 0

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
        elif h_avg and eff_p <= int(h_avg * 0.85):
            price_ctx = "real_drop"
        elif (eff_disc_pct or 0) >= 60 and brand_tier in ("tier1", "tier2"):
            price_ctx = "all_time_low"
        elif (eff_disc_pct or 0) >= 50 and brand_tier == "tier1":
            price_ctx = "real_drop"

    buy_conf = getattr(deal, "buy_confidence", None)
    if not buy_conf:
        buy_conf = compute_buy_confidence(deal, score=score)

    # Calibrated is_loot (Requirement 5):
    # Only genuine screamers (~15-25% of live deals):
    # - Tier-1 brands with >= 55% discount and >= ₹1,200 savings (score >= 82)
    # - Tier-2 brands with >= 65% discount and >= ₹2,000 savings (score >= 82)
    # - Deals with score >= 84 and massive savings >= ₹4,000
    # - Elite deals with score >= 88, discount >= 50%, and savings >= ₹2,000
    is_loot = bool(
        (brand_tier == "tier1" and (eff_disc_pct or 0) >= 55 and savings_inr >= 1200 and score >= 82)
        or (brand_tier == "tier2" and (eff_disc_pct or 0) >= 65 and savings_inr >= 2000 and score >= 82)
        or (score >= 84 and savings_inr >= 4000)
        or (score >= 88 and (eff_disc_pct or 0) >= 50 and savings_inr >= 2000)
        or (getattr(deal, "is_loot", False) and score >= 82 and (eff_disc_pct or 0) >= 40)
    )

    extra_offers = list(getattr(deal, "extra_offers", []) or [])

    # Build Step-by-Step "How to Claim" Checklist (Requirement 3)
    claim_steps = []
    claim_steps.append("🛒 Click 'Grab Deal' to open the official Amazon India product page.")
    if coupon_disc or coupon_text:
        c_label = coupon_text or f"Apply ₹{coupon_disc:,} coupon"
        claim_steps.append(f"☑️ Look below the price on Amazon and check the '{c_label}' box.")
    if promo_code:
        claim_steps.append(f"📋 At checkout, paste promo code '{promo_code}' in the Gift Cards & Promo Codes box.")
    if bank_offer:
        claim_steps.append(f"💳 Select eligible card/payment for instant bank discount: {bank_offer}.")
    for off in extra_offers:
        if "Subscribe & Save" in off or "S&S" in off:
            claim_steps.append("🔄 For recurring items, select 'Subscribe & Save' to unlock an extra 5%–10% off.")
        elif "Cashback" in off or "UPI" in off:
            claim_steps.append(f"💸 Pay via Amazon Pay UPI to receive your cashback: {off}.")
        elif "Buy" in off:
            claim_steps.append(f"📦 Add qualifying combo items to your cart: {off}.")
    claim_steps.append(f"🎉 Complete checkout at the effective deal price of ₹{eff_p:,}!")

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
        "bank_offer": bank_offer,
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
        "extra_offers": extra_offers,
        "claim_steps": claim_steps,
        "expired": expired,
        "price_spiked": price_spiked,
        "posted_to_telegram": posted_to_telegram,
        "posted_at_epoch": post_time,
        "age_hours": age_hours,
        "trending_score": trending_score,
        "is_new": is_new,
        "updated_at": updated_at_epoch,
        "updated_at_ist": updated_at_ist,
    }


def sync_price_sightings_cache(conn: sqlite3.Connection) -> None:
    """Backfill price_sightings table from price_seen and posted_deals if empty or sparse."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM price_sightings")
        count = cur.fetchone()[0]
        if count == 0:
            # Aggregation from price_seen
            cur.execute("""
                INSERT OR IGNORE INTO price_sightings (canonical_id, min_price, max_price, avg_price, last_price, sighting_count, first_seen_at, last_seen_at)
                SELECT 
                    CASE 
                        WHEN deal_id LIKE 'amz-%' THEN 'amazon:' || SUBSTR(deal_id, 5)
                        ELSE deal_id 
                    END as canonical_id,
                    MIN(price) as min_price,
                    MAX(price) as max_price,
                    AVG(price) as avg_price,
                    price as last_price,
                    COUNT(*) as sighting_count,
                    MIN(seen_at) as first_seen_at,
                    MAX(seen_at) as last_seen_at
                FROM price_seen
                WHERE price IS NOT NULL AND price > 0
                GROUP BY canonical_id
            """)
            # Also insert from posted_deals for any missing
            cur.execute("""
                INSERT OR IGNORE INTO price_sightings (canonical_id, min_price, max_price, avg_price, last_price, sighting_count, first_seen_at, last_seen_at)
                SELECT 
                    COALESCE(canonical_product_id, 'amazon:' || SUBSTR(deal_id, 5)) as canonical_id,
                    price as min_price,
                    price as max_price,
                    price as avg_price,
                    price as last_price,
                    1 as sighting_count,
                    posted_at as first_seen_at,
                    posted_at as last_seen_at
                FROM posted_deals
                WHERE price IS NOT NULL
                  AND canonical_id IS NOT NULL
                GROUP BY canonical_id
            """)
            conn.commit()
    except Exception as e:
        logger.debug(f"sync_price_sightings_cache error: {e}")


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

    # Backfill and load price sightings for historical stats lookup
    sightings: Dict[str, Dict[str, Any]] = {}
    with store._connect() as conn:
        sync_price_sightings_cache(conn)
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

            # Extract coupon & promo code from caption and title
            c_disc, c_text, p_code = extract_coupon_info(f"{title}\n{caption}")

            # Extract price, mrp, bank offer from caption or database
            final_price = extract_deal_price(caption, d_dict.get("price"))
            if not final_price or final_price <= 0:
                logger.warning(f"Skipping deal {deal_id} ({asin}) because no valid price could be resolved.")
                continue

            mrp_price = extract_mrp(caption)
            discount_pct = d_dict.get("discount") or extract_discount_pct(caption, final_price, mrp_price)
            bank_offer = extract_bank_offer(f"{title}\n{caption}")

            # Time-decay check: drop unverified deals older than 48h
            post_epoch = parse_posted_epoch(d_dict.get("posted_at"))
            now_epoch = int(time.time())
            age_hours = round(max(0.0, (now_epoch - post_epoch) / 3600.0), 1)
            if age_hours > 48.0:
                continue

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
                coupon_code=p_code,
                bank_offer=bank_offer,
            )
            # Extract extra offers and coupon tick instructions from caption if present
            extra_offers = []
            c_text_val = c_text
            for line in caption.split("\n"):
                if "Bonus Offer:" in line:
                    off_text = re.sub(r"🎁\s*<b>Bonus Offer:</b>\s*", "", line).strip()
                    if off_text and off_text not in extra_offers:
                        extra_offers.append(off_text)
                elif "Coupon Trick:" in line:
                    c_tick = re.search(r'Tick\s*<i>"?([^"<]+)"?</i>', line)
                    if c_tick and not c_text_val:
                        c_text_val = c_tick.group(1).strip()
            deal_obj.coupon_text = c_text_val
            deal_obj.extra_offers = extra_offers

            if c_disc:
                deal_obj.coupon_discount = c_disc
                deal_obj.effective_price = max(1, (final_price or 0) - c_disc)

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
                posted_epoch=post_epoch,
            )
            deals_by_asin[asin] = w_json

    # 2. Layer in freshly verified deals from the current cycle
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
                posted_to_telegram=False,
                posted_epoch=int(time.time()),
            )
            deals_by_asin[asin] = w_json

    # Sort final items by trending_score DESC, then buy_confidence DESC, then updated_at DESC
    # Guard: Strictly filter out any anomalies with zero or missing price
    deal_list = [d for d in deals_by_asin.values() if (d.get("effective_price") or 0) > 0]
    deal_list.sort(key=lambda x: (x.get("trending_score") or x.get("score") or 0, x.get("buy_confidence") or 0, x.get("updated_at") or 0), reverse=True)
    final_deals = deal_list[:max_items]

    # Write data/live_deals.json
    live_deals_path = output_dir / "live_deals.json"
    with open(live_deals_path, "w", encoding="utf-8") as f:
        json.dump(final_deals, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported {len(final_deals)} deals to {live_deals_path}")

    # Compute stats
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    tg_url = resolve_telegram_url()
    stats = {
        "last_updated_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "last_updated_epoch": int(time.time()),
        "total_live_deals": len(final_deals),
        "loot_count": sum(1 for d in final_deals if d.get("is_loot")),
        "coupon_deals_count": sum(1 for d in final_deals if (d.get("coupon_discount") or d.get("coupon_text") or d.get("promo_code") or d.get("bank_offer"))),
        "all_time_lows_count": sum(1 for d in final_deals if (d.get("price_context") in ("all_time_low", "real_drop") or d.get("is_further_drop"))),
        "max_discount_pct": max((d.get("effective_discount_pct") or d.get("discount_pct") or 0 for d in final_deals), default=0),
        "total_savings_available": sum(d.get("savings_inr") or 0 for d in final_deals),
        "telegram_url": tg_url,
    }

    stats_path = output_dir / "stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported stats to {stats_path}")

    return {"deals_count": len(final_deals), "stats": stats}
