"""
Image and Title Resolver and Cache Engine for BharatBazaro.com.
Detects Amazon 1x1 dummy pixels (43-byte transparent GIFs on m.media-amazon.com/images/P/{asin}.jpg)
and placeholder titles like 'Amazon Deal (B0...)', resolving genuine high-res product images (images/I/...)
and real product titles directly from Amazon India, Keepa API, or URL slugs.
Persists all resolved images and titles in data/image_cache.json across 24/7 runs.
"""

import json
import logging
import os
import re
import urllib.request
import html as html_lib
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]


def load_image_cache(cache_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load existing image cache from data/image_cache.json."""
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load image cache: {e}")
    return {}


def save_image_cache(cache: Dict[str, Dict[str, Any]], cache_path: Path):
    """Save image cache to data/image_cache.json."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save image cache: {e}")


def is_dummy_pixel(url: str, timeout: float = 3.5) -> bool:
    """Check if an image URL returns Amazon's 43-byte transparent dummy 1x1 pixel."""
    if not url or not url.startswith("http"):
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENTS[0]})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(256)
            if len(data) <= 100:
                return True
            return False
    except Exception:
        return True


def clean_amazon_scraped_title(raw_t: str) -> Optional[str]:
    """Clean scraped Amazon title and strip site suffixes."""
    if not raw_t or not isinstance(raw_t, str):
        return None
    clean_t = html_lib.unescape(raw_t).strip()
    clean_t = re.sub(r"^Amazon\.in:\s*Buy\s*", "", clean_t, flags=re.IGNORECASE)
    clean_t = re.sub(r"\s+at\s+Low\s+Prices\s+in\s+India.*$", "", clean_t, flags=re.IGNORECASE)
    clean_t = re.sub(r"\s*:\s*Amazon\.in.*$", "", clean_t, flags=re.IGNORECASE)
    clean_t = re.sub(r"\s*\|\s*Amazon\.in.*$", "", clean_t, flags=re.IGNORECASE)
    clean_t = re.sub(r"\s+", " ", clean_t).strip()
    if (
        len(clean_t) >= 5
        and "Robot Check" not in clean_t
        and "Page Not Found" not in clean_t
        and clean_t.lower() != "amazon.in"
        and not clean_t.lower().startswith("amazon deal")
    ):
        return clean_t
    return None


def fetch_metadata_from_keepa(asin: str, timeout: float = 5.0) -> Dict[str, Optional[str]]:
    """Fetch product metadata from Keepa API if KEEPA_API_KEY is configured in env."""
    keepa_key = os.getenv("KEEPA_API_KEY", "").strip()
    if not keepa_key:
        return {"title": None, "image_url": None}
    try:
        url = f"https://api.keepa.com/product?key={keepa_key}&domain=10&asin={asin}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENTS[0]})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            products = data.get("products", [])
            if products:
                prod = products[0]
                raw_t = prod.get("title")
                clean_t = clean_amazon_scraped_title(raw_t) if raw_t else None
                imgs = prod.get("imagesCSV", "")
                img_url = f"https://m.media-amazon.com/images/I/{imgs.split(',')[0]}" if imgs else None
                return {"title": clean_t, "image_url": img_url}
    except Exception as e:
        logger.debug(f"Keepa lookup failed for {asin}: {e}")
    return {"title": None, "image_url": None}


