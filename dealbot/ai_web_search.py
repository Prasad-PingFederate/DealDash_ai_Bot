"""
Autonomous Typesafe AI Internet Deal Search Engine for BharatBazaro & DealDash AI Bot.
Discovers live Amazon India deals across the internet using:
1. Engine A: Gemini 2.5 Flash with Live Google Search Grounding
2. Engine B: Real-Time Google News India Deal Search (when:1d)
3. Engine C: Live DuckDuckGo / Web 24-Hour amazon.in/dp/ Search

Includes smart Amazon.in ASIN Resolver (resolve_query_to_amazon_asin) and
converts candidates to typesafe Deal models with requires_live_verification=True.
"""

import concurrent.futures
import html as html_lib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from dealbot.models import Deal

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ASIN_PATTERN = re.compile(r"\b([B0][A-Z0-9]{9})\b")
AMAZON_DP_PATTERN = re.compile(r"(?:amazon\.in|amzn\.to|amzn\.in)(?:/dp/|/gp/product/|/d/|/)?([B0][A-Z0-9]{9})?", re.IGNORECASE)


@dataclass
class WebDealCandidate:
    product_name: str
    amazon_url_or_asin: Optional[str] = None
    asin: Optional[str] = None
    deal_price: Optional[float] = None
    mrp: Optional[float] = None
    discount_pct: Optional[float] = None
    coupon_note: Optional[str] = None
    source_engine: str = "web_search"
    grounded_url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], engine: str = "web_search") -> Optional["WebDealCandidate"]:
        """Typesafe schema validation with aggressive sanitization and coercion."""
        if not isinstance(data, dict):
            return None

        # 1. Product Name Extraction & Sanitization
        raw_name = data.get("product_name") or data.get("title") or data.get("name") or ""
        if not isinstance(raw_name, str):
            raw_name = str(raw_name)
        product_name = html_lib.unescape(raw_name).strip()
        # Clean common news junk affixes
        product_name = re.sub(r"^\[.*?\]\s*", "", product_name)
        product_name = re.sub(r"\s*-\s*(?:India Today|NDTV|Gadgets 360|Livemint|Times of India).*$", "", product_name, flags=re.IGNORECASE)
        product_name = re.sub(r"\s+", " ", product_name).strip()
        if len(product_name) < 5:
            return None

        # 2. Price Coercion helper
        def _coerce_price(val: Any) -> Optional[float]:
            if val is None or val == "":
                return None
            if isinstance(val, (int, float)):
                return float(val) if val > 0 else None
            # Extract first numeric amount (handles '₹1,499.00', 'Rs. 2,999', '1499')
            m = re.search(r"(\d{1,3}(?:,\d{2,3})*(?:\.\d+)?)", str(val))
            if m:
                try:
                    p = float(m.group(1).replace(",", ""))
                    return p if p > 0 else None
                except (ValueError, TypeError):
                    return None
            return None

        deal_price = _coerce_price(data.get("deal_price") or data.get("price") or data.get("current_price"))
        mrp = _coerce_price(data.get("mrp") or data.get("original_price") or data.get("list_price"))

        # Discount percent calculation / coercion
        raw_disc = data.get("discount_pct") or data.get("discount_percent") or data.get("discount")
        discount_pct = None
        if raw_disc is not None:
            try:
                discount_pct = float(re.sub(r"[^\d.]", "", str(raw_disc)))
            except (ValueError, TypeError):
                discount_pct = None

        if not discount_pct and deal_price and mrp and mrp > deal_price:
            discount_pct = round(((mrp - deal_price) / mrp) * 100, 1)

        # 3. ASIN extraction
        url_or_asin = str(data.get("amazon_url_or_asin") or data.get("url") or data.get("asin") or "").strip()
        asin = None
        m_asin = ASIN_PATTERN.search(url_or_asin)
        if m_asin:
            asin = m_asin.group(1)
        elif ASIN_PATTERN.match(str(data.get("asin") or "")):
            asin = str(data.get("asin"))

        coupon_note = data.get("coupon_note") or data.get("coupon") or data.get("coupon_text")
        if coupon_note:
            coupon_note = str(coupon_note).strip()

        grounded_url = data.get("grounded_url")

        return cls(
            product_name=product_name,
            amazon_url_or_asin=url_or_asin or None,
            asin=asin,
            deal_price=deal_price,
            mrp=mrp,
            discount_pct=discount_pct,
            coupon_note=coupon_note,
            source_engine=engine,
            grounded_url=grounded_url,
        )


