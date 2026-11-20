# dealbot/scrapers/__init__.py
"""Pluggable multi-channel deal scraping and ingestion package."""
from dealbot.scrapers.base import BaseDealScraper
from dealbot.scrapers.amazon import AmazonDealScraper
from dealbot.scrapers.flipkart import FlipkartDealScraper
from dealbot.scrapers.aggregators import AggregatorDealScraper
from dealbot.scrapers.community import CommunityChannelScraper
from dealbot.scrapers.registry import DealScraperRegistry, get_default_registry

__all__ = [
    "BaseDealScraper",
    "AmazonDealScraper",
    "FlipkartDealScraper",
    "AggregatorDealScraper",
    "CommunityChannelScraper",
    "DealScraperRegistry",
    "get_default_registry",
]
