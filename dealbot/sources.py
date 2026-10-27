import html
import json
import logging
import os
import re
import time
import urllib.parse
import concurrent.futures
from pathlib import Path
from typing import Dict, List, Optional, Union
import xml.etree.ElementTree as ET
import requests
from dealbot.models import Deal, parse_rupees

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# In-memory token cache for Amazon Creators API
_AMAZON_TOKEN_CACHE = {"token": None, "expires_at": 0.0}

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent

def clean_title(title: Optional[str]) -> str:
    """Clean promotional noise, coupons, and trailing price/retailer tags from titles."""
    if not title or not isinstance(title, str):
        return "Special Deal"
    t = title.strip()
    # Strip [Apply X% Coupon] or similar tags at start
    t = re.sub(r"^\[.*?(?:coupon|offer|flat).*?\]\s*", "", t, flags=re.IGNORECASE)
    # Strip trailing price and retailer suffix like 'Rs. 799 - Amazon' or '₹ 899 - Amazon'
    t = re.sub(r"\s*(?:Rs\.?|₹)\s*[\d,]+(?:\s*-\s*[A-Za-z]+)?$", "", t, flags=re.IGNORECASE)
    # Strip trailing retailer like ' - Amazon' or ' - Flipkart'
    t = re.sub(r"\s*-\s*(Amazon|Flipkart|Myntra|Ajio)$", "", t, flags=re.IGNORECASE)
    return t.strip() or "Special Deal"

SHORTLINK_DOMAINS = (
    "amzn.to", "amzn.in", "fkrt.it", "bitli.in", "extp.in",
    "wishlink.com", "bit.ly", "cutt.ly", "tinyurl.com", "t.co", "vurl.com"
)
SHORTLINK_CACHE: Dict[str, str] = {}
AMAZON_ASIN_REGEX = re.compile(
    r"/(?:dp|gp/product|exec/obidos/ASIN|aw/d)/([A-Z0-9]{10})",
    re.IGNORECASE,
)
FLIPKART_STRIP_PARAMS = {
    "affid", "affextparam1", "affextparam2", "cmpid", "otracker",
    "ssid", "gclid", "fbclid", "ref_", "semcmpid"
}

def unroll_shortlink(url: str, timeout: int = 6) -> str:
    """
    Detect shortened URLs and resolve redirect chain (HEAD with fallback to GET).
    Caches resolved destination URLs in memory.
    """
    if not url:
        return ""
    if url in SHORTLINK_CACHE:
        return SHORTLINK_CACHE[url]

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()
    is_short = any(s in domain for s in SHORTLINK_DOMAINS)
    if not is_short:
        return url

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    target = url
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        if resp.url and resp.url != url and resp.status_code in (200, 301, 302, 307, 308):
            target = resp.url
        elif resp.status_code in (400, 403, 405) or resp.url == url:
            resp_get = requests.get(url, allow_redirects=True, timeout=timeout, headers=headers, stream=True)
            if resp_get.url:
                target = resp_get.url
            resp_get.close()
    except requests.RequestException as e:
        logger.warning(f"Could not unroll shortlink {url}: {e}")

    SHORTLINK_CACHE[url] = target
    return target


def expand_and_convert_url(raw_url: Optional[str]) -> tuple[str, str, str]:
    """Extract ASIN or PID and automatically inject user affiliate tracking parameters."""
    amazon_tag = (
        os.getenv("AMAZON_AFFILIATE_TAG")
        or os.getenv("AMAZON_PARTNER_TAG")
        or "dammaiprasad-21"
    ).strip()
    flipkart_id = (
        os.getenv("FLIPKART_AFFILIATE_ID")
        or os.getenv("FLIPKART_AFFID")
        or ""
    ).strip()

    if not raw_url or not isinstance(raw_url, str):
        return "", "deal-invalid", "Online Store"

    url = raw_url.strip()
    if not url:
        return "", "deal-empty", "Online Store"

    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    # Unroll shortened URLs
    url = unroll_shortlink(url)

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()

    # 1. Amazon.in Canonicalization
    if "amazon." in domain:
        asin_match = AMAZON_ASIN_REGEX.search(parsed.path)
        if not asin_match:
            asin_match = re.search(r"[?&]asin=([A-Z0-9]{10})", url, re.IGNORECASE)
        if asin_match:
            asin = asin_match.group(1).upper()
            clean_url = f"https://www.amazon.in/dp/{asin}?tag={amazon_tag}&linkCode=ll1"
            return clean_url, f"amz-{asin}", "Amazon"

    # 2. Flipkart Canonicalization
    if "flipkart.com" in domain:
        qs = urllib.parse.parse_qs(parsed.query)
        pid = qs.get("pid", [None])[0]
        if not pid:
            pid_match = re.search(r"/p/(itm[a-zA-Z0-9]+)", parsed.path)
            if pid_match:
                pid = pid_match.group(1)

        # Strip noisy parameters
        clean_qs = {k: v for k, v in qs.items() if k.lower() not in FLIPKART_STRIP_PARAMS}
        if pid:
            clean_qs["pid"] = [pid]
        if flipkart_id:
            clean_qs["affid"] = [flipkart_id]

        query_str = urllib.parse.urlencode(clean_qs, doseq=True)
        clean_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query_str, ""))
        deal_id = f"fk-{pid}" if pid else f"fk-{abs(hash(parsed.path))}"
        return clean_url, deal_id, "Flipkart"

    return url, f"deal-{abs(hash(url))}", "Online Store"


