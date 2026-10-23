import logging
import os
import re
import requests
from typing import Optional

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

OOS_INDICATORS = [
    re.compile(r"currently unavailable", re.IGNORECASE),
    re.compile(r"out of stock", re.IGNORECASE),
    re.compile(r"we don't know when or if this item will be back in stock", re.IGNORECASE),
    re.compile(r"this item cannot be shipped", re.IGNORECASE),
]

def check_deal_is_active(url: str, timeout: int = 8) -> bool:
    """
    Check if an e-commerce product URL is still active and in stock.
    Returns False if 404/410, or if page contains out-of-stock indicators.
    """
    if not url:
        return False
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, allow_redirects=True)
        if resp.status_code in [404, 410]:
            return False
        if resp.status_code != 200:
            return True  # Avoid false positive on temporary server glitches

        text = resp.text[:40000]  # Check first 40KB where buy box and stock status reside
        for pattern in OOS_INDICATORS:
            if pattern.search(text):
                return False
        return True
    except requests.RequestException as e:
        logger.warning(f"Availability check failed for {url}: {e}")
        return True  # If network blip, assume active to avoid false expiry

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
        r = requests.post(endpoint, json=payload, timeout=10)
        if r.status_code == 200:
            logger.info(f"Successfully updated Telegram message {message_id} as EXPIRED.")
            return True
        else:
            logger.warning(f"Telegram edit message failed ({r.status_code}): {r.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Error calling Telegram edit message: {e}")
        return False
