import html
from dealbot.ssg_engine import (
    ensure_og_banner,
    pre_render_all_pages,
    render_deal_card_html,
    generate_page_html,
)
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


def sync_telegram_deals_from_dealdash(root_dir: Optional[Path] = None, timeout: float = 6.0) -> int:
    """
    Download the latest public SQLite database from DealDash_ai_Bot on GitHub
    (trying data/deals.db first, then falling back to data/state.sqlite3) with a 6s timeout,
    failing silently if offline.
    Merges newer rows from DealDash_ai_Bot's posted_deals and price_sightings tables
    into bharatbazaro/data/deals.db and bharatbazaro/data/state.sqlite3.
    """
    import tempfile
    import urllib.request

    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent

    urls = [
        "https://raw.githubusercontent.com/Prasad-PingFederate/DealDash_ai_Bot/main/data/deals.db",
        "https://raw.githubusercontent.com/Prasad-PingFederate/DealDash_ai_Bot/main/data/state.sqlite3",
    ]
    raw_bytes = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BharatBazaro-Sync/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    raw_bytes = resp.read()
                    if raw_bytes and len(raw_bytes) > 1024:
                        logger.info(f"Downloaded {len(raw_bytes)} bytes from {url} for Telegram deal sync")
                        break
        except Exception as e:
            logger.debug(f"Could not fetch {url}: {e}")

    if not raw_bytes:
        return 0

    tmp_path = None
    synced_deals_count = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3") as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name

        src_conn = sqlite3.connect(tmp_path)
        src_conn.row_factory = sqlite3.Row
        src_cur = src_conn.cursor()

        # Check if posted_deals exists in source
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posted_deals'")
        if not src_cur.fetchone():
            src_conn.close()
            return 0

        src_cur.execute(
            """
            SELECT deal_id, retailer, chat_id, message_id, caption, status, posted_at,
                   last_checked_at, category, discount, agent_score, agent_reason,
                   canonical_product_id, price, consecutive_oos_count, consecutive_spike_count
            FROM posted_deals
            """
        )
        deals_rows = src_cur.fetchall()

        # Check if price_sightings exists in source
        src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='price_sightings'")
        has_sightings = bool(src_cur.fetchone())
        sightings_rows = []
        if has_sightings:
            src_cur.execute(
                """
                SELECT canonical_id, min_price, max_price, avg_price, last_price,
                       sighting_count, first_seen_at, last_seen_at
                FROM price_sightings
                """
            )
            sightings_rows = src_cur.fetchall()
        src_conn.close()

        # Destination databases: data/deals.db and data/state.sqlite3
        data_dir = root_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        dest_dbs = [data_dir / "deals.db", data_dir / "state.sqlite3"]

        ddl_posted_deals = """
            CREATE TABLE IF NOT EXISTS posted_deals (
                deal_id TEXT PRIMARY KEY,
                retailer TEXT,
                chat_id TEXT,
                message_id INTEGER,
                caption TEXT,
                status TEXT DEFAULT 'active',
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP,
                category TEXT,
                discount INTEGER,
                agent_score INTEGER,
                agent_reason TEXT,
                canonical_product_id TEXT,
                price INTEGER,
                consecutive_oos_count INTEGER DEFAULT 0,
                consecutive_spike_count INTEGER DEFAULT 0
            )
        """
        ddl_sightings = """
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
        """

        for db_file in dest_dbs:
            try:
                with sqlite3.connect(db_file, timeout=10.0) as dst_conn:
                    dst_cur = dst_conn.cursor()
                    dst_cur.execute(ddl_posted_deals)
                    dst_cur.execute(ddl_sightings)

                    for r in deals_rows:
                        dst_cur.execute(
                            """
                            INSERT INTO posted_deals (
                                deal_id, retailer, chat_id, message_id, caption, status, posted_at,
                                last_checked_at, category, discount, agent_score, agent_reason,
                                canonical_product_id, price, consecutive_oos_count, consecutive_spike_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(deal_id) DO UPDATE SET
                                status = excluded.status,
                                last_checked_at = excluded.last_checked_at,
                                price = COALESCE(excluded.price, posted_deals.price),
                                message_id = COALESCE(excluded.message_id, posted_deals.message_id),
                                chat_id = COALESCE(excluded.chat_id, posted_deals.chat_id)
                            """,
                            tuple(r),
                        )

                    for s in sightings_rows:
                        dst_cur.execute(
                            """
                            INSERT INTO price_sightings (
                                canonical_id, min_price, max_price, avg_price, last_price,
                                sighting_count, first_seen_at, last_seen_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(canonical_id) DO UPDATE SET
                                min_price = MIN(price_sightings.min_price, excluded.min_price),
                                max_price = MAX(price_sightings.max_price, excluded.max_price),
                                avg_price = (price_sightings.avg_price + excluded.avg_price) / 2.0,
                                last_price = excluded.last_price,
                                sighting_count = price_sightings.sighting_count + excluded.sighting_count,
                                last_seen_at = MAX(price_sightings.last_seen_at, excluded.last_seen_at)
                            """,
                            tuple(s),
                        )
                    dst_conn.commit()
            except Exception as e:
                logger.warning(f"Error merging Telegram deals into {db_file}: {e}")

        synced_deals_count = len(deals_rows)
        logger.info(f"Successfully synced {synced_deals_count} Telegram deals from DealDash into BharatBazaro databases.")
    except Exception as e:
        logger.warning(f"Failed to sync DealDash SQLite deals: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    return synced_deals_count


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


def strip_title_prefixes(title: str) -> str:
    """Strip leading brackets [ICICI CC], (Offer), emojis, and promo prefixes."""
    if not title:
        return ""
    t = title.strip()
    while True:
        m = re.match(r"^(?:\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}|【[^】]*】)\s*", t)
        if m:
            t = t[m.end():].strip()
        else:
            break
    # Strip leading emoji or non-alphanumeric punctuation (except opening brackets)
    t = re.sub(r"^[^\w\[\(\{]+", "", t).strip()
    # Strip prefixes like "Deal:", "Loot:", "Hot Deal -", etc.
    t = re.sub(r"^(?:deal|loot|hot\s+deal|steal\s+deal|special\s+deal|offer)\s*[:\-]\s*", "", t, flags=re.IGNORECASE).strip()
    return t


def detect_brand(title: str) -> Optional[str]:
    """
    Extract brand name from product title.
    Focuses strictly on the first 1-2 words of the product title
    (after stripping bank tags like [ICICI CC], coupon brackets, and prefixes)
    rather than scanning the entire title string, preventing false positive matches
    from feature descriptions (e.g. 'Google Assistant' in Philips bulbs, or 'HP Out' in LG monitors).
    """
    if not title:
        return None
    clean = strip_title_prefixes(title)
    if not clean:
        return None

    raw_words = clean.split()
    words = []
    for w in raw_words:
        cw = w.strip(" ,-:()[]{}\"'`")
        if cw:
            words.append(cw)
    if not words:
        return None

    stop_words = {
        "the", "new", "all", "deal", "loot", "best", "pack", "set", "combo",
        "item", "unit", "buy", "save", "upto", "flat", "special", "super", "mega",
    }
    acronym_brands = {
        "lg": "LG",
        "hp": "HP",
        "jbl": "JBL",
        "ifb": "IFB",
        "wd": "WD",
        "dji": "DJI",
        "boat": "boAt",
        "u.s. polo": "U.S. Polo",
        "us polo": "U.S. Polo",
    }

    start_idx = 1 if words[0].lower() in ("new", "all-new", "latest", "trending", "original", "genuine") and len(words) > 1 else 0
    lead_words = words[start_idx : start_idx + 3]
    lead_str = " ".join(lead_words).lower()
    first_w = lead_words[0].lower().rstrip(".") if lead_words else ""

    all_brands = sorted(list(set(TIER_1_BRANDS + TIER_2_BRANDS)), key=len, reverse=True)
    for b in all_brands:
        if b == "apple" and re.search(r"\b(green\s+apple|apple\s+cider|apple\s+juice|apple\s+flavo?ur)\b", lead_str):
            continue
        if " " in b or "-" in b or "." in b:
            if re.search(r"^\b" + re.escape(b) + r"\b", lead_str):
                return acronym_brands.get(b, b.title())
        else:
            if first_w == b:
                return acronym_brands.get(b, b.title())
            if len(lead_words) > 1 and lead_words[1].lower().rstrip(".") == b and first_w in stop_words:
                return acronym_brands.get(b, b.title())

    # Fallback: first word of title if alphabetic and not a generic stop word
    if len(first_w) >= 3 and first_w.isalpha() and first_w not in stop_words:
        return first_w.title()
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

    if c in ("mobiles", "mobile", "smartphones", "smartphone") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("phone", "phones", "mobile", "mobiles", "smartphone", "smartphones", "iphone", "galaxy", "case", "cases", "charger", "chargers", "cable", "cables", "tempered glass", "power bank", "oneplus", "redmi", "realme", "iqoo", "poco", "motorola")):
        return "Mobiles & Accessories"
    if c in ("audio", "headphones") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("headphone", "headphones", "earphone", "earphones", "earbud", "earbuds", "audio", "speaker", "speakers", "soundbar", "tws", "neckband", "bluetooth speaker", "mic", "microphone")):
        return "Audio & Headphones"
    if c in ("computers", "laptops", "laptop") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("laptop", "laptops", "macbook", "pc", "desktop", "monitor", "monitors", "keyboard", "mouse", "ssd", "ram", "hard drive", "processor", "gpu", "printer", "chromebook", "nvme")):
        return "Laptops & Computing"
    if c in ("home", "kitchen", "appliances") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("cooker", "fryer", "air fryer", "pan", "bottle", "purifier", "vacuum", "mixer", "grinder", "kitchen", "kettle", "bedsheet", "curtain", "furniture", "appliance", "blender", "iron", "toaster", "lunch box", "geyser", "induction", "chimney")):
        return "Home & Kitchen"
    if c == "fashion" or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("shirt", "t-shirt", "tshirt", "shoe", "shoes", "sneaker", "sneakers", "jeans", "saree", "kurta", "jacket", "sandals", "wallet", "backpack", "watch", "watches", "sling bag", "toiletry bag", "dress")):
        return "Fashion & Footwear"
    if c in ("beauty", "grocery") or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("shampoo", "cream", "serum", "perfume", "soap", "tea", "coffee", "biscuit", "almond", "oil", "chocolate", "snack", "diaper", "face wash", "sunscreen", "lotion", "moisturizer", "cleanser", "ghee", "atta")):
        return "Personal Care & Grocery"
    if c == "electronics" or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ("tv", "television", "camera", "drone", "trimmer", "smartwatch", "gadget")):
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
    last_verified_epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Transform an internal Deal object into the standard BharatBazaro live_deals.json item."""
    asin = extract_asin(deal)
    if not asin:
        asin = "B0UNKNOWN0"

    now_epoch = int(time.time())
    post_time = posted_epoch or now_epoch
    verified_time = last_verified_epoch or post_time
    age_hours = round(max(0.0, (now_epoch - verified_time) / 3600.0), 1)
    decay = max(0.0, (age_hours - 2.0) * 2.5)
    trending_score = round(max(10.0, float(score or 70) - decay), 1)
    is_new = bool(age_hours <= 2.0)
    is_fresh = bool(age_hours <= 24.0)

    # Use verified timestamp for updated_at display
    verif_dt = datetime.fromtimestamp(verified_time, tz=timezone(timedelta(hours=5, minutes=30)))
    updated_at_ist = verif_dt.strftime("%Y-%m-%d %H:%M IST")
    updated_at_epoch = verified_time

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

    # High-converting Diwali Steal check (Requirement 3):
    is_diwali_steal = bool(
        savings_inr >= 1000
        or (eff_p >= 2500 and (eff_disc_pct or 0) >= 25)
        or ((getattr(deal, "review_count", None) or 0) >= 1000 and (eff_disc_pct or 0) >= 30)
        or is_loot
    )

    # Cookie-Magnet / Daily Essential Steal Identification (Requirement 1 & 3)
    t_lower = (deal.title or "").lower()
    is_vp = any(vp in t_lower for vp in (
        "1l", "1 l", "1 litre", "1000ml", "2l", "2 l", "2 litre", "5kg", "5 kg",
        "pack of 2", "pack of 3", "pack of 4", "pack of 6", "pack of 8", "pack of 10",
        "combo pack", "value pack", "refill pack", "mega pack"
    ))
    fmcg_brands = (
        "dove", "nivea", "pears", "cetaphil", "minimalist", "derma co", "plum", "l'oreal", "loreal",
        "garnier", "chemist at play", "tresemme", "vaseline", "lakme", "beardo", "colgate", "dettol",
        "savlon", "listerine", "whisper", "surf excel", "ariel", "vim", "comfort", "harpic", "lizol",
        "presto", "cadbury", "ferrero", "happilo", "tata sampann", "nescafe", "davidoff"
    )
    is_fmcg = any(b in t_lower for b in fmcg_brands) or ui_category == "Personal Care & Grocery"
    is_cookie_magnet = bool(
        getattr(deal, "is_cookie_magnet", False)
        or (is_fmcg and eff_p and eff_p <= 499 and (eff_disc_pct or 0) >= 30)
        or (is_vp and eff_p and eff_p <= 499 and (eff_disc_pct or 0) >= 35)
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

    clean_aff_url = f"https://www.amazon.in/dp/{asin}?tag={partner_tag}&linkCode=ll1&ascsubtag=bb-grid"
    image_url = deal.image_url or f"https://m.media-amazon.com/images/P/{asin}.jpg"

    return {
        "id": f"amazon:{asin}",
        "asin": asin,
        "title": html.unescape(deal.title or "Amazon Special Deal").strip(),
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
        "is_diwali_steal": is_diwali_steal,
        "is_cookie_magnet": is_cookie_magnet,
        "is_daily_essential": is_fmcg,
        "is_further_drop": getattr(deal, "is_further_price_drop", False),
        "source_count": getattr(deal, "source_count", 1),
        "extra_offers": extra_offers,
        "claim_steps": claim_steps,
        "expired": expired,
        "price_spiked": price_spiked,
        "posted_to_telegram": posted_to_telegram,
        "posted_at_epoch": post_time,
        "last_verified_at_epoch": verified_time,
        "last_verified_at_ist": updated_at_ist,
        "age_hours": age_hours,
        "trending_score": trending_score,
        "is_new": is_new,
        "is_fresh": is_fresh,
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



def export_ai_briefing(
    deals: List[Dict[str, Any]],
    stats: Dict[str, Any],
    output_dir: Path
) -> Dict[str, Any]:
    """Generate hourly AI market briefing at data/ai_briefing.json for BazarAI."""
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))

    # 1. Top loot summary (top 5 loots)
    loots = [d for d in deals if d.get("is_loot")]
    top_loots = loots[:5] if loots else deals[:5]
    top_loot_summary = [
        {
            "asin": d.get("asin"),
            "title": d.get("title"),
            "effective_price": d.get("effective_price") or d.get("list_price"),
            "mrp": d.get("mrp"),
            "discount_pct": d.get("effective_discount_pct") or d.get("discount_pct"),
            "savings_inr": d.get("savings_inr"),
            "brand": d.get("brand"),
            "category": d.get("category"),
            "affiliate_url": d.get("affiliate_url"),
            "deal_url": f"https://bharatbazaro.com/deal/{d.get('asin')}.html",
            "coupon_text": d.get("coupon_text"),
        }
        for d in top_loots
    ]

    # 2. Best coupon deal
    coupon_deals = [
        d for d in deals
        if (d.get("coupon_discount") and d.get("coupon_discount") > 0)
        or d.get("coupon_text")
        or d.get("promo_code")
    ]
    best_coupon = coupon_deals[0] if coupon_deals else None

    # 3. Best deal under ₹299
    under_299_deals = [d for d in deals if (d.get("effective_price") or d.get("list_price") or 999999) <= 299]
    best_under_299 = under_299_deals[0] if under_299_deals else None

    # 4. Best Tier 1 brand deal
    tier1_deals = [d for d in deals if d.get("brand_tier") == "tier1"]
    best_tier1 = tier1_deals[0] if tier1_deals else None

    # 5. Category highlights
    category_highlights = {}
    categories = ["mobiles", "electronics", "home", "fashion", "grocery"]
    for cat in categories:
        for d in deals:
            d_cat = (d.get("category") or "").lower()
            if cat in d_cat or (cat == "electronics" and ("audio" in d_cat or "laptop" in d_cat)):
                category_highlights[cat] = {
                    "asin": d.get("asin"),
                    "title": d.get("title"),
                    "effective_price": d.get("effective_price") or d.get("list_price"),
                    "discount_pct": d.get("effective_discount_pct") or d.get("discount_pct"),
                    "deal_url": f"https://bharatbazaro.com/deal/{d.get('asin')}.html",
                }
                break

    briefing = {
        "generated_at_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "generated_at_epoch": int(time.time()),
        "top_loot_summary": top_loot_summary,
        "best_coupon_deal": best_coupon,
        "best_under_299": best_under_299,
        "best_tier1_deal": best_tier1,
        "category_highlights": category_highlights,
        "market_stats": {
            "total_deals": stats.get("total_live_deals", len(deals)),
            "loot_count": stats.get("loot_count", 0),
            "max_discount_pct": stats.get("max_discount_pct", 0),
            "total_savings_inr": stats.get("total_savings_available", 0),
        }
    }

    briefing_path = output_dir / "ai_briefing.json"
    with open(briefing_path, "w", encoding="utf-8") as f:
        json.dump(briefing, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported AI market briefing to {briefing_path}")
    return briefing


def export_web_feed(
    store,
    current_cycle_deals: Optional[List[Deal]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    max_items: int = 250,
) -> Dict[str, Any]:
    """Export current live verified deals to live_deals.json and stats.json."""
    root_dir = Path(__file__).resolve().parent.parent
    try:
        sync_telegram_deals_from_dealdash(root_dir)
    except Exception as e:
        logger.warning(f"sync_telegram_deals_from_dealdash error: {e}")

    if output_dir is None:
        output_dir = root_dir / "data"
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

    # Load image/title cache for robust title fallbacks
    cache_path = root_dir / "data" / "image_cache.json"
    img_cache: Dict[str, Dict[str, Any]] = {}
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                img_cache = json.load(cf)
        except Exception:
            pass

    # 1. Ingest deals from SQLite posted_deals (active, within last 48 hours)
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT deal_id, retailer, chat_id, message_id, caption, status, posted_at, last_verified_at,
                   category, discount, agent_score, agent_reason, canonical_product_id, price, rating, review_count
            FROM posted_deals
            WHERE status = 'active'
              AND (posted_at >= datetime('now', '-72 hours') OR last_verified_at >= datetime('now', '-72 hours') OR posted_at IS NOT NULL)
            ORDER BY COALESCE(last_verified_at, posted_at) DESC
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
                if m_sub and not m_sub.group(1).strip().startswith("Amazon Deal"):
                    title = m_sub.group(1).strip()

            # Robust fallback: check image cache, Keepa, or URL slug before accepting placeholder title
            if not title or title.startswith("Amazon Deal") or title.startswith("Trending Amazon Deal"):
                if asin in img_cache and img_cache[asin].get("title"):
                    c_title = img_cache[asin]["title"]
                    if not c_title.startswith("Amazon Deal") and not c_title.startswith("Trending Amazon Deal"):
                        title = c_title

                if not title or title.startswith("Amazon Deal") or title.startswith("Trending Amazon Deal"):
                    url_m = re.search(r'https?://(?:www\.)?amazon\.in/[^\s<>"]+', caption)
                    if url_m:
                        try:
                            from dealbot.sources import extract_title_from_amazon_url_slug
                            slug_t = extract_title_from_amazon_url_slug(url_m.group(0))
                            if slug_t:
                                title = slug_t
                        except Exception:
                            pass

                if not title:
                    title = f"Amazon Deal ({asin})"

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
            verif_epoch = parse_posted_epoch(d_dict.get("last_verified_at")) if d_dict.get("last_verified_at") else post_epoch
            now_epoch = int(time.time())
            verif_age_hours = round(max(0.0, (now_epoch - verif_epoch) / 3600.0), 1)
            post_age_hours = round(max(0.0, (now_epoch - post_epoch) / 3600.0), 1)
            if verif_age_hours > 48.0 and post_age_hours > 48.0:
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
                rating=d_dict.get("rating"),
                review_count=d_dict.get("review_count"),
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
                posted_to_telegram=bool(d_dict.get("message_id") or d_dict.get("chat_id") or True),
                expired=(d_dict.get("status") == "expired"),
                price_spiked=(d_dict.get("status") == "price_spiked"),
                posted_epoch=post_epoch,
                last_verified_epoch=verif_epoch,
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

    # 3. Layer in curated seed deals to guarantee minimum 15 products per category
    local_deals_path = root_dir / "data" / "deals.json"
    if output_dir == (root_dir / "data") and local_deals_path.exists():
        try:
            from dealbot.sources import load_local_deals
            seed_deals = load_local_deals(local_deals_path)
            for sd in seed_deals:
                s_asin = extract_asin(sd)
                if not s_asin or len(s_asin) != 10:
                    continue
                if s_asin not in deals_by_asin:
                    score = getattr(sd, "agent_score", None) or 75
                    s_ctx = sightings.get(f"amazon:{s_asin}") or sightings.get(sd.id)
                    w_json = deal_to_web_json(
                        sd,
                        score=score,
                        price_sightings=s_ctx,
                        partner_tag=partner_tag,
                        posted_to_telegram=False,
                        posted_epoch=int(time.time()),
                        last_verified_epoch=int(time.time()),
                    )
                    deals_by_asin[s_asin] = w_json
        except Exception as e:
            logger.warning(f"Error loading seed deals in web_exporter: {e}")

    # Guard: Strictly filter out any anomalies with zero or missing price
    deal_list = [d for d in deals_by_asin.values() if (d.get("effective_price") or 0) > 0]

    # Category Balancing (Requirement 1):
    # Ensure every target category maintains at least 15 active verified deals if available
    TARGET_CATEGORIES = [
        "Mobiles & Accessories",
        "Electronics & Audio",
        "Laptops & Computing",
        "Audio & Headphones",
        "Home & Kitchen",
        "Fashion & Footwear",
        "Personal Care & Grocery",
    ]
    balanced_deals = []
    selected_asins = set()

    # Sort candidates strictly by recency, trending score, buy confidence
    deal_list.sort(
        key=lambda x: (
            1 if x.get("is_fresh", True) else 0,
            x.get("last_verified_at_epoch") or x.get("posted_at_epoch") or x.get("updated_at") or 0,
            x.get("trending_score") or x.get("score") or 0,
            x.get("buy_confidence") or 0
        ),
        reverse=True
    )

    # 1. Guarantee minimum 15 deals per target category
    for cat in TARGET_CATEGORIES:
        cat_items = [d for d in deal_list if d.get("category") == cat]
        for it in cat_items[:15]:
            if it.get("asin") not in selected_asins:
                selected_asins.add(it.get("asin"))
                balanced_deals.append(it)

    # 2. Backfill remaining slots up to max_items with top ranked deals
    for d in deal_list:
        if len(balanced_deals) >= max_items:
            break
        if d.get("asin") not in selected_asins:
            selected_asins.add(d.get("asin"))
            balanced_deals.append(d)

    # Final re-sort: fresh deals first, newest verified timestamp first, then score
    balanced_deals.sort(
        key=lambda x: (
            1 if x.get("is_fresh", True) else 0,
            x.get("last_verified_at_epoch") or x.get("posted_at_epoch") or x.get("updated_at") or 0,
            x.get("trending_score") or x.get("score") or 0,
            x.get("buy_confidence") or 0
        ),
        reverse=True
    )
    final_deals = balanced_deals[:max_items]

    # Resolve and ensure high-res images and non-blank photos
    root_dir = Path(__file__).resolve().parent.parent
    try:
        from dealbot.image_resolver import resolve_deal_images
        final_deals = resolve_deal_images(final_deals, root_dir=root_dir)
    except Exception as e:
        logger.warning(f"resolve_deal_images error: {e}")

    # Write data/live_deals.json
    live_deals_path = output_dir / "live_deals.json"
    with open(live_deals_path, "w", encoding="utf-8") as f:
        json.dump(final_deals, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported {len(final_deals)} deals to {live_deals_path}")

    # Compute stats
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    tg_url = resolve_telegram_url()
    fresh_deals = [d for d in final_deals if d.get("is_fresh", True)]
    older_deals = [d for d in final_deals if not d.get("is_fresh", True)]
    stats = {
        "last_updated_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "last_updated_epoch": int(time.time()),
        "total_live_deals": len(fresh_deals),
        "total_all_deals": len(final_deals),
        "fresh_deals_count": len(fresh_deals),
        "older_deals_count": len(older_deals),
        "loot_count": sum(1 for d in fresh_deals if d.get("is_loot")),
        "coupon_deals_count": sum(1 for d in fresh_deals if (d.get("coupon_discount") or d.get("coupon_text") or d.get("promo_code") or d.get("bank_offer"))),
        "all_time_lows_count": sum(1 for d in fresh_deals if (d.get("price_context") in ("all_time_low", "real_drop") or d.get("is_further_drop"))),
        "max_discount_pct": max((d.get("effective_discount_pct") or d.get("discount_pct") or 0 for d in fresh_deals), default=0),
        "total_savings_available": sum(d.get("savings_inr") or 0 for d in fresh_deals),
        "telegram_url": tg_url,
    }

    stats_path = output_dir / "stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported stats to {stats_path}")

    # Export hourly AI market briefing for BazarAI
    try:
        export_ai_briefing(final_deals, stats, output_dir=output_dir)
    except Exception as e:
        logger.error(f"Error exporting AI briefing: {e}", exc_info=True)

    # SSG Pre-rendering of index.html and all 6 standalone category landing pages
    root_dir = Path(__file__).resolve().parent.parent
    try:
        ensure_og_banner(root_dir)
        pre_render_all_pages(final_deals, stats, root_dir=root_dir)
    except Exception as e:
        logger.error(f"Error pre-rendering SSG HTML pages: {e}", exc_info=True)

    return {"deals_count": len(final_deals), "stats": stats}


def export_web_data(store=None, **kwargs):
    """
    Export live web data and pre-render all 7 SSG pages.
    Can be invoked with a StateStore or standalone.
    """
    root_dir = Path(__file__).resolve().parent.parent
    try:
        sync_telegram_deals_from_dealdash(root_dir)
    except Exception as e:
        logger.warning(f"sync_telegram_deals_from_dealdash error: {e}")

    if store is None:
        try:
            from dealbot.publisher import StateStore
            from dealbot.session import database_path
            db_p = database_path()
            if not db_p.exists():
                alt_p = root_dir / "data" / "deals.db"
                if alt_p.exists():
                    db_p = alt_p
                else:
                    alt_p2 = root_dir / "data" / "state.sqlite3"
                    if alt_p2.exists():
                        db_p = alt_p2
            store = StateStore(str(db_p))
        except Exception as e:
            logger.warning(f"Failed to initialize StateStore in export_web_data: {e}")
            store = None

    if store is not None:
        return export_web_feed(store, **kwargs)

    root_dir = Path(__file__).resolve().parent.parent
    output_dir = kwargs.get("output_dir") or (root_dir / "data")
    output_dir = Path(output_dir)
    live_deals_path = output_dir / "live_deals.json"
    stats_path = output_dir / "stats.json"
    deals = []
    stats = {}
    import json
    if live_deals_path.exists():
        with open(live_deals_path, "r", encoding="utf-8") as f:
            deals = json.load(f)
    if stats_path.exists():
        with open(stats_path, "r", encoding="utf-8") as f:
            stats = json.load(f)

    try:
        export_ai_briefing(deals, stats, output_dir=output_dir)
    except Exception:
        pass
    ensure_og_banner(root_dir)
    pre_render_all_pages(deals, stats, root_dir=root_dir)
    return {"deals_count": len(deals), "stats": stats}
