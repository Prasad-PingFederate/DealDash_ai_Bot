# dealbot/scrapers/registry.py
"""Registry and parallel execution orchestrator for pluggable deal scrapers."""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Dict, List, Optional, Set

from dealbot.models import Deal
from dealbot.scrapers.base import BaseDealScraper
from dealbot.scrapers.amazon import AmazonDealScraper
from dealbot.scrapers.flipkart import FlipkartDealScraper
from dealbot.scrapers.aggregators import AggregatorDealScraper
from dealbot.scrapers.community import CommunityChannelScraper

logger = logging.getLogger("dealbot.scrapers.registry")


class DealScraperRegistry:
    """Manages pluggable scrapers, executes them concurrently, and deduplicates deals."""

    def __init__(self):
        self._scrapers: Dict[str, BaseDealScraper] = {}

    def register(self, scraper: BaseDealScraper) -> None:
        """Register a deal scraper instance."""
        self._scrapers[scraper.name] = scraper
        logger.debug(f"Registered deal scraper: {scraper.name} (enabled={scraper.enabled})")

    def unregister(self, name: str) -> None:
        """Unregister a scraper by name."""
        self._scrapers.pop(name, None)

    def get_scraper(self, name: str) -> Optional[BaseDealScraper]:
        return self._scrapers.get(name)

    def list_scrapers(self) -> List[BaseDealScraper]:
        return list(self._scrapers.values())

    def collect_all(self, min_discount: int = 20, max_workers: int = 4) -> List[Deal]:
        """Execute all enabled scrapers in parallel with error isolation and deduplication."""
        enabled_scrapers = [s for s in self._scrapers.values() if s.enabled]
        if not enabled_scrapers:
            logger.warning("No deal scrapers enabled in registry.")
            return []

        all_collected: List[Deal] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_scraper = {
                executor.submit(s.safe_scrape, min_discount=min_discount): s
                for s in enabled_scrapers
            }
            for future in concurrent.futures.as_completed(future_to_scraper):
                scraper = future_to_scraper[future]
                try:
                    scraped = future.result()
                    all_collected.extend(scraped)
                except Exception as exc:
                    logger.error(f"Scraper '{scraper.name}' generated unexpected exception: {exc}")

        # Intra-registry deduplication & multi-source viral consensus merging
        deduped: List[Deal] = []
        by_canon: Dict[str, Deal] = {}

        for d in all_collected:
            canon_key = getattr(d, "canonical_product_id", None) or d.id
            if not canon_key:
                continue

            if canon_key not in by_canon:
                by_canon[canon_key] = d
                deduped.append(d)
            else:
                existing = by_canon[canon_key]
                # Merge matched sources
                sources = set(existing.matched_sources or [])
                sources.update(d.matched_sources or [])
                if getattr(d, "source_platform", None):
                    sources.add(d.source_platform)
                existing.matched_sources = sorted(list(sources))
                existing.source_count = max(len(existing.matched_sources), 1)

                # Keep richer deal data (images, original price, etc.)
                if not existing.image_url and d.image_url:
                    existing.image_url = d.image_url
                if not existing.original_price and d.original_price:
                    existing.original_price = d.original_price
                if not existing.coupon_code and d.coupon_code:
                    existing.coupon_code = d.coupon_code

        logger.info(
            f"[DealScraperRegistry] Ingested {len(all_collected)} deals from {len(enabled_scrapers)} scrapers; "
            f"{len(deduped)} remaining after deduplication."
        )
        return deduped


_DEFAULT_REGISTRY: Optional[DealScraperRegistry] = None


def get_default_registry() -> DealScraperRegistry:
    """Retrieve or initialize singleton registry with standard pluggable scrapers."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        reg = DealScraperRegistry()
        reg.register(AmazonDealScraper())
        reg.register(FlipkartDealScraper())
        reg.register(AggregatorDealScraper())
        reg.register(CommunityChannelScraper())
        _DEFAULT_REGISTRY = reg
    return _DEFAULT_REGISTRY
