"""
Global Multi-Region Architecture configuration for DealDash AI.
Supports IN (India - Default), US (United States), UK (United Kingdom), and AE (United Arab Emirates).
"""

import os
from typing import Any, Dict, List, Optional, Tuple

REGIONS: Dict[str, Dict[str, Any]] = {
    "IN": {
        "name": "India",
        "domain": "amazon.in",
        "currency": "₹",
        "currency_code": "INR",
        "keepa_domain": 10,
        "tz": "Asia/Kolkata",
        "price_history_provider": "PriceHistory",
        "price_history_url": "https://pricehistoryapp.com/product/{asin}",
        "impulse_tiers": [
            (49, 99),
            (149, 199),
            (249, 299),
            (449, 499),
            (899, 999),
        ],
        "reddit_feeds": [
            "https://www.reddit.com/r/dealsforindia/new.json?limit=25",
            "https://www.reddit.com/r/IndianGaming/new.json?limit=25",
        ],
        "movers_url": "https://www.amazon.in/gp/movers-and-shakers/electronics",
    },
    "US": {
        "name": "United States",
        "domain": "amazon.com",
        "currency": "$",
        "currency_code": "USD",
        "keepa_domain": 1,
        "tz": "America/New_York",
        "price_history_provider": "CamelCamelCamel",
        "price_history_url": "https://camelcamelcamel.com/product/{asin}",
        "impulse_tiers": [
            (5, 10),
            (15, 20),
            (25, 30),
            (40, 50),
            (80, 100),
        ],
        "rss_feeds": [
            "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
        ],
        "reddit_feeds": [
            "https://www.reddit.com/r/deals/new.json?limit=25",
            "https://www.reddit.com/r/buildapcsales/new.json?limit=25",
        ],
        "movers_url": "https://www.amazon.com/gp/movers-and-shakers/electronics",
    },
    "UK": {
        "name": "United Kingdom",
        "domain": "amazon.co.uk",
        "currency": "£",
        "currency_code": "GBP",
        "keepa_domain": 2,
        "tz": "Europe/London",
        "price_history_provider": "CamelCamelCamel",
        "price_history_url": "https://uk.camelcamelcamel.com/product/{asin}",
        "impulse_tiers": [
            (5, 10),
            (15, 20),
            (25, 30),
            (40, 50),
            (80, 100),
        ],
        "reddit_feeds": [
            "https://www.reddit.com/r/HotUKDeals/new.json?limit=25",
            "https://www.reddit.com/r/DealsUK/new.json?limit=25",
        ],
        "movers_url": "https://www.amazon.co.uk/gp/movers-and-shakers/electronics",
    },
    "AE": {
        "name": "United Arab Emirates",
        "domain": "amazon.ae",
        "currency": "AED",
        "currency_code": "AED",
        "keepa_domain": 9,
        "tz": "Asia/Dubai",
        "price_history_provider": "PriceHistory",
        "price_history_url": "",
        "impulse_tiers": [
            (20, 50),
            (50, 100),
            (100, 200),
        ],
        "reddit_feeds": [],
        "movers_url": "https://www.amazon.ae/gp/movers-and-shakers/electronics",
    },
}


def get_market_region() -> str:
    region = os.getenv("MARKET_REGION", "IN").strip().upper()
    return region if region in REGIONS else "IN"


def get_region_config(region: Optional[str] = None) -> Dict[str, Any]:
    r = (region or get_market_region()).strip().upper()
    return REGIONS.get(r, REGIONS["IN"])


def get_currency_symbol(region: Optional[str] = None) -> str:
    return str(get_region_config(region).get("currency", "₹"))


def get_keepa_url(asin: str, region: Optional[str] = None) -> str:
    cfg = get_region_config(region)
    domain_id = cfg.get("keepa_domain", 10)
    return f"https://keepa.com/#!product/{domain_id}-{asin}"


def get_price_history_url(asin: str, region: Optional[str] = None) -> Optional[str]:
    cfg = get_region_config(region)
    template = cfg.get("price_history_url")
    if template:
        return template.format(asin=asin)
    return None


def get_impulse_tiers(region: Optional[str] = None) -> List[Tuple[int, int]]:
    cfg = get_region_config(region)
    return list(cfg.get("impulse_tiers", []))
