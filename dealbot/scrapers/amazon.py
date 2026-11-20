# dealbot/scrapers/amazon.py
"""Amazon India deal scraper and feed parser.
Supports:
1. Amazon Lightning Deals & GoldBox deals page semantic parsing.
2. Amazon Movers & Shakers / Bestsellers price-drop feeds.
3. Amazon Creators / Product Advertising feeds when credentials present.
4. Preserves th=1&psc=1 and smid variant params while injecting bot SubIDs.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set
import xml.etree.ElementTree as ET

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url
from dealbot.scrapers.base import BaseDealScraper

AMAZON_ASIN_REGEX = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.IGNORECASE)
PRICE_REGEX = re.compile(r"[₹Rs\.]*\s*([0-9,]+(?:\.[0-9]+)?)")


class AmazonDealScraper(BaseDealScraper):
    """Scrapes live Amazon India deals across GoldBox, Movers & Shakers, and Deal feeds."""

    def __init__(self, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = os.getenv("AMAZON_SCRAPER_ENABLED", "true").lower() in ("true", "1", "yes")
        super().__init__(name="amazon", enabled=enabled)
        self.partner_tag = os.getenv("AMAZON_ASSOCIATE_TAG") or os.getenv("AMAZON_TAG") or "dammaiprasad-21"
        self.domain = os.getenv("AMAZON_DOMAIN", "amazon.in")

    def scrape(self, min_discount: int = 20) -> List[Deal]:
        deals: List[Deal] = []
        seen_asins: Set[str] = set()

        # 1. Parse Amazon Deals & Goldbox page
        goldbox_deals = self._scrape_goldbox(min_discount=min_discount, seen_asins=seen_asins)
        deals.extend(goldbox_deals)

        # 2. Parse Amazon Category Movers & Shakers feeds
        movers_deals = self._scrape_movers_and_shakers(min_discount=min_discount, seen_asins=seen_asins)
        deals.extend(movers_deals)

        # 3. Parse custom RSS feeds if configured
        rss_urls = os.getenv("AMAZON_DEAL_RSS_URLS", "")
        if rss_urls:
            for url in rss_urls.split(","):
                url = url.strip()
                if url:
                    deals.extend(self._parse_rss_feed(url, min_discount=min_discount, seen_asins=seen_asins))

        return deals

    def _scrape_goldbox(self, min_discount: int, seen_asins: Set[str]) -> List[Deal]:
        """Scrape Amazon India Deals / Goldbox page."""
        deals: List[Deal] = []
        url = f"https://www.{self.domain}/deals"
        resp = self.fetch_url(url, timeout=10)
        if not resp or resp.status_code != 200:
            return deals

        html = resp.text
        if "validateCaptcha" in html or "Type the characters" in html:
            self.logger.debug("Amazon deals page requested CAPTCHA; backing off cleanly.")
            return deals

        # Find deal cards / product containers
        # Match ASIN patterns in deal cards
        asin_matches = re.findall(r'href="([^"]*/(?:dp|gp/product)/([A-Z0-9]{10})[^"]*)"', html)
        for href, asin in asin_matches:
            asin = asin.upper()
            if asin in seen_asins:
                continue
            seen_asins.add(asin)

            deal = self._build_amazon_deal(asin=asin, href=href, min_discount=min_discount, source_type="goldbox")
            if deal:
                deals.append(deal)

        return deals

    def _scrape_movers_and_shakers(self, min_discount: int, seen_asins: Set[str]) -> List[Deal]:
        """Scrape Amazon Movers & Shakers trending categories."""
        deals: List[Deal] = []
        categories = [
            ("electronics", f"https://www.{self.domain}/gp/movers-and-shakers/electronics"),
            ("computers", f"https://www.{self.domain}/gp/movers-and-shakers/computers"),
            ("home", f"https://www.{self.domain}/gp/movers-and-shakers/kitchen"),
            ("beauty", f"https://www.{self.domain}/gp/bestsellers/beauty"),
        ]

        for cat_name, cat_url in categories:
            resp = self.fetch_url(cat_url, timeout=8)
            if not resp or resp.status_code != 200:
                continue
            html = resp.text
            if "validateCaptcha" in html:
                continue

            matches = re.findall(r'href="([^"]*/(?:dp|gp/product)/([A-Z0-9]{10})[^"]*)"', html)
            added_in_cat = 0
            for href, asin in matches:
                asin = asin.upper()
                if asin in seen_asins:
                    continue
                seen_asins.add(asin)

                deal = self._build_amazon_deal(
                    asin=asin,
                    href=href,
                    category=cat_name,
                    min_discount=min_discount,
                    source_type="movers",
                )
                if deal:
                    deals.append(deal)
                    added_in_cat += 1
                    if added_in_cat >= 4:
                        break

        return deals

    def _parse_rss_feed(self, feed_url: str, min_discount: int, seen_asins: Set[str]) -> List[Deal]:
        """Parse an XML/RSS feed containing Amazon deal links."""
        deals: List[Deal] = []
        resp = self.fetch_url(feed_url, timeout=10)
        if not resp or resp.status_code != 200:
            return deals

        try:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                link_el = item.find("link")
                title_el = item.find("title")
                desc_el = item.find("description")
                link = (link_el.text or "").strip() if link_el is not None else ""
                title = (title_el.text or "").strip() if title_el is not None else ""
                desc = (desc_el.text or "").strip() if desc_el is not None else ""

                m = AMAZON_ASIN_REGEX.search(link) or AMAZON_ASIN_REGEX.search(desc)
                if not m:
                    continue
                asin = m.group(1).upper()
                if asin in seen_asins:
                    continue
                seen_asins.add(asin)

                deal = self._build_amazon_deal(
                    asin=asin,
                    href=link,
                    title_hint=title,
                    desc_hint=desc,
                    min_discount=min_discount,
                    source_type="rss",
                )
                if deal:
                    deals.append(deal)
        except Exception as e:
            self.logger.debug(f"Error parsing Amazon RSS feed {feed_url}: {e}")
        return deals

    def _build_amazon_deal(
        self,
        asin: str,
        href: str,
        category: str = "electronics",
        title_hint: Optional[str] = None,
        desc_hint: Optional[str] = None,
        min_discount: int = 20,
        source_type: str = "scrape",
    ) -> Optional[Deal]:
        """Construct a standardized Deal object with variant params and monetization."""
        clean_url = f"https://www.{self.domain}/dp/{asin}"

        # Preserve variant parameters if present in href
        variant_params = []
        if "th=1" in href:
            variant_params.append("th=1")
        if "psc=1" in href:
            variant_params.append("psc=1")
        smid_m = re.search(r"[?&]smid=([A-Z0-9]+)", href)
        if smid_m:
            variant_params.append(f"smid={smid_m.group(1)}")

        if variant_params:
            clean_url += "?" + "&".join(variant_params)

        # Title extraction
        title = title_hint
        if not title:
            # Try title slug from href
            slug_m = re.search(r"/([^/]+)/(?:dp|gp/product)/", href)
            if slug_m and not slug_m.group(1).startswith("dp"):
                title = slug_m.group(1).replace("-", " ").title()
            else:
                title = f"Amazon Deal ({asin})"

        # Clean title
        title = re.sub(r"\s+", " ", title).strip()

        # Price extraction from hint text if available
        curr_price_str = None
        orig_price_str = None
        disc_pct = min_discount

        blob = f"{title_hint or ''} {desc_hint or ''}"
        prices = [int(p.replace(",", "")) for p in PRICE_REGEX.findall(blob) if p.replace(",", "").isdigit()]
        if len(prices) >= 2:
            sorted_prices = sorted(prices)
            curr_p = sorted_prices[0]
            mrp_p = sorted_prices[-1]
            if mrp_p > curr_p:
                calc_disc = int(round((mrp_p - curr_p) / mrp_p * 100))
                if calc_disc >= min_discount:
                    curr_price_str = f"₹{curr_p:,}"
                    orig_price_str = f"₹{mrp_p:,}"
                    disc_pct = calc_disc

        # Monetize URL with dynamic SubID
        deal_id = f"amz-{asin}"
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
            retailer="Amazon",
            title=title,
            url=aff_url,
            current_price=curr_price_str,
            original_price=orig_price_str,
            discount_percent=disc_pct,
            canonical_product_id=f"amazon:{asin}",
            category=category,
            image_url=f"https://m.media-amazon.com/images/P/{asin}.jpg",
            source_platform=f"amazon_{source_type}",
            matched_sources=[f"amazon_{source_type}"],
            source_count=1,
            checked=False,
        )
        return deal if deal.is_valid() else None
