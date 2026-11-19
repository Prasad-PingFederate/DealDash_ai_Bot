# dealbot/monetizer.py
"""Monetization and URL Canonicalization Engine for DealBot.

Handles:
1. Safe redirect unshortening (max 5 hops, timeout protection) for short domains (amzn.to, fkrt.it, bit.ly, etc.).
2. Competitor affiliate and tracking parameter stripping (tag, affid, ascsubtag, utm_*, ref, pf_rd_*, pd_rd_*).
3. Canonical Amazon India ASIN extraction and tag injection:
   https://www.amazon.in/dp/{ASIN}?tag={AMAZON_TAG}&ascsubtag={source}_{verdict}
4. Non-Amazon store affiliate wrapping (Flipkart, Myntra, Ajio, TataCliq, Nykaa) via Cuelinks / EarnKaro / templates.
5. Structured NamedTuple metadata: (monetized_url, store, is_monetized, error_reason).
6. Auto-posting gate preventing unmonetized links unless allow_unmonetized=True.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import NamedTuple, Optional, Sequence, Set, Tuple
import requests

logger = logging.getLogger("dealbot.monetizer")

# Known URL shorteners that need unshortening
SHORTENER_DOMAINS: Set[str] = {
    "amzn.to",
    "fkrt.it",
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "cutt.ly",
    "is.gd",
    "rb.gy",
    "buff.ly",
    "app.link",
}

# Parameters stripped from incoming URLs to eliminate competitor tracking
TRACKING_PARAM_NAMES: Set[str] = {
    "tag",
    "affid",
    "ascsubtag",
    "ref",
    "ref_",
    "linkcode",
    "camp",
    "creative",
    "creativeasin",
    "fbclid",
    "gclid",
    "msclkid",
    "_encoding",
}

TRACKING_PARAM_PREFIXES: Tuple[str, ...] = (
    "utm_",
    "pf_rd_",
    "pd_rd_",
)

# Regex to extract 10-character Amazon ASIN
AMAZON_ASIN_REGEX = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN|d)/([A-Z0-9]{10})(?:[/?#]|$)",
    re.IGNORECASE,
)


class MonetizationResult(NamedTuple):
    monetized_url: str
    store: str
    is_monetized: bool
    error_reason: Optional[str] = None


def slugify(text: Optional[str]) -> str:
    """Safe alphanumeric slug for ascsubtag values."""
    if not text:
        return "na"
    s = re.sub(r"[^\w\s-]", "", str(text).lower()).strip()
    return re.sub(r"[-\s]+", "_", s) or "na"


def unshorten_url(
    url: str,
    max_hops: int = 5,
    timeout: float = 3.0,
    session: Optional[requests.Session] = None,
) -> str:
    """Follow HTTP redirect hops safely with timeout and loop protection."""
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower().split(":")[0]

    # Fast path: not a shortener domain
    if netloc not in SHORTENER_DOMAINS:
        return url

    sess = session or requests.Session()
    current_url = url
    visited: Set[str] = {url}

    for _ in range(max_hops):
        # Stop immediately once expanded to a non-shortener destination
        curr_netloc = urllib.parse.urlparse(current_url).netloc.lower().split(":")[0]
        if curr_netloc not in SHORTENER_DOMAINS:
            break

        try:
            # Try HEAD first for speed
            resp = sess.head(
                current_url,
                allow_redirects=False,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            # If server forbids HEAD (405), fallback to streaming GET
            if resp.status_code == 405:
                resp = sess.get(
                    current_url,
                    allow_redirects=False,
                    stream=True,
                    timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
            
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                next_url = urllib.parse.urljoin(current_url, resp.headers["location"])
                if next_url in visited:
                    logger.debug("Redirect loop detected for %s", current_url)
                    break
                visited.add(next_url)
                current_url = next_url
            else:
                break
        except requests.RequestException as e:
            logger.debug("Redirect expansion stopped: %s", e)
            break

    return current_url


def strip_tracking_params(url: str) -> str:
    """Strip competitor affiliate tags, tracking IDs, and session noise."""
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    if not parsed.query:
        return url

    kept_params = []
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        k_lower = k.lower()
        if k_lower in TRACKING_PARAM_NAMES:
            continue
        if any(k_lower.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES):
            continue
        kept_params.append((k, v))

    clean_query = urllib.parse.urlencode(kept_params)
    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        clean_query,
        parsed.fragment,
    ))


def extract_asin(url: str) -> Optional[str]:
    """Extract 10-character Amazon ASIN from URL path or query params."""
    if not url:
        return None

    # Check path pattern
    m = AMAZON_ASIN_REGEX.search(url)
    if m:
        return m.group(1).upper()

    # Fallback to query param 'asin'
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for q_asin in qs.get("asin", []):
        if len(q_asin.strip()) == 10 and q_asin.strip().isalnum():
            return q_asin.strip().upper()

    return None


def detect_store(url: str) -> str:
    """Identify e-commerce store from URL domain."""
    if not url:
        return "unknown"

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().split(":")[0]

    if any(h in host for h in ("amazon.", "amzn.")):
        return "amazon"
    if any(h in host for h in ("flipkart.", "fkrt.")):
        return "flipkart"
    if "myntra." in host:
        return "myntra"
    if "ajio." in host:
        return "ajio"
    if "tatacliq." in host:
        return "tatacliq"
    if "nykaa." in host:
        return "nykaa"

    return host or "unknown"


def monetize_url(
    url: str,
    source: str = "telegram",
    verdict: str = "verified",
    allow_unshorten: bool = True,
    session: Optional[requests.Session] = None,
) -> MonetizationResult:
    """Canonicalize and monetize product URL.

    Returns:
        MonetizationResult(monetized_url, store, is_monetized, error_reason)
    """
    if not url or not str(url).strip():
        return MonetizationResult(
            monetized_url="",
            store="unknown",
            is_monetized=False,
            error_reason="Empty URL",
        )

    # 1. Unshorten redirect URLs (e.g. amzn.to -> amazon.in/dp/...)
    expanded_url = unshorten_url(url.strip(), session=session) if allow_unshorten else url.strip()

    # 2. Strip tracking parameters
    clean_url = strip_tracking_params(expanded_url)

    # 3. Detect store
    store = detect_store(clean_url)
    subtag = f"{slugify(source)}_{slugify(verdict)}"

    # 4. Amazon India Canonicalization
    if store == "amazon":
        asin = extract_asin(clean_url)
        if asin:
            amazon_tag = (
                os.getenv("AMAZON_TAG")
                or os.getenv("AMAZON_AFFILIATE_TAG")
                or os.getenv("AMAZON_PARTNER_TAG")
                or "dammaiprasad-21"
            ).strip()
            monetized = f"https://www.amazon.in/dp/{asin}?tag={amazon_tag}&ascsubtag={subtag}"
            return MonetizationResult(
                monetized_url=monetized,
                store="amazon",
                is_monetized=True,
                error_reason=None,
            )
        return MonetizationResult(
            monetized_url=clean_url,
            store="amazon",
            is_monetized=False,
            error_reason="No valid 10-character ASIN found in Amazon URL",
        )

    # 5. Non-Amazon Indian Stores (Flipkart, Myntra, Ajio, TataCliq, Nykaa)
    if store in ("flipkart", "myntra", "ajio", "tatacliq", "nykaa"):
        # Check Cuelinks network publisher ID
        cuelinks_pub_id = os.getenv("CUELINKS_PUB_ID", "").strip()
        if cuelinks_pub_id:
            encoded_target = urllib.parse.quote(clean_url, safe="")
            cuelinks_url = (
                f"https://linksredirect.com/?cid={cuelinks_pub_id}&subid={subtag}&url={encoded_target}"
            )
            return MonetizationResult(
                monetized_url=cuelinks_url,
                store=store,
                is_monetized=True,
                error_reason=None,
            )

        # Check EarnKaro or generic redirect template
        affiliate_template = os.getenv("AFFILIATE_REDIRECT_TEMPLATE", "").strip()
        if affiliate_template and "{url}" in affiliate_template:
            encoded_target = urllib.parse.quote(clean_url, safe="")
            monetized = affiliate_template.format(url=encoded_target, subid=subtag)
            return MonetizationResult(
                monetized_url=monetized,
                store=store,
                is_monetized=True,
                error_reason=None,
            )

        # Store-specific fallback (e.g. Flipkart Affiliate ID)
        if store == "flipkart":
            flipkart_affid = os.getenv("FLIPKART_AFFID", "").strip()
            if flipkart_affid:
                sep = "&" if "?" in clean_url else "?"
                monetized = f"{clean_url}{sep}affid={flipkart_affid}"
                return MonetizationResult(
                    monetized_url=monetized,
                    store=store,
                    is_monetized=True,
                    error_reason=None,
                )

        return MonetizationResult(
            monetized_url=clean_url,
            store=store,
            is_monetized=False,
            error_reason=f"No affiliate network or publisher ID configured for store: {store}",
        )

    # 6. Unknown Store
    cuelinks_pub_id = os.getenv("CUELINKS_PUB_ID", "").strip()
    if cuelinks_pub_id:
        encoded_target = urllib.parse.quote(clean_url, safe="")
        cuelinks_url = (
            f"https://linksredirect.com/?cid={cuelinks_pub_id}&subid={subtag}&url={encoded_target}"
        )
        return MonetizationResult(
            monetized_url=cuelinks_url,
            store=store,
            is_monetized=True,
            error_reason=None,
        )

    return MonetizationResult(
        monetized_url=clean_url,
        store=store,
        is_monetized=False,
        error_reason=f"Unsupported or unmonetized store: {store}",
    )


def can_publish_monetization(
    result: MonetizationResult,
    allow_unmonetized: bool = False,
) -> Tuple[bool, str]:
    """Publishing gate: blocks auto-posting if is_monetized is False."""
    if result.is_monetized:
        return True, f"Monetized link for {result.store}"
    if allow_unmonetized:
        return True, "Allowed unmonetized link via explicit override"
    return False, f"Blocked unmonetized link: {result.error_reason or 'No monetization'}"