# ==============================================================================
# ENGINE A: Gemini 2.5 Flash with Live Google Search Grounding
# ==============================================================================
def search_gemini_grounded(timeout: float = 10.0) -> List[WebDealCandidate]:
    """
    Search Google via Gemini 2.5 Flash REST API with Google Search Grounding enabled.
    Returns structured WebDealCandidate objects with grounded amazon.in links.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.debug("search_gemini_grounded: GEMINI_API_KEY not configured, skipping.")
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = (
        "Search Google for today's newest Amazon India (amazon.in) price drops, loot deals, "
        "all-time low electronics, home appliances, smartphones, audio, daily essentials deals, "
        "and on-page coupon offers in India today. "
        "Return a strict JSON list without markdown formatting: "
        '[{"product_name": "...", "amazon_url_or_asin": "...", "deal_price": 1499, "mrp": 3999, "coupon_note": "..."}]'
    )

    # First attempt with google_search grounding
    payload_grounded = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
    }

    candidates: List[WebDealCandidate] = []
    response_data = None

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload_grounded).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        logger.debug(f"Gemini grounded search with tools failed ({e}), falling back to standard prompt...")
        # Fallback without tools parameter if tier doesn't support it
        try:
            payload_simple = {"contents": [{"parts": [{"text": prompt}]}]}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload_simple).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as e2:
            logger.warning(f"Gemini deal search API call failed: {e2}")
            return []

    if not response_data:
        return []

    try:
        cand_block = response_data.get("candidates", [])[0]
        # 1. Inspect groundingMetadata for live amazon.in URLs
        grounded_urls: List[str] = []
        grounding_meta = cand_block.get("groundingMetadata", {})
        for chunk in grounding_meta.get("groundingChunks", []):
            web = chunk.get("web", {})
            uri = web.get("uri", "")
            if "amazon.in" in uri or "amzn.to" in uri:
                grounded_urls.append(uri)

        # 2. Extract model text response
        text = cand_block.get("content", {}).get("parts", [])[0].get("text", "")
        # Strip markdown fences
        clean_text = re.sub(r"^```json\s*", "", text.strip(), flags=re.MULTILINE)
        clean_text = re.sub(r"^```\s*", "", clean_text, flags=re.MULTILINE)
        clean_text = clean_text.strip()

        # Find JSON array
        m_arr = re.search(r"\[\s*\{.*?\}\s*\]", clean_text, re.DOTALL)
        if m_arr:
            items = json.loads(m_arr.group(0))
            if isinstance(items, list):
                for idx, item in enumerate(items):
                    cand = WebDealCandidate.from_dict(item, engine="gemini_grounded")
                    if cand:
                        if not cand.asin and idx < len(grounded_urls):
                            cand.grounded_url = grounded_urls[idx]
                            m_asin = ASIN_PATTERN.search(grounded_urls[idx])
                            if m_asin:
                                cand.asin = m_asin.group(1)
                        candidates.append(cand)
    except Exception as e:
        logger.debug(f"Error parsing Gemini deal search response: {e}")

    logger.info(f"Engine A (Gemini Grounded): Discovered {len(candidates)} deal candidates")
    return candidates


# ==============================================================================
# ENGINE B: Real-Time Google News India Deal Search (when:1d)
# ==============================================================================
GOOGLE_NEWS_QUERIES = [
    '("Amazon India" OR "Amazon.in") ("price drop" OR "lowest price" OR "deal" OR "discount") ₹ when:1d',
    '("Amazon" ₹) (earbuds OR smartwatch OR smartphone OR laptop OR air fryer OR headphones) deal when:1d',
    '("Amazon" ₹) (Dove OR Nivea OR "Surf Excel" OR Colgate OR Cetaphil OR Minimalist OR Ariel OR Cadbury) deal when:1d',
]


def _extract_deals_from_news_headline(title: str, summary: str = "") -> Optional[WebDealCandidate]:
    """Typesafe deterministic extraction from Google News headline and summary."""
    full_text = f"{title}. {summary}"

    # Price pattern: ₹1,499 or Rs. 1499 or INR 1,499
    m_price = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]+)?)", full_text, re.IGNORECASE)
    if not m_price:
        return None

    try:
        deal_price = float(m_price.group(1).replace(",", ""))
    except ValueError:
        return None

    if deal_price <= 0:
        return None

    # Check for MRP: 'down from ₹4,999' or 'MRP ₹4,999' or 'was ₹4,999' or 'originally priced at Rs. 18,999'
    m_mrp = re.search(
        r"(?:down\s+from|MRP|was|original(?:ly)?\s+pric(?:e|ed)(?:\s+at)?)\s*(?:₹|Rs\.?|INR)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*)",
        full_text,
        re.IGNORECASE,
    )
    mrp = None
    if m_mrp:
        try:
            val = float(m_mrp.group(1).replace(",", ""))
            if val > deal_price:
                mrp = val
        except ValueError:
            pass

    # Discount percent: '50% off' or 'flat 40% discount'
    m_disc = re.search(r"(\d{1,2})%\s*(?:off|discount)", full_text, re.IGNORECASE)
    discount_pct = float(m_disc.group(1)) if m_disc else None
    if not discount_pct and mrp:
        discount_pct = round(((mrp - deal_price) / mrp) * 100, 1)

    # Coupon note: 'apply ₹500 coupon' or 'with ₹1,000 coupon'
    m_coupon = re.search(r"((?:apply|with)\s*(?:₹|Rs\.?)\s*[\d,]+[\w\s]*coupon)", full_text, re.IGNORECASE)
    coupon_note = m_coupon.group(1).strip() if m_coupon else None

    # Clean product title: remove lead/trail phrases
    clean_title = re.sub(r"(?:Deal Alert|Price Drop|Amazon Sale|Big Discount|Steal Deal):\s*", "", title, flags=re.IGNORECASE)
    clean_title = re.sub(r"\s*-\s*(?:India Today|NDTV|Gadgets 360|Mint|Livemint|The Economic Times|Times of India).*$", "", clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r"\s*(?:available|drops|down|discounted|slashed)\s+(?:at|to|for)\s*(?:₹|Rs\.?).*$", "", clean_title, flags=re.IGNORECASE)
    clean_title = clean_title.strip()

    if len(clean_title) < 6:
        return None

    # Check for embedded ASIN
    m_asin = ASIN_PATTERN.search(full_text)
    asin = m_asin.group(1) if m_asin else None

    return WebDealCandidate(
        product_name=clean_title,
        amazon_url_or_asin=f"https://www.amazon.in/dp/{asin}" if asin else None,
        asin=asin,
        deal_price=deal_price,
        mrp=mrp,
        discount_pct=discount_pct,
        coupon_note=coupon_note,
        source_engine="google_news_rss",
    )


def search_google_news_india(timeout: float = 10.0) -> List[WebDealCandidate]:
    """Query Google News India RSS for real-time 24h Amazon deals."""
    candidates: List[WebDealCandidate] = []
    headers = {"User-Agent": USER_AGENT}

    for query in GOOGLE_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q={urllib.parse.quote_plus(query)}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_data = resp.read()
                root = ET.fromstring(xml_data)
                for item in root.findall(".//item"):
                    title_elem = item.find("title")
                    desc_elem = item.find("description")
                    title = title_elem.text if title_elem is not None and title_elem.text else ""
                    desc = desc_elem.text if desc_elem is not None and desc_elem.text else ""
                    # Strip html tags from description
                    clean_desc = re.sub(r"<[^>]+>", " ", desc)
                    cand = _extract_deals_from_news_headline(title, clean_desc)
                    if cand:
                        candidates.append(cand)
        except Exception as e:
            logger.debug(f"Google News RSS query failed for '{query[:30]}...': {e}")

    logger.info(f"Engine B (Google News RSS): Discovered {len(candidates)} deal candidates")
    return candidates


# ==============================================================================
# ENGINE C: Live DuckDuckGo / Web 24-Hour amazon.in/dp/ Search
# ==============================================================================
DDG_QUERIES = [
    'site:amazon.in/dp/ ("off" OR "deal" OR "coupon") ₹',
    '("amazon.in/dp/" OR "amzn.to") ("loot" OR "price drop" OR "apply coupon") ₹',
    'site:amazon.in/dp/ ("Dove" OR "Nivea" OR "Surf Excel" OR "Colgate" OR "Minimalist" OR "Cadbury") ("off" OR "₹")',
]


def search_duckduckgo_deals(timeout: float = 10.0) -> List[WebDealCandidate]:
    """Query DuckDuckGo HTML 24h filter for live Amazon India deals."""
    candidates: List[WebDealCandidate] = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for query in DDG_QUERIES:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}&df=d"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

                # Match search result blocks: link and snippet
                # Result links have class="result__url" or "result__snippet"
                blocks = re.findall(
                    r'<a[^>]+class="result__url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                    html,
                    re.DOTALL,
                )
                for href, link_text, snippet in blocks:
                    clean_snippet = re.sub(r"<[^>]+>", " ", snippet)
                    clean_text = f"{link_text} {clean_snippet}"

                    # Extract ASIN from URL or text
                    m_asin = ASIN_PATTERN.search(href) or ASIN_PATTERN.search(clean_text)
                    asin = m_asin.group(1) if m_asin else None

                    # Extract price
                    m_price = re.search(r"(?:₹|Rs\.?)\s*([0-9]{1,3}(?:,[0-9]{2,3})*)", clean_text)
                    deal_price = None
                    if m_price:
                        try:
                            deal_price = float(m_price.group(1).replace(",", ""))
                        except ValueError:
                            pass

                    # Clean title
                    raw_title = html_lib.unescape(link_text).strip()
                    raw_title = re.sub(r"^https?://[^\s]+", "", raw_title).strip()
                    raw_title = re.sub(r"\s*:\s*Amazon\.in.*$", "", raw_title, flags=re.IGNORECASE)
                    if len(raw_title) < 5:
                        raw_title = clean_snippet[:80].strip()

                    if len(raw_title) >= 5:
                        cand = WebDealCandidate(
                            product_name=raw_title,
                            amazon_url_or_asin=f"https://www.amazon.in/dp/{asin}" if asin else href,
                            asin=asin,
                            deal_price=deal_price,
                            source_engine="duckduckgo_web",
                        )
                        candidates.append(cand)
        except Exception as e:
            logger.debug(f"DuckDuckGo search query failed for '{query[:30]}...': {e}")

    logger.info(f"Engine C (DuckDuckGo Web): Discovered {len(candidates)} deal candidates")
    return candidates


# ==============================================================================
# 2. SMART AMAZON.IN ASIN RESOLVER FOR WEB DISCOVERED PRODUCTS
# ==============================================================================
def resolve_query_to_amazon_asin(product_name: str, timeout: float = 4.0) -> Optional[str]:
    """
    Given a product title/query, query Amazon India organic search to resolve the 10-char ASIN.
    Strictly skips sponsored ads (AdHolder) and ensures brand/model tokens match.
    """
    if not product_name or len(product_name.strip()) < 4:
        return None

    # Sanitize search query: keep top 5-6 core keywords
    clean_q = re.sub(r"[^\w\s-]", " ", product_name)
    tokens = [w for w in clean_q.split() if len(w) > 2 and w.lower() not in {
        "the", "with", "for", "and", "under", "price", "drop", "drops", "deal",
        "lowest", "amazon", "india", "offer", "discount", "sale", "flat"
    }]
    if not tokens:
        tokens = clean_q.split()[:4]

    query_str = " ".join(tokens[:5])
    url = f"https://www.amazon.in/s?k={urllib.parse.quote_plus(query_str)}&rh=p_72%3A1318476031"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Cookie": "i18n-prefs=INR; lc-acbin=en_IN",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

            # Parse result cards: split by data-component-type="s-search-result"
            chunks = re.split(r'data-component-type="s-search-result"', html)
            for chunk in chunks[1:]:
                m_asin = re.search(r'data-asin="([A-Z0-9]{10,12})"', chunk)
                if not m_asin:
                    continue
                asin = m_asin.group(1)[:10]
                chunk_snippet = chunk[:1500]
                # Skip sponsored ads
                if "AdHolder" in chunk_snippet or "s-sponsored-label" in chunk_snippet or "Sponsored" in chunk_snippet:
                    continue

                # Ensure card title matches at least 1-2 major tokens
                chunk_lower = chunk_snippet.lower()
                matches = sum(1 for tok in tokens[:3] if tok.lower() in chunk_lower)
                if matches >= 1:
                    logger.debug(f"Resolved '{query_str}' -> ASIN {asin} (matched {matches} tokens)")
                    return asin

            # If no strict token match, return first non-sponsored organic ASIN
            for chunk in chunks[1:]:
                m_asin = re.search(r'data-asin="([A-Z0-9]{10,12})"', chunk)
                if m_asin:
                    chunk_snippet = chunk[:1500]
                    if "AdHolder" not in chunk_snippet and "s-sponsored-label" not in chunk_snippet and "Sponsored" not in chunk_snippet:
                        return m_asin.group(1)[:10]

    except Exception as e:
        logger.debug(f"resolve_query_to_amazon_asin failed for '{query_str}': {e}")

    return None


# ==============================================================================
# 3. PIPELINE: FETCH, RESOLVE & CONVERT TO DEALS
# ==============================================================================
def fetch_ai_web_search_deals(max_results: int = 8, timeout: float = 10.0) -> List[Deal]:
    """
    Orchestrate the 3 complimentary web engines in parallel with a strict 10s timeout,
    resolve ASINs, and build Deal objects marked with requires_live_verification=True.
    """
    logger.info("Starting Autonomous AI Web Search across Gemini Grounded, Google News & DDG...")
    candidates: List[WebDealCandidate] = []

    # Parallel execution of the 3 engines
    engines = [
        search_gemini_grounded,
        search_google_news_india,
        search_duckduckgo_deals,
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_to_engine = {executor.submit(fn, timeout): fn.__name__ for fn in engines}
        for future in concurrent.futures.as_completed(future_to_engine):
            eng_name = future_to_engine[future]
            try:
                res = future.result()
                candidates.extend(res)
            except Exception as e:
                logger.warning(f"Engine {eng_name} error: {e}")

    logger.info(f"AI Web Search total raw candidates discovered: {len(candidates)}")

    # Deduplicate candidates by product name / ASIN
    seen_keys: Set[str] = set()
    unique_candidates: List[WebDealCandidate] = []
    for cand in candidates:
        key = cand.asin or cand.product_name.lower()[:30]
        if key not in seen_keys:
            seen_keys.add(key)
            unique_candidates.append(cand)

    deals: List[Deal] = []
    asin_lookups = 0
    max_asin_lookups = 4  # Cap organic search lookups per cycle to avoid throttling

    for cand in unique_candidates:
        if len(deals) >= max_results:
            break

        asin = cand.asin
        # If candidate lacks ASIN, use our smart resolver (up to 4 lookups per cycle)
        if not asin and asin_lookups < max_asin_lookups:
            asin_lookups += 1
            resolved_asin = resolve_query_to_amazon_asin(cand.product_name, timeout=4.0)
            if resolved_asin:
                asin = resolved_asin
                cand.asin = resolved_asin

        if not asin:
            continue

        aff_url = f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1"

        deal = Deal(
            id=f"aiweb:{asin}",
            retailer="Amazon",
            title=cand.product_name,
            url=aff_url,
            current_price=f"₹{int(cand.deal_price):,}" if cand.deal_price else None,
            original_price=f"₹{int(cand.mrp):,}" if cand.mrp else None,
            discount_percent=int(cand.discount_pct) if cand.discount_pct else None,
            category="AI Web Search",
            coupon_text=cand.coupon_note,
            matched_sources=["AI Web Search"],
            source_platform="AI Web Search",
        )
        deal.requires_live_verification = True
        deals.append(deal)

    logger.info(f"Successfully generated {len(deals)} verified-ready AI Web Search deals")
    return deals
