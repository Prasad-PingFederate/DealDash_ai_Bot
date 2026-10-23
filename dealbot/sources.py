import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import List, Union
import requests
from dealbot.models import Deal

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def expand_and_convert_url(raw_url: str) -> tuple[str, str, str]:
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

def load_local_deals(filepath: Union[str, Path]) -> List[Deal]:
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
            title=item["title"].strip(),
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

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent

def collect_all_deals() -> List[Deal]:
    local_file = os.getenv("LOCAL_DEALS_FILE", "data/deals.json")
    path = Path(local_file)
    if not path.is_absolute():
        path = get_project_root() / path
    return load_local_deals(path)