import html
import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import List, Optional, Union
import xml.etree.ElementTree as ET
import requests
from dealbot.models import Deal

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# In-memory token cache for Amazon Creators API
_AMAZON_TOKEN_CACHE = {"token": None, "expires_at": 0.0}

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent

def clean_title(title: str) -> str:
    """Clean promotional noise, coupons, and trailing price/retailer tags from titles."""
    t = title.strip()
    # Strip [Apply X% Coupon] or similar tags at start
    t = re.sub(r"^\[.*?(?:coupon|offer|flat).*?\]\s*", "", t, flags=re.IGNORECASE)
    # Strip trailing price and retailer suffix like 'Rs. 799 - Amazon' or '₹ 899 - Amazon'
    t = re.sub(r"\s*(?:Rs\.?|₹)\s*[\d,]+(?:\s*-\s*[A-Za-z]+)?$", "", t, flags=re.IGNORECASE)
    # Strip trailing retailer like ' - Amazon' or ' - Flipkart'
    t = re.sub(r"\s*-\s*(Amazon|Flipkart|Myntra|Ajio)$", "", t, flags=re.IGNORECASE)
    return t.strip()

def expand_and_convert_url(raw_url: str) -> tuple[str, str, str]:
    """Extract ASIN or PID and automatically inject user affiliate tracking parameters."""
    amazon_tag = os.getenv("AMAZON_PARTNER_TAG", "").strip()
    flipkart_id = os.getenv("FLIPKART_AFFILIATE_ID", "").strip()

    url = raw_url.strip()
    if any(short in url for short in ["amzn.to", "amzn.in", "fkrt.it", "bit.ly"]):
        try:
            resp = requests.head(url, allow_redirects=True, timeout=10, headers={"User-Agent": USER_AGENT})
            url = resp.url
        except requests.RequestException as e:
            logger.warning(f"Could not expand short link {url}: {e}")

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()

    if "amazon." in domain:
        asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path, re.IGNORECASE)
        if asin_match:
            asin = asin_match.group(1).upper()
            clean_url = f"https://{parsed.netloc}/dp/{asin}"
            if amazon_tag:
                clean_url += f"?tag={amazon_tag}"
            return clean_url, f"amz-{asin}", "Amazon"

    if "flipkart.com" in domain:
        qs = urllib.parse.parse_qs(parsed.query)
        pid = qs.get("pid", [None])[0]
        new_qs = {}
        if pid:
            new_qs["pid"] = pid
        if flipkart_id:
            new_qs["affid"] = flipkart_id
        query_str = urllib.parse.urlencode(new_qs)
        clean_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query_str, ""))
        deal_id = f"fk-{pid}" if pid else f"fk-{abs(hash(parsed.path))}"
        return clean_url, deal_id, "Flipkart"

    return url, f"deal-{abs(hash(url))}", "Online Store"

def fetch_live_feed_deals(min_discount: int = 25) -> List[Deal]:
    """Fetch live deals from real-time aggregator feeds and inject affiliate tags."""
    feed_url = os.getenv("DEALS_FEED_URL", "https://www.indiafreestuff.in/feed")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml"
    }
    deals: List[Deal] = []
    try:
        r = requests.get(feed_url, headers=headers, timeout=12)
        if r.status_code != 200:
            logger.warning(f"Live deals feed returned status {r.status_code}")
            return []

        root = ET.fromstring(r.content)
        items = root.findall(".//all_deals")
        for item in items:
            raw_title = item.findtext("title") or ""
            raw_url = item.findtext("product_url") or ""
            org_price = item.findtext("org_price") or ""
            disc_price = item.findtext("discount_price") or ""
            disc_per_str = item.findtext("discount_per") or "0"
            asin = (item.findtext("asin") or "").strip()
            is_expired = (item.findtext("expire") or "0") == "1"

            if not raw_title or not raw_url or is_expired:
                continue

            try:
                disc_per = int(float(disc_per_str))
            except ValueError:
                disc_per = 0

            if disc_per < min_discount:
                continue

            aff_url, deal_id, retailer = expand_and_convert_url(raw_url)
            title = clean_title(raw_title)

            # High-resolution product image
            image_url = None
            if retailer == "Amazon" and asin:
                image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX500_.jpg"

            deal = Deal(
                id=deal_id,
                retailer=retailer,
                title=title,
                url=aff_url,
                current_price=f"₹{disc_price}" if disc_price else None,
                original_price=f"₹{org_price}" if org_price else None,
                discount_percent=disc_per if disc_per > 0 else None,
                image_url=image_url
            )
            if deal.is_valid():
                deals.append(deal)
        logger.info(f"Ingested {len(deals)} valid deals from live aggregator feed.")
    except Exception as e:
        logger.error(f"Error fetching live deals feed: {e}")

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
        )
        if deal.is_valid():
            deals.append(deal)
    return deals

def collect_all_deals() -> List[Deal]:
    """Aggregate and deduplicate deals from all configured sources."""
    deals: List[Deal] = []
    seen_ids = set()

    # 1. Live automated deals feeds
    if os.getenv("FEEDS_ENABLED", "true").lower() == "true":
        min_disc = int(os.getenv("MIN_DISCOUNT_PERCENT", "30"))
        for d in fetch_live_feed_deals(min_discount=min_disc):
            if d.id not in seen_ids and d.is_valid():
                seen_ids.add(d.id)
                deals.append(d)

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
