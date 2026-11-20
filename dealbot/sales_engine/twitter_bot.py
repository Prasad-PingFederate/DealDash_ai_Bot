# dealbot/sales_engine/twitter_bot.py
"""Automated Twitter / X Deals Bot for Viral Loot Deals.

Posts 280-character high-conversion deal updates with hashtag optimization
and direct links back to BharatBazaro to capture free organic search traffic.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url

logger = logging.getLogger(__name__)


def format_x_tweet(deal: Deal, max_chars: int = 280) -> str:
    """Format high-impact tweet staying strictly within the 280 character limit."""
    retailer = deal.retailer or "Amazon"
    price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
    mrp = parse_rupees(deal.original_price)
    disc = deal.discount_percent or (int(round((mrp - price) / mrp * 100)) if mrp and price and mrp > price else None)

    clean_id = deal.canonical_product_id or deal.id
    if clean_id.startswith("amz-"):
        clean_id = clean_id.replace("amz-", "")

    # BharatBazaro SEO tracking landing page
    bb_url = f"https://bharatbazaro.com/deal/{clean_id}"

    # Direct short tracking URL
    raw_url = deal.url or ""
    res = monetize_url(raw_url, channel="x", category=getattr(deal, "category", "gen"), deal_id=deal.id)
    aff_url = res.monetized_url or raw_url

    hashtags = "#LootDeal #AmazonSale #IndiaDeals #PriceDrop"
    if retailer.lower() == "flipkart":
        hashtags = "#LootDeal #FlipkartSale #IndiaDeals #PriceDrop"

    # Price string
    price_info = f"₹{price:,}" if price else "Loot"
    if mrp and price and mrp > price:
        price_info += f" (MRP ₹{mrp:,} • {disc}% OFF)"
    elif disc:
        price_info += f" ({disc}% OFF)"

    header = "🚨 LOOT DEAL ALERT" if (disc and disc >= 50) else "⚡ HOT DEAL"

    # Calculate remaining budget for title
    # Fixed content length
    fixed_part = f"{header}\n\n\n💰 {price_info}\n🛒 Grab: {aff_url}\n📊 History: {bb_url}\n\n{hashtags}"
    budget_for_title = max_chars - len(fixed_part)

    title = (deal.title or "Steal Deal").strip()
    if len(title) > budget_for_title:
        title = title[: max(10, budget_for_title - 3)] + "..."

    tweet = f"{header}\n{title}\n\n💰 {price_info}\n🛒 Grab: {aff_url}\n📊 History: {bb_url}\n\n{hashtags}"

    if len(tweet) > max_chars:
        # Fallback ultra-compact format
        tweet = f"{header}: {title[:50]}...\n💰 {price_info}\n🛒 {aff_url}\n{hashtags}"

    return tweet[:max_chars]


def post_to_twitter(
    deal: Deal,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """Publish a deal tweet to X via API v2 (POST /2/tweets)."""
    tweet_text = format_x_tweet(deal)

    if dry_run:
        logger.info(f"[Twitter DRY RUN] Would post ({len(tweet_text)} chars):\n{tweet_text}")
        return True, "dry_run_success"

    # Check API v2 credentials
    api_key = os.getenv("TWITTER_API_KEY") or os.getenv("X_API_KEY")
    api_secret = os.getenv("TWITTER_API_SECRET") or os.getenv("X_API_SECRET")
    access_token = os.getenv("TWITTER_ACCESS_TOKEN") or os.getenv("X_ACCESS_TOKEN")
    access_token_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET") or os.getenv("X_ACCESS_TOKEN_SECRET")

    if not (api_key and api_secret and access_token and access_token_secret):
        logger.debug("[Twitter/X] API credentials not configured in environment. Skipping.")
        return False, "unconfigured"

    # Try requests_oauthlib
    try:
        from requests_oauthlib import OAuth1

        auth = OAuth1(api_key, api_secret, access_token, access_token_secret)
        endpoint = "https://api.twitter.com/2/tweets"
        resp = requests.post(endpoint, json={"text": tweet_text}, auth=auth, timeout=12)

        if resp.status_code in (200, 201):
            data = resp.json().get("data", {})
            tweet_id = data.get("id", "posted")
            logger.info(f"[Twitter/X] Successfully posted tweet {tweet_id}")
            return True, f"tweet_{tweet_id}"

        logger.warning(f"[Twitter/X] Post failed ({resp.status_code}): {resp.text}")
        return False, f"http_{resp.status_code}"
    except ImportError:
        logger.warning("[Twitter/X] requests_oauthlib not installed; cannot compute OAuth 1.0a signatures.")
        return False, "requests_oauthlib_missing"
    except Exception as e:
        logger.warning(f"[Twitter/X] Post exception: {e}")
        return False, str(e)
