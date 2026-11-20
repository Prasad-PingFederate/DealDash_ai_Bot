# dealbot/scrapers/flipkart.py
"""Flipkart deal scraper and feed parser.
Supports:
1. Flipkart Deals of the Day & Top Offers semantic parsing.
2. Preserves pid and lid variant parameters strictly.
3. Injects Flipkart affiliate tags or Cuelinks/EarnKaro waterfall fallback.
4. Robust error isolation with rotating headers.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url
from dealbot.scrapers.base import BaseDealScraper

FLIPKART_PID_REGEX = re.compile(r"[?&]pid=([A-Z0-9]{16})", re.IGNORECASE)
FLIPKART_LID_REGEX = re.compile(r"[?&]lid=([A-Z0-9]+)", re.IGNORECASE)
PRICE_REGEX = re.compile(r"[₹Rs\.]*\s*([0-9,]+(?:\.[0-9]+)?)")


class FlipkartDealScraper(BaseDealScraper):
    """Scrapes Flipkart Deals of the Day and Top Offers feeds."""

    def __init__(self, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = os.getenv("FLIPKART_SCRAPER_ENABLED", "true").lower() in ("true", "1", "yes")
        super().__init__(name="flipkart", enabled=enabled)
        self.affid = os.getenv("FLIPKART_AFFILIATE_ID") or os.getenv("FLIPKART_AFFID") or ""

    def scrape(self, min_discount: int = 20) -> List[Deal]:
        deals: List[Deal] = []
        seen_pids: Set[str] = set()

        # Flipkart Deals of the day & Top offers
        seed_urls = [
            ("deals_of_the_day", "https://www.flipkart.com/offers-list/deals-of-the-day"),
            ("top_offers", "https://www.flipkart.com/offers-list/top-offers"),
        ]

        for source_name, url in seed_urls:
            resp = self.fetch_url(url, timeout=10)
            if not resp or resp.status_code != 200:
                continue

            html = resp.text
            # Look for Flipkart product links with pid
            # e.g. href="/.../p/itm...?pid=MOB...&lid=..."
            links = re.findall(r'href="([^"]*/p/[^"]*pid=[A-Z0-9]{16}[^"]*)"', html, re.IGNORECASE)
            for href in links:
                full_url = urllib.parse.urljoin("https://www.flipkart.com", href)
                pid_m = FLIPKART_PID_REGEX.search(full_url)
                if not pid_m:
                    continue
                pid = pid_m.group(1).upper()
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)

                deal = self._build_flipkart_deal(pid=pid, href=full_url, source_name=source_name, min_discount=min_discount)
                if deal:
                    deals.append(deal)

        return deals

    def _build_flipkart_deal(
        self,
        pid: str,
        href: str,
        source_name: str = "flipkart",
        min_discount: int = 20,
        title_hint: Optional[str] = None,
        price_hint: Optional[int] = None,
        mrp_hint: Optional[int] = None,
    ) -> Optional[Deal]:
        """Construct a standardized Flipkart Deal preserving pid & lid with waterfall attribution."""
        # Clean canonical URL preserving pid and lid
        lid_m = FLIPKART_LID_REGEX.search(href)
        lid = lid_m.group(1) if lid_m else None

        base_clean = href.split("?")[0]
        params = [f"pid={pid}"]
        if lid:
            params.append(f"lid={lid}")
        clean_url = f"{base_clean}?{'&'.join(params)}"

        # Title extraction from URL slug
        title = title_hint
        if not title:
            slug_m = re.search(r"flipkart\.com/([^/]+)/p/", href)
            if slug_m:
                title = slug_m.group(1).replace("-", " ").title()
            else:
                title = f"Flipkart Deal ({pid})"
        title = re.sub(r"\s+", " ", title).strip()

        # Category inference from slug
        category = "electronics"
        title_lower = title.lower()
        if any(w in title_lower for w in ("shirt", "shoes", "jean", "dress", "saree", "watch")):
            category = "fashion"
        elif any(w in title_lower for w in ("mobile", "phone", "galaxy", "redmi", "realme")):
            category = "mobiles"
        elif any(w in title_lower for w in ("cleaner", "cooker", "pan", "mixer", "grinder")):
            category = "home"

        # Pricing
        curr_price_str = f"₹{price_hint:,}" if price_hint else None
        orig_price_str = f"₹{mrp_hint:,}" if mrp_hint else None
        disc_pct = min_discount
        if price_hint and mrp_hint and mrp_hint > price_hint:
            calc_disc = int(round((mrp_hint - price_hint) / mrp_hint * 100))
            if calc_disc >= min_discount:
                disc_pct = calc_disc

        deal_id = f"fk-{pid}"
        monetized_res = monetize_url(
            clean_url,
            channel="tg",
            category=category,
            deal_id=deal_id,
            allow_unshorten=False,
        )
        aff_url = monetized_res.monetized_url

        deal = Deal(
            id=deal_id,
            retailer="Flipkart",
            title=title,
            url=aff_url,
            current_price=curr_price_str,
            original_price=orig_price_str,
            discount_percent=disc_pct,
            canonical_product_id=f"flipkart:{pid}",
            category=category,
            source_platform=f"flipkart_{source_name}",
            matched_sources=[f"flipkart_{source_name}"],
            source_count=1,
            checked=False,
        )
        return deal if deal.is_valid() else None
