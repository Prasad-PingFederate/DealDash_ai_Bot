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

def inspect_stock(url: str, timeout: int = 8) -> str:
    """Return in_stock, out_of_stock, or unknown. Unknown means we could not tell."""
    if not url:
        return "out_of_stock"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, allow_redirects=True)
        if resp.status_code in [404, 410]:
            return "out_of_stock"
        if resp.status_code != 200:
            return "unknown"

        text = resp.text[:80000]
        blocked = re.search(
            r"not a robot|validateCaptcha|Enter the characters you see|api-services-support@amazon",
            text,
            re.IGNORECASE,
        )
        if blocked:
            return "unknown"
        out_of_stock = any(pattern.search(text) for pattern in OOS_INDICATORS)
        buyable = re.search(r"add to cart|buy now|(?<!back )in stock", text, re.IGNORECASE)
        if out_of_stock and not buyable:
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
