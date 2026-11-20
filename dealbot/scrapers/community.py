# dealbot/scrapers/community.py
"""Competitor & Community deal channel scraper (Telegram public web previews, Reddit, and RSS).
Extracts candidate deals from community channels, unshortens links, strips competitor affiliate tags,
re-monetizes with our SubIDs, and ranks via our Expected Profit Ranker.
"""
from __future__ import annotations

import html
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set
import xml.etree.ElementTree as ET

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url, unshorten_url
from dealbot.profit_ranker import compute_expected_profit
from dealbot.scrapers.base import BaseDealScraper

PRICE_REGEX = re.compile(r"[₹Rs\.]*\s*([0-9,]+(?:\.[0-9]+)?)")
URL_REGEX = re.compile(r'https?://[^\s<>"\'\)]+')


class CommunityChannelScraper(BaseDealScraper):
    """Scrapes public deal channels (Telegram public previews, Reddit, RSS)."""

    def __init__(self, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = os.getenv("COMMUNITY_SCRAPER_ENABLED", "true").lower() in ("true", "1", "yes")
        super().__init__(name="community", enabled=enabled)

        # Configurable public Telegram deal channels (channel handles without @)
        raw_channels = os.getenv("COMMUNITY_DEAL_CHANNELS", "Flash_Deals_Flipkart_Amazon,lootdealsindia")
        self.telegram_channels = [c.strip().lstrip("@") for c in raw_channels.split(",") if c.strip()]

    def scrape(self, min_discount: int = 20) -> List[Deal]:
        deals: List[Deal] = []
        seen_urls: Set[str] = set()

        # 1. Scrape public Telegram web channel previews (https://t.me/s/<channel>)
        for channel in self.telegram_channels:
            deals.extend(self._scrape_telegram_public_channel(channel, min_discount=min_discount, seen_urls=seen_urls))

        # 2. Scrape Reddit community deal feeds
        deals.extend(self._scrape_reddit_channels(min_discount=min_discount, seen_urls=seen_urls))

        return deals

    def _scrape_telegram_public_channel(self, channel: str, min_discount: int, seen_urls: Set[str]) -> List[Deal]:
        """Scrape public Telegram channel via https://t.me/s/<channel> without requiring Telethon login."""
        deals: List[Deal] = []
        preview_url = f"https://t.me/s/{channel}"
        resp = self.fetch_url(preview_url, timeout=4)
        if not resp or resp.status_code != 200:
            return deals

        html_text = resp.text
        # Telegram public channel preview wraps messages in:
        # <div class="tgme_widget_message_text js-message_text" ...>MESSAGE</div>
        message_blocks = re.findall(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )

        for raw_block in message_blocks[-6:]:  # Analyze latest 6 messages for fast cycle latency
            clean_text = html.unescape(re.sub(r"<[^>]+>", " ", raw_block)).strip()
            found_urls = URL_REGEX.findall(clean_text)
            if not found_urls:
                continue

            for raw_url in found_urls[:2]:  # Max 2 URLs per message
                raw_url_lower = raw_url.lower()
                # Focus on shopping and shortener domains
                if not any(d in raw_url_lower for d in (
                    "amazon", "amzn.to", "amzn.in", "flipkart", "fkrt.it", "myntra", "ajio", "croma", "tatacliq", "bit.ly"
                )):
                    continue

                # Unshorten with SSRF guard and strip competitor tags (fast timeout)
                unshortened = unshorten_url(raw_url, timeout=3)
                if unshortened in seen_urls:
                    continue
                seen_urls.add(unshortened)

                deal = self._process_community_deal(
                    target_url=unshortened,
                    message_text=clean_text,
                    channel_name=channel,
                    min_discount=min_discount,
                )
                if deal:
                    deals.append(deal)

        return deals

    def _scrape_reddit_channels(self, min_discount: int, seen_urls: Set[str]) -> List[Deal]:
        """Scrape deals from Reddit communities."""
        deals: List[Deal] = []
        reddit_feeds = [
            "https://www.reddit.com/r/dealsforindia/new.json?limit=20",
        ]
        headers = {"User-Agent": "Mozilla/5.0 DealDashBot/1.0"}

        for feed_url in reddit_feeds:
            resp = self.fetch_url(feed_url, timeout=4, extra_headers=headers)
            if not resp or resp.status_code != 200:
                continue

            try:
                data = resp.json()
                children = data.get("data", {}).get("children", [])
                for child in children:
                    pdata = child.get("data", {})
                    title = pdata.get("title", "")
                    link_url = pdata.get("url", "")
                    selftext = pdata.get("selftext", "")
                    full_text = f"{title} {selftext}"

                    candidate_urls = URL_REGEX.findall(f"{link_url} {selftext}")
                    for raw_u in candidate_urls[:2]:
                        if not any(d in raw_u.lower() for d in ("amazon", "amzn.to", "flipkart", "fkrt.it")):
                            continue

                        unshortened = unshorten_url(raw_u, timeout=3)
                        if unshortened in seen_urls:
                            continue
                        seen_urls.add(unshortened)

                        deal = self._process_community_deal(
                            target_url=unshortened,
                            message_text=full_text,
                            channel_name="reddit",
                            min_discount=min_discount,
                        )
                        if deal:
                            deals.append(deal)
            except Exception as e:
                self.logger.debug(f"Error parsing Reddit feed {feed_url}: {e}")

        return deals

    def _process_community_deal(
        self,
        target_url: str,
        message_text: str,
        channel_name: str,
        min_discount: int,
    ) -> Optional[Deal]:
        """Sanitize, strip competitor tags, re-monetize, and evaluate with Expected Profit Ranker."""
        # 1. Price extraction from message text
        prices = [int(p.replace(",", "")) for p in PRICE_REGEX.findall(message_text) if p.replace(",", "").isdigit()]
        curr_price = None
        orig_price = None
        disc_pct = min_discount

        if len(prices) >= 2:
            sorted_prices = sorted(prices)
            curr_price = sorted_prices[0]
            orig_price = sorted_prices[-1]
            if orig_price > curr_price:
                calc_disc = int(round((orig_price - curr_price) / orig_price * 100))
                disc_pct = max(calc_disc, min_discount)
        elif len(prices) == 1:
            curr_price = prices[0]

        # 2. Extract title line (first line of message)
        first_line = message_text.split("\n")[0].strip()
        first_line_clean = re.sub(r'https?://[^\s]+', '', first_line).strip()
        title = first_line_clean[:80] if len(first_line_clean) > 8 else "Trending Community Loot Deal"

        # 3. Detect retailer & ASIN/PID
        retailer = "Amazon" if "amazon" in target_url.lower() else ("Flipkart" if "flipkart" in target_url.lower() else "Store")
        asin_m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", target_url, re.IGNORECASE)
        asin = asin_m.group(1).upper() if asin_m else None

        pid_m = re.search(r"[?&]pid=([A-Z0-9]{16})", target_url, re.IGNORECASE)
        pid = pid_m.group(1).upper() if pid_m else None

        deal_id = f"amz-comm-{asin}" if asin else (f"fk-comm-{pid}" if pid else f"comm-{abs(hash(target_url)) % 10000000}")
        category = "electronics"
        if any(w in message_text.lower() for w in ("shirt", "shoes", "fashion", "dress", "saree")):
            category = "fashion"
        elif any(w in message_text.lower() for w in ("mobile", "phone", "iphone", "oneplus")):
            category = "mobiles"

        # 4. Expected Profit Ranker Gate: Reject 0%-commission items (gift cards, gold coins, etc.)
        profit_score = compute_expected_profit(
            deal_price=float(curr_price or 999),
            category=category,
            title=title,
            is_loot=disc_pct >= 35,
        )
        if profit_score <= 0.0:
            self.logger.debug(f"Excluding community item '{title}' (profit_score=0 / 0%-commission category).")
            return None

        # 5. Re-monetize with OUR SubIDs
        monetized_res = monetize_url(
            target_url,
            channel="tg",
            category=category,
            deal_id=deal_id,
            allow_unshorten=False,
        )

        deal = Deal(
            id=deal_id,
            retailer=retailer,
            title=title,
            url=monetized_res.monetized_url,
            current_price=f"₹{curr_price:,}" if curr_price else None,
            original_price=f"₹{orig_price:,}" if orig_price else None,
            discount_percent=disc_pct,
            canonical_product_id=f"amazon:{asin}" if asin else (f"flipkart:{pid}" if pid else None),
            category=category,
            source_platform=f"community_{channel_name}",
            matched_sources=[f"community_{channel_name}"],
            source_count=1,
            checked=False,
        )
        return deal if deal.is_valid() else None
