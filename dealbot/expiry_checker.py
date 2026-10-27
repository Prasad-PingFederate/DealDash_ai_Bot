import logging
import os
import random
import re
from typing import Optional
import requests

logger = logging.getLogger(__name__)

CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

USER_AGENT = CHROME_USER_AGENTS[0]

DEFAULT_STOCK_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

CAPTCHA_PATTERNS = [
    re.compile(r"validateCaptcha", re.IGNORECASE),
    re.compile(r"api-services-support@amazon\.com", re.IGNORECASE),
    re.compile(r"Enter the characters you see below", re.IGNORECASE),
    re.compile(r"Type the characters you see in this image", re.IGNORECASE),
    re.compile(r"automated access", re.IGNORECASE),
    re.compile(r"robot check", re.IGNORECASE),
    re.compile(r"not a robot", re.IGNORECASE),
]

AMAZON_OOS_INDICATORS = [
    re.compile(r"currently unavailable", re.IGNORECASE),
    re.compile(r"temporarily out of stock", re.IGNORECASE),
    re.compile(r"we don't know when or if this item will be back in stock", re.IGNORECASE),
    re.compile(r"this item cannot be shipped", re.IGNORECASE),
    re.compile(r"out of stock", re.IGNORECASE),
]

FLIPKART_OOS_INDICATORS = [
    re.compile(r"\bsold out\b", re.IGNORECASE),
    re.compile(r"currently out of stock", re.IGNORECASE),
    re.compile(r"this product is currently out of stock", re.IGNORECASE),
    re.compile(r"item is out of stock", re.IGNORECASE),
]

OOS_INDICATORS = AMAZON_OOS_INDICATORS


def inspect_stock(url: str, timeout: int = 8) -> str:
    """
    Return in_stock, out_of_stock, or unknown.
    Positive DOM signal is required for out_of_stock.
    CAPTCHA and soft-blocks strictly return unknown to prevent premature edits.
    """
    if not url:
        return "out_of_stock"
    try:
        headers = dict(DEFAULT_STOCK_HEADERS)
        headers["User-Agent"] = random.choice(CHROME_USER_AGENTS)
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code in [404, 410]:
            return "out_of_stock"
        # Soft blocks, rate limits, or server errors must never trigger false OOS
        if resp.status_code in [403, 429, 503] or resp.status_code != 200:
            return "unknown"

        text = resp.text[:80000]
        # Explicit bot/CAPTCHA detection
        if any(p.search(text) for p in CAPTCHA_PATTERNS):
            return "unknown"

        is_amazon = "amazon." in url.lower()
        is_flipkart = "flipkart." in url.lower()

        oos_matched = False
        if is_flipkart:
            oos_matched = any(p.search(text) for p in FLIPKART_OOS_INDICATORS)
        elif is_amazon:
            oos_matched = any(p.search(text) for p in AMAZON_OOS_INDICATORS)
        else:
            oos_matched = any(p.search(text) for p in OOS_INDICATORS)

        buyable = bool(re.search(r"add to cart|buy now|(?<!back )in stock", text, re.IGNORECASE))
        if oos_matched and not buyable:
            return "out_of_stock"
        if buyable:
            return "in_stock"
        return "unknown"
    except requests.RequestException as e:
        logger.warning(f"Availability check failed for {url}: {e}")
        return "unknown"


def check_deal_is_active(url: str, timeout: int = 8) -> bool:
    """False only when the page is clearly gone or out of stock. A block page stays active."""
    return inspect_stock(url, timeout=timeout) != "out_of_stock"


def mark_telegram_deal_expired(token: str, chat_id: str, message_id: int, original_caption: str, is_photo: bool = True) -> bool:
    """
    Update a previously posted Telegram message to prepend [OFFER EXPIRED / OUT OF STOCK].
    """
    if not token or not chat_id or not message_id:
        return False

    expired_caption = f"❌ <b>[OFFER EXPIRED / OUT OF STOCK]</b>\n\n{original_caption}"
    # Truncate if exceeds Telegram 1024 char caption limit
    if len(expired_caption) > 1024:
        expired_caption = expired_caption[:1020] + "..."

    endpoint = (
        f"https://api.telegram.org/bot{token}/editMessageCaption"
        if is_photo
        else f"https://api.telegram.org/bot{token}/editMessageText"
    )

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "parse_mode": "HTML",
    }
    if is_photo:
        payload["caption"] = expired_caption
    else:
        payload["text"] = expired_caption

    try:
        from dealbot.publisher import safe_telegram_post
        r = safe_telegram_post(endpoint, payload=payload, timeout=10)
        if r.status_code == 200:
            logger.info(f"Successfully updated Telegram message {message_id} as EXPIRED.")
            return True
        else:
            logger.warning(f"Telegram edit message failed ({r.status_code}): {r.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Error calling Telegram edit message: {e}")
        return False
