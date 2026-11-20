# dealbot/scrapers/aggregators.py
"""Aggregator & Affiliate Network deal ingestion (Cuelinks, EarnKaro, Admitad).
Fetches curated top deals from affiliate network APIs when credentials are configured.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
import requests

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url
from dealbot.scrapers.base import BaseDealScraper


class AggregatorDealScraper(BaseDealScraper):
    """Scrapes curated top deals from affiliate aggregator APIs (Cuelinks, EarnKaro)."""

    def __init__(self, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = os.getenv("AGGREGATOR_SCRAPER_ENABLED", "true").lower() in ("true", "1", "yes")
        super().__init__(name="aggregators", enabled=enabled)
        self.cuelinks_token = os.getenv("CUELINKS_API_TOKEN") or os.getenv("CUELINKS_PUB_ID") or ""
        self.earnkaro_token = os.getenv("EARNKARO_API_TOKEN") or os.getenv("EARNKARO_ID") or ""

    def scrape(self, min_discount: int = 20) -> List[Deal]:
        deals: List[Deal] = []

        # 1. Cuelinks Deals API
        if self.cuelinks_token and self.cuelinks_token not in ("replace_me", "none", "false"):
            deals.extend(self._fetch_cuelinks_deals(min_discount=min_discount))
        else:
            self.logger.debug("Cuelinks credentials not configured; skipping Cuelinks API deals.")

        # 2. EarnKaro Deals Feed / API
        if self.earnkaro_token and self.earnkaro_token not in ("replace_me", "none", "false"):
            deals.extend(self._fetch_earnkaro_deals(min_discount=min_discount))
        else:
            self.logger.debug("EarnKaro credentials not configured; skipping EarnKaro API deals.")

        return deals

    def _fetch_cuelinks_deals(self, min_discount: int) -> List[Deal]:
        """Fetch curated top deals from Cuelinks API."""
        deals: List[Deal] = []
        api_url = os.getenv("CUELINKS_DEALS_API_URL", "https://www.cuelinks.com/api/v2/deals.json")
        headers = {
            "Authorization": f"Token token={self.cuelinks_token}",
            "Accept": "application/json",
        }
        params = {"status": "active", "per_page": 25}

        try:
            resp = requests.get(api_url, headers=headers, params=params, timeout=12)
            if resp.status_code != 200:
                self.logger.warning(f"Cuelinks API returned status {resp.status_code}: {resp.text[:120]}")
                return deals

            data = resp.json()
            items = data.get("deals") or data.get("data") or (data if isinstance(data, list) else [])
            for it in items:
                deal = self._parse_cuelinks_item(it, min_discount=min_discount)
                if deal:
                    deals.append(deal)
        except Exception as exc:
            self.logger.error(f"Error querying Cuelinks API: {exc}")

        return deals

    def _parse_cuelinks_item(self, it: Dict[str, Any], min_discount: int) -> Optional[Deal]:
        title = (it.get("title") or it.get("name") or "").strip()
        raw_url = it.get("url") or it.get("affiliate_url") or it.get("link") or ""
        if not title or not raw_url:
            return None

        merchant = (it.get("merchant_name") or it.get("campaign_name") or it.get("store") or "Store").strip()
        category = (it.get("category_name") or it.get("category") or "general").strip().lower()

        orig_price = parse_rupees(it.get("original_price") or it.get("mrp"))
        curr_price = parse_rupees(it.get("special_price") or it.get("deal_price") or it.get("price"))
        disc_pct = it.get("discount_percent") or it.get("discount_pct")

        if orig_price and curr_price and orig_price > curr_price:
            calc_disc = int(round((orig_price - curr_price) / orig_price * 100))
            disc_pct = calc_disc
        elif disc_pct:
            try:
                disc_pct = int(disc_pct)
            except ValueError:
                disc_pct = min_discount
        else:
            disc_pct = min_discount

        if disc_pct < min_discount:
            return None

        clean_title = re.sub(r"\s+", " ", title).strip()
        deal_id = f"cl-{it.get('id') or abs(hash(raw_url)) % 10000000}"

        monetized_res = monetize_url(
            raw_url,
            channel="tg",
            category=category,
            deal_id=deal_id,
            allow_unshorten=True,
        )

        deal = Deal(
            id=deal_id,
            retailer=merchant,
            title=clean_title,
            url=monetized_res.monetized_url,
            current_price=f"₹{curr_price:,}" if curr_price else None,
            original_price=f"₹{orig_price:,}" if orig_price else None,
            discount_percent=disc_pct,
            category=category,
            image_url=it.get("image_url") or it.get("image"),
            source_platform="cuelinks",
            matched_sources=["cuelinks"],
            source_count=1,
            checked=False,
        )
        return deal if deal.is_valid() else None

    def _fetch_earnkaro_deals(self, min_discount: int) -> List[Deal]:
        """Fetch curated top deals from EarnKaro API/feed."""
        deals: List[Deal] = []
        api_url = os.getenv("EARNKARO_DEALS_API_URL", "https://earnkaro.com/api/deals")
        headers = {
            "Authorization": f"Bearer {self.earnkaro_token}",
            "Accept": "application/json",
        }

        try:
            resp = requests.get(api_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                self.logger.debug(f"EarnKaro API returned status {resp.status_code}")
                return deals

            data = resp.json()
            items = data.get("deals") or data.get("data") or (data if isinstance(data, list) else [])
            for it in items:
                title = (it.get("title") or it.get("deal_title") or "").strip()
                raw_url = it.get("link") or it.get("product_url") or ""
                if not title or not raw_url:
                    continue

                merchant = (it.get("retailer") or it.get("store") or "Store").strip()
                category = (it.get("category") or "general").strip().lower()
                deal_id = f"ek-{it.get('id') or abs(hash(raw_url)) % 10000000}"

                orig_p = parse_rupees(it.get("mrp"))
                curr_p = parse_rupees(it.get("price"))
                disc_pct = int(it.get("discount") or min_discount)
                if orig_p and curr_p and orig_p > curr_p:
                    disc_pct = int(round((orig_p - curr_p) / orig_p * 100))

                monetized_res = monetize_url(
                    raw_url,
                    channel="tg",
                    category=category,
                    deal_id=deal_id,
                    allow_unshorten=True,
                )

                deal = Deal(
                    id=deal_id,
                    retailer=merchant,
                    title=title,
                    url=monetized_res.monetized_url,
                    current_price=f"₹{curr_p:,}" if curr_p else None,
                    original_price=f"₹{orig_p:,}" if orig_p else None,
                    discount_percent=disc_pct,
                    category=category,
                    source_platform="earnkaro",
                    matched_sources=["earnkaro"],
                    source_count=1,
                    checked=False,
                )
                if deal.is_valid():
                    deals.append(deal)
        except Exception as exc:
            self.logger.debug(f"EarnKaro API query exception: {exc}")

        return deals