def worth_posting_discount(discount_percent: int, original_price: str, deal_price: str, min_discount: int) -> bool:
    """Keep the usual percent, and also a smaller percent at any price. No rupee minimum."""
    if discount_percent >= min_discount:
        return True
    price = parse_rupees(deal_price)
    mrp = parse_rupees(original_price)
    if price is None or mrp is None or mrp <= price:
        return False
    return discount_percent >= 15


DEFAULT_EXTRA_FEED = "https://www.reddit.com/r/IndiaDeals/.rss"
PRODUCT_URL = re.compile(
    r"https?://(?:www\.|dl\.)?(?:amazon\.in|amzn\.to|amzn\.in|flipkart\.com)/[^\s<\"']+",
    re.IGNORECASE,
)


def _first_text(item: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(item):
        key = child.tag.split("}")[-1].lower()
        if key in wanted and child.text and child.text.strip():
            return child.text.strip()
    return ""


def _clean_code(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,24}", raw):
        return raw
    return None


def _amazon_image(asin: str) -> Optional[str]:
    if asin and re.fullmatch(r"[A-Z0-9]{10}", asin.upper()):
        return f"https://m.media-amazon.com/images/P/{asin.upper()}.jpg"
    return None


def _prices_from_text(text: str) -> tuple:
    percent = None
    match = re.search(r"(\d{1,2})\s*%", text or "")
    if match:
        percent = int(match.group(1))

    # Strip discount/coupon phrases like "Get ₹2,000 Off", "Save ₹500", "Flat Rs. 1000 Discount", "₹2,000 coupon"
    # so discount amounts are never confused with the deal price or MRP.
    cleaned_text = text or ""
    cleaned_text = re.sub(
        r"(?:save|flat|get|apply)\s*(?:₹|Rs\.?)\s*[\d,]+(?:\.\d+)?",
        "",
        cleaned_text,
        flags=re.IGNORECASE,
    )
    cleaned_text = re.sub(
        r"(?:₹|Rs\.?)\s*[\d,]+(?:\.\d+)?\s*(?:off|discount|cashback|coupon|save|instant)\b",
        "",
        cleaned_text,
        flags=re.IGNORECASE,
    )

    amounts = []
    for raw in re.findall(r"(?:₹|Rs\.?)\s*[\d,]+(?:\.\d+)?", cleaned_text, flags=re.IGNORECASE):
        amount = parse_rupees(raw)
        if amount:
            amounts.append(amount)
    current = amounts[0] if amounts else None
    original = None
    if len(amounts) > 1 and amounts[1] > (current or 0):
        original = amounts[1]
    if percent is None and current and original and original > current:
        percent = int(round((original - current) / original * 100))
    return current, original, percent or 0


def _deal_from_fields(
    title: str,
    raw_url: str,
    org_price: str,
    disc_price: str,
    disc_per: int,
    asin: str,
    min_discount: int,
    coupon: str = "",
    bank: str = "",
    image_url: str = "",
) -> Optional[Deal]:
    if not title or not raw_url:
        return None
    if not worth_posting_discount(disc_per, org_price, disc_price, min_discount):
        # Allow Amazon deals with valid price but missing/unstated feed MRP into Stage 1/2
        # with provisional MRP/discount so live Amazon enrichment can fetch real MRP and discount
        has_stated_mrp = bool(org_price and parse_rupees(org_price))
        aff_url, deal_id, retailer = expand_and_convert_url(raw_url)
        is_amazon = retailer == "Amazon" or "amazon.in" in raw_url.lower() or "amzn.to" in raw_url.lower() or bool(asin)
        if not has_stated_mrp and is_amazon and disc_price:
            p_val = parse_rupees(disc_price)
            if p_val and p_val > 0:
                prov_disc = max(disc_per, 30) if disc_per > 0 else 30
                prov_mrp = str(int(round(p_val / (1.0 - prov_disc / 100.0))))
                image = image_url if image_url.lower().startswith("https://") else None
                if not image and retailer == "Amazon":
                    image = _amazon_image(asin)
                canon_id = f"amazon:{deal_id[4:]}" if deal_id.startswith("amz-") else (f"flipkart:{deal_id[3:]}" if deal_id.startswith("fk-") else f"store:{deal_id}")
                deal = Deal(
                    id=deal_id,
                    retailer=retailer,
                    title=clean_title(title),
                    url=aff_url,
                    current_price=f"₹{disc_price}",
                    original_price=f"₹{prov_mrp}",
                    discount_percent=prov_disc,
                    image_url=image,
                    coupon_code=_clean_code(coupon),
                    bank_offer=bank[:90] if bank else None,
                    canonical_product_id=canon_id,
                )
                return deal if deal.is_valid() else None
        return None
    aff_url, deal_id, retailer = expand_and_convert_url(raw_url)
    image = image_url if image_url.lower().startswith("https://") else None
    if not image and retailer == "Amazon":
        image = _amazon_image(asin)
    canon_id = f"amazon:{deal_id[4:]}" if deal_id.startswith("amz-") else (f"flipkart:{deal_id[3:]}" if deal_id.startswith("fk-") else f"store:{deal_id}")
    deal = Deal(
        id=deal_id,
        retailer=retailer,
        title=clean_title(title),
        url=aff_url,
        current_price=f"₹{disc_price}" if disc_price else None,
        original_price=f"₹{org_price}" if org_price else None,
        discount_percent=disc_per if disc_per > 0 else None,
        image_url=image,
        coupon_code=_clean_code(coupon),
        bank_offer=bank[:90] if bank else None,
        canonical_product_id=canon_id,
    )
    return deal if deal.is_valid() else None


def parse_offer_feed(content: bytes, min_discount: int = 25) -> List[Deal]:
    """Read an IndiaFreeStuff-style XML feed or a normal RSS/Atom feed."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning(f"Feed XML could not be parsed: {exc}")
        return []

    deals: List[Deal] = []
    items = root.findall(".//all_deals")
    if items:
        for item in items:
            if (_first_text(item, "expire") or "0") == "1":
                continue
            try:
                disc_per = int(float(_first_text(item, "discount_per") or "0"))
            except ValueError:
                disc_per = 0
            deal = _deal_from_fields(
                title=_first_text(item, "title"),
                raw_url=_first_text(item, "product_url", "url", "link"),
                org_price=_first_text(item, "org_price"),
                disc_price=_first_text(item, "discount_price"),
                disc_per=disc_per,
                asin=_first_text(item, "asin"),
                min_discount=min_discount,
                coupon=_first_text(item, "coupon", "coupon_code", "offer_code", "promo_code"),
                bank=_first_text(item, "bank_offer", "bank", "card_offer"),
                image_url=_first_text(item, "image", "image_url", "img", "product_image"),
            )
            if deal:
                deals.append(deal)
        return deals

    for item in list(root.iter()):
        kind = item.tag.split("}")[-1].lower()
        if kind not in {"item", "entry"}:
            continue
        title = _first_text(item, "title")
        blob_parts = [title, _first_text(item, "description", "summary", "content")]
        links = []
        for node in item.iter():
            href = node.attrib.get("href") or ""
            if href:
                links.append(href)
            if node.text:
                blob_parts.append(node.text)
        blob = " ".join(blob_parts)
        product_url = ""
        for found in PRODUCT_URL.findall(blob):
            if "reddit.com" in found:
                continue
            product_url = found.rstrip(").,]")
            break
        if not product_url:
            for href in links:
                if PRODUCT_URL.search(href) and "reddit.com" not in href:
                    product_url = href
                    break
        if not product_url or not title:
            continue
        current, original, percent = _prices_from_text(blob)
        deal = _deal_from_fields(
            title=title,
            raw_url=product_url,
            org_price=str(original or ""),
            disc_price=str(current or ""),
            disc_per=percent or 0,
            asin="",
            min_discount=min_discount,
        )
        if deal:
            deals.append(deal)
    return deals


def _download_feed(feed_url: str, min_discount: int) -> List[Deal]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/xml, application/rss+xml, application/atom+xml"}
    try:
        response = requests.get(feed_url, headers=headers, timeout=12)
        if response.status_code != 200:
            logger.warning(f"Feed {feed_url} returned status {response.status_code}")
            return []
        deals = parse_offer_feed(response.content, min_discount=min_discount)
        logger.info(f"Ingested {len(deals)} valid deals from {feed_url}")
        return deals
    except Exception as exc:
        logger.error(f"Error fetching feed {feed_url}: {exc}")
        return []


def fetch_live_feed_deals(min_discount: int = 25) -> List[Deal]:
    """Fetch live deals from the primary aggregator feed and inject affiliate tags."""
    feed_url = os.getenv("DEALS_FEED_URL", "https://www.indiafreestuff.in/feed")
    return _download_feed(feed_url, min_discount)


def fetch_extra_feeds(min_discount: int = 25) -> List[Deal]:
    """Second source. Defaults to r/IndiaDeals. Set EXTRA_FEED_URLS=off to disable."""
    raw = os.getenv("EXTRA_FEED_URLS", DEFAULT_EXTRA_FEED).strip()
    if raw.lower() in {"", "none", "false", "off", "0"}:
        return []
    primary = os.getenv("DEALS_FEED_URL", "https://www.indiafreestuff.in/feed").strip()
    deals: List[Deal] = []
    for url in [part.strip() for part in raw.split(",") if part.strip()]:
        if url == primary:
            continue
        deals.extend(_download_feed(url, min_discount))
    return deals


REDDIT_DEAL_FEEDS = (
    "https://www.reddit.com/r/dealsforindia/new.json?limit=25",
    "https://www.reddit.com/r/IndianGaming/new.json?limit=25",
)

def fetch_reddit_deals(min_discount: int = 25) -> List[Deal]:
    """Fetch deals from Indian deal-sharing subreddits via Reddit public JSON feeds."""
    raw = os.getenv("REDDIT_FEEDS_ENABLED", "true").strip().lower()
    if raw in {"false", "off", "0", "no"}:
        return []
    deals: List[Deal] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 DealDashBot/1.0"
    }
    for feed_url in REDDIT_DEAL_FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            for child in children:
                pdata = child.get("data", {})
                title = pdata.get("title", "")
                link_url = pdata.get("url", "")
                selftext = pdata.get("selftext", "")

                target_url = None
                if any(dom in link_url.lower() for dom in ("amazon.in", "amzn.to", "amzn.in", "flipkart.com", "fkrt.it")):
                    target_url = link_url
                else:
                    found = re.findall(r'https?://[^\s<>"]+', selftext)
                    for u in found:
                        if any(dom in u.lower() for dom in ("amazon.in", "amzn.to", "amzn.in", "flipkart.com", "fkrt.it")):
                            target_url = u
                            break

                if not target_url:
                    continue

                full_blob = f"{title} {selftext}"
                disc_price, org_price, disc_per = _prices_from_text(full_blob)
                asin_match = AMAZON_ASIN_REGEX.search(target_url)
                asin = asin_match.group(1).upper() if asin_match else None
                deal = _deal_from_fields(
                    title=title,
                    raw_url=target_url,
                    org_price=org_price,
                    disc_price=disc_price,
                    disc_per=disc_per,
                    asin=asin,
                    min_discount=min_discount,
                )
                if not deal and (asin or "amazon.in" in target_url.lower() or "amzn.to" in target_url.lower()):
                    clean_url, deal_id, retailer = expand_and_convert_url(target_url)
                    p_num = parse_rupees(disc_price)
                    prov_disc = max(disc_per, 30) if disc_per > 0 else 30
                    prov_mrp = org_price if org_price else (str(int(round(p_num / (1.0 - prov_disc / 100.0)))) if p_num else None)
                    deal = Deal(
                        id=f"amz-reddit-{asin}" if asin else deal_id,
                        retailer=retailer,
                        title=clean_title(title),
                        url=clean_url,
                        current_price=f"₹{disc_price}" if disc_price else None,
                        original_price=f"₹{prov_mrp}" if prov_mrp else None,
                        discount_percent=prov_disc,
                        canonical_product_id=f"amazon:{asin}" if asin else (deal_id if retailer != "Amazon" else None),
                        category="reddit",
                        image_url=_amazon_image(asin) if asin else None,
                    )
                if deal and deal.is_valid():
                    deal.category = "reddit"
                    deals.append(deal)
        except Exception as exc:
            logger.debug(f"Failed to fetch Reddit deals from {feed_url}: {exc}")
    return deals


AMAZON_MOVERS_URL = "https://www.amazon.in/gp/movers-and-shakers/electronics"

def fetch_amazon_movers_and_shakers(min_discount: int = 25) -> List[Deal]:
    """Lightweight scraper for trending products from Amazon Movers & Shakers. Fails silently if blocked."""
    raw = os.getenv("AMAZON_MOVERS_ENABLED", "true").strip().lower()
    if raw in {"false", "off", "0", "no"}:
        return []
    deals: List[Deal] = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(AMAZON_MOVERS_URL, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        text = resp.text
        if "validateCaptcha" in text or "Type the characters" in text:
            return []

        asin_matches = re.findall(r'href="([^"]*/dp/([A-Z0-9]{10})[^"]*)"', text)
        seen_asins = set()
        for href, asin in asin_matches:
            if asin in seen_asins:
                continue
            seen_asins.add(asin)
            url = f"https://www.amazon.in/dp/{asin}"
            clean_url, deal_id, retailer = expand_and_convert_url(url)
            deal = Deal(
                id=deal_id,
                retailer=retailer,
                title=f"Trending Deal: Amazon Movers & Shakers ({asin})",
                url=clean_url,
                canonical_product_id=f"amazon:{asin}",
                image_url=_amazon_image(asin),
            )
            deals.append(deal)
            if len(deals) >= 5:
                break
    except Exception as exc:
        logger.debug(f"Amazon Movers & Shakers scraper exception: {exc}")
    return deals

def get_amazon_creators_token(client_id: str, client_secret: str) -> Optional[str]:
    """Retrieve and cache OAuth2 Bearer token for Amazon Creators API."""
    now = time.time()
    if _AMAZON_TOKEN_CACHE["token"] and _AMAZON_TOKEN_CACHE["expires_at"] > now + 60:
        return _AMAZON_TOKEN_CACHE["token"]

    try:
        resp = requests.post(
            "https://api.amazon.com/auth/o2/token",
            json={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "creatorsapi::default"
            },
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            _AMAZON_TOKEN_CACHE["token"] = token
            _AMAZON_TOKEN_CACHE["expires_at"] = now + expires_in
            return token
        else:
            logger.warning(f"Amazon Creators OAuth failed ({resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        logger.error(f"Amazon Creators OAuth request error: {e}")

    return None

def fetch_amazon_creators_deals() -> List[Deal]:
    """Search products using Amazon India Creators API."""
    client_id = os.getenv("AMAZON_CREATOR_CLIENT_ID", "").strip()
    client_secret = os.getenv("AMAZON_CREATOR_CLIENT_SECRET", "").strip()
    partner_tag = os.getenv("AMAZON_PARTNER_TAG", "").strip()
    keywords = os.getenv("AMAZON_SEARCH_KEYWORDS", "deals;electronics;headphones").split(";")

    if not client_id or not client_secret or not partner_tag:
        return []

    token = get_amazon_creators_token(client_id, client_secret)
    if not token:
        return []

    deals: List[Deal] = []
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "x-marketplace": "www.amazon.in"
    }

    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        body = {
            "keywords": kw,
            "partnerTag": partner_tag,
            "marketplace": "www.amazon.in",
            "itemCount": 5,
            "resources": [
                "images.primary.large",
                "itemInfo.title",
                "offersV2.listings.price",
                "offersV2.listings.dealDetails"
            ]
        }
        try:
            r = requests.post("https://creatorsapi.amazon/catalog/v1/searchItems", headers=headers, json=body, timeout=12)
            if r.status_code == 403 and "AssociateNotEligible" in r.text:
                logger.info("Amazon Creators API: Associate account currently pending 10 qualifying referral sales. Using live deal feed instead.")
                break
            elif r.status_code == 200:
                data = r.json()
                items = data.get("searchResult", {}).get("items", [])
                for it in items:
                    asin = it.get("asin")
                    item_title = it.get("itemInfo", {}).get("title", {}).get("displayValue")
                    img_url = it.get("images", {}).get("primary", {}).get("large", {}).get("url")
                    if asin and item_title:
                        aff_url = f"https://www.amazon.in/dp/{asin}?tag={partner_tag}"
                        deals.append(Deal(
                            id=f"amz-{asin}",
                            retailer="Amazon",
                            title=clean_title(item_title),
                            url=aff_url,
                            image_url=img_url
                        ))
        except Exception as e:
            logger.error(f"Creators API search error for '{kw}': {e}")

    return deals

def load_local_deals(filepath: Union[str, Path]) -> List[Deal]:
    """Load manually curated deals from a local JSON file."""
    path = Path(filepath)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            items = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {filepath}: {e}")
        return []

    deals: List[Deal] = []
    for item in items:
        if not item.get("title") or not item.get("url"):
            continue
        aff_url, auto_id, auto_retailer = expand_and_convert_url(item["url"])
        deal = Deal(
            id=str(item.get("id") or auto_id),
            retailer=item.get("retailer") or auto_retailer,
            title=clean_title(item["title"]),
            url=aff_url,
            description=item.get("description"),
            current_price=str(item["current_price"]) if item.get("current_price") else None,
            original_price=str(item["original_price"]) if item.get("original_price") else None,
            discount_percent=item.get("discount_percent"),
            category=item.get("category"),
            image_url=item.get("image_url"),
            expires_at=item.get("expires_at"),
            coupon_code=item.get("coupon_code") or item.get("coupon"),
            bank_offer=item.get("bank_offer"),
        )
        if deal.is_valid():
            deals.append(deal)
    return deals

def collect_all_deals() -> List[Deal]:
    """Aggregate and deduplicate deals from all configured sources using parallel fetching."""
    deals: List[Deal] = []
    seen_ids = set()

    # 1. Live automated deals feeds (parallel fetching)
    if os.getenv("FEEDS_ENABLED", "true").lower() == "true":
        min_disc = int(os.getenv("MIN_DISCOUNT_PERCENT", "30"))
        tasks = [
            lambda: fetch_live_feed_deals(min_discount=min_disc),
            lambda: fetch_extra_feeds(min_discount=min_disc),
            lambda: fetch_reddit_deals(min_discount=min_disc),
            lambda: fetch_amazon_movers_and_shakers(min_discount=min_disc),
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            future_to_fn = {executor.submit(fn): fn for fn in tasks}
            for future in concurrent.futures.as_completed(future_to_fn):
                try:
                    res = future.result()
                    for d in res:
                        if d.id not in seen_ids and d.is_valid():
                            seen_ids.add(d.id)
                            deals.append(d)
                except Exception as exc:
                    logger.warning(f"Error fetching from deal source: {exc}")

    # 2. Amazon Creators API
    if os.getenv("AMAZON_API_ENABLED", "false").lower() == "true":
        for d in fetch_amazon_creators_deals():
            if d.id not in seen_ids and d.is_valid():
                seen_ids.add(d.id)
                deals.append(d)

    # 3. Local manual fallback deals
    local_file = os.getenv("LOCAL_DEALS_FILE", "data/deals.json")
    path = Path(local_file)
    if not path.is_absolute():
        path = get_project_root() / path
    for d in load_local_deals(path):
        if d.id not in seen_ids and d.is_valid():
            seen_ids.add(d.id)
            deals.append(d)

    # Filter by allowed retailers:
    # 1. Respect explicit ALLOWED_RETAILERS if set (e.g. ALLOWED_RETAILERS=Amazon)
    # 2. If not specified, automatically skip Flipkart if FLIPKART_AFFILIATE_ID is missing or placeholder
    allowed_raw = os.getenv("ALLOWED_RETAILERS", "").strip()
    if allowed_raw:
        allowed = {r.strip().lower() for r in allowed_raw.split(",") if r.strip()}
        deals = [d for d in deals if d.retailer.lower() in allowed]
    else:
        fk_id = os.getenv("FLIPKART_AFFILIATE_ID", "").strip().lower()
        if not fk_id or fk_id in ["yourflipkartid", "none", "false"]:
            deals = [d for d in deals if d.retailer.lower() != "flipkart"]

    return deals