def fetch_real_amazon_image_and_title(
    asin: str,
    timeout: float = 6.0,
    url_slug_hint: Optional[str] = None
) -> Dict[str, Optional[str]]:
    """
    Fetch Amazon product page and extract real image (images/I/...) and title.
    Supports multi-tier fallbacks:
    1. Desktop HTML (<span id="productTitle">, og:title, twitter:title, ld+json, <title>)
    2. Mobile User-Agent fallback to avoid datacenter IP bot-check blocks
    3. Keepa API (if KEEPA_API_KEY is set)
    4. URL slug extraction (if url_slug_hint or deal URL is provided)
    """
    url = f"https://www.amazon.in/dp/{asin}"
    result = {"image_url": None, "title": None}

    # Helper to parse HTML body for title & high-res image
    def _extract_from_html(html: str) -> None:
        if not result["title"]:
            t_span = re.search(r'<span id="productTitle"[^>]*>([^<]+)</span>', html)
            if t_span:
                result["title"] = clean_amazon_scraped_title(t_span.group(1))

        if not result["title"]:
            m_og = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if m_og:
                result["title"] = clean_amazon_scraped_title(m_og.group(1))

        if not result["title"]:
            m_tw = re.search(r'<meta name="twitter:title" content="([^"]+)"', html)
            if m_tw:
                result["title"] = clean_amazon_scraped_title(m_tw.group(1))

        if not result["title"]:
            m_ld = re.search(r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m_ld:
                try:
                    ld = json.loads(m_ld.group(1))
                    name = ld.get("name") if isinstance(ld, dict) else None
                    if name:
                        result["title"] = clean_amazon_scraped_title(name)
                except Exception:
                    pass

        if not result["title"]:
            t_tag = re.search(r'<title>([^<]+)</title>', html)
            if t_tag:
                result["title"] = clean_amazon_scraped_title(t_tag.group(1))

        if not result["image_url"]:
            m = re.search(r'data-old-hires="([^"]+)"', html)
            if m and m.group(1).startswith("http"):
                result["image_url"] = m.group(1)
            else:
                m2 = re.search(r'"large":"([^"]+)"', html)
                if m2 and m2.group(1).startswith("http"):
                    result["image_url"] = m2.group(1)
                else:
                    m3 = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                    if m3 and m3.group(1).startswith("http") and "amazon.com" in m3.group(1):
                        result["image_url"] = m3.group(1)
                    else:
                        imgs = re.findall(r'https://m\.media-amazon\.com/images/I/[a-zA-Z0-9%_-]+\.jpg', html)
                        if imgs:
                            result["image_url"] = imgs[0]

    # 1. Primary Desktop request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENTS[0],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            _extract_from_html(html)
    except Exception as e:
        logger.debug(f"Primary desktop Amazon fetch failed for {asin}: {e}")

    # 2. Secondary Mobile request (bypasses many datacenter desktop CAPTCHA walls)
    if not result["title"] or not result["image_url"]:
        try:
            mobile_url = f"https://www.amazon.in/gp/aw/d/{asin}"
            req_m = urllib.request.Request(mobile_url, headers={
                "User-Agent": USER_AGENTS[3],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req_m, timeout=timeout) as resp_m:
                html_m = resp_m.read().decode("utf-8", errors="ignore")
                _extract_from_html(html_m)
        except Exception as e:
            logger.debug(f"Mobile Amazon fetch failed for {asin}: {e}")

    # 3. Keepa API fallback
    if not result["title"] or not result["image_url"]:
        keepa_meta = fetch_metadata_from_keepa(asin)
        if keepa_meta.get("title") and not result["title"]:
            result["title"] = keepa_meta["title"]
        if keepa_meta.get("image_url") and not result["image_url"]:
            result["image_url"] = keepa_meta["image_url"]

    # 4. URL slug fallback
    if not result["title"] and url_slug_hint:
        try:
            from dealbot.sources import extract_title_from_amazon_url_slug
            slug_t = extract_title_from_amazon_url_slug(url_slug_hint)
            if slug_t:
                result["title"] = slug_t
        except Exception:
            pass

    return result


def resolve_deal_images(deals: List[Dict[str, Any]], root_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Ensure every deal in deals has a genuine, non-blank product image and a real product title.
    Uses data/image_cache.json for fast caching across cycles and recovers real titles from
    Keepa, URL slugs, and mobile metadata tags when datacenter IP challenges occur.
    """
    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent

    cache_path = root_dir / "data" / "image_cache.json"
    cache = load_image_cache(cache_path)
    cache_dirty = False
    live_fetches_done = 0
    max_live_fetches = int(os.getenv("MAX_IMAGE_RESOLVE_FETCHES", "8"))

    for d in deals:
        asin = d.get("asin")
        if not asin:
            continue

        current_img = d.get("image_url") or ""
        current_title = d.get("title") or ""
        is_generic_title = (
            not current_title
            or current_title.startswith("Amazon Deal (")
            or current_title.startswith("Amazon Deal")
            or current_title.startswith("Trending Amazon Deal")
            or len(current_title) < 15
        )

        cached_meta = cache.get(asin, {})
        cached_img = cached_meta.get("image_url")
        cached_title = cached_meta.get("title")

        has_cached_image = bool(cached_img and "/images/P/" not in cached_img)
        has_cached_title = bool(
            cached_title
            and not cached_title.startswith("Amazon Deal")
            and not cached_title.startswith("Trending Amazon Deal")
            and len(cached_title) >= 10
        )

        # Apply cache if available
        if has_cached_image:
            d["image_url"] = cached_img
        if is_generic_title and has_cached_title:
            d["title"] = cached_title
            current_title = d["title"]
            is_generic_title = False

        # Check if we need to fetch live from Amazon
        needs_img_fetch = not d.get("image_url") or "/images/P/" in (d.get("image_url") or "")
        if needs_img_fetch and d.get("image_url"):
            # Check if current P-url is dummy pixel
            if not is_dummy_pixel(d["image_url"]):
                needs_img_fetch = False

        if (is_generic_title or needs_img_fetch) and live_fetches_done < max_live_fetches:
            live_fetches_done += 1
            slug_hint = d.get("url") or d.get("affiliate_url") or ""
            meta = fetch_real_amazon_image_and_title(asin, url_slug_hint=slug_hint)
            new_img = meta.get("image_url")
            new_title = meta.get("title")

            if new_img and "/images/P/" not in new_img:
                d["image_url"] = new_img
            elif not d.get("image_url"):
                d["image_url"] = f"https://m.media-amazon.com/images/P/{asin}.jpg"

            if new_title and (is_generic_title or not d.get("title") or d["title"].startswith("Amazon Deal")):
                d["title"] = new_title
                current_title = new_title

            # Preserve existing high-value cached data if live fetch was challenged
            existing_cached = cache.get(asin, {})
            final_cached_title = d.get("title")
            if (
                not final_cached_title
                or final_cached_title.startswith("Amazon Deal")
                or final_cached_title.startswith("Trending Amazon Deal")
            ):
                final_cached_title = existing_cached.get("title")

            final_cached_img = d.get("image_url")
            if not final_cached_img or "/images/P/" in final_cached_img:
                final_cached_img = existing_cached.get("image_url")

            cache[asin] = {
                "image_url": final_cached_img,
                "title": final_cached_title,
            }
            cache_dirty = True
            logger.info(f"Resolved Amazon metadata for {asin}: title='{(d.get('title') or '')[:40]}...', img={d.get('image_url')}")

        # Re-run brand & category enrichment if brand is missing or default "Amazon"
        real_title = d.get("title") or ""
        if real_title and not real_title.startswith("Amazon Deal") and not real_title.startswith("Trending Amazon Deal"):
            try:
                from dealbot.enricher import detect_brand, detect_category
                current_brand = d.get("brand") or ""
                if not current_brand or current_brand.lower() in ["amazon", "generic", ""]:
                    detected_b = detect_brand(real_title)
                    if detected_b and detected_b.lower() != "amazon":
                        d["brand"] = detected_b
                current_cat = (d.get("category") or "").lower()
                if not current_cat or current_cat in ["deals", "general", "other"]:
                    detected_cat = detect_category(real_title)
                    if detected_cat:
                        d["category"] = detected_cat
            except Exception:
                pass

    if cache_dirty:
        save_image_cache(cache, cache_path)

    # Sort strictly: fresh deals first, newest verified timestamp first, then verified images and score
    deals.sort(
        key=lambda x: (
            1 if x.get("is_fresh", True) else 0,
            x.get("last_verified_at_epoch") or x.get("posted_at_epoch") or x.get("updated_at") or 0,
            1 if x.get("image_url") and not is_dummy_pixel(x.get("image_url"), timeout=1.0) else 0,
            x.get("score") or x.get("trending_score") or 0,
        ),
        reverse=True
    )

    return deals
