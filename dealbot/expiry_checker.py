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


def enrich_amazon_deal(url: str, timeout: int = 8) -> dict:
    """
    Fetch an Amazon product page and extract live stock status and enriched metadata:
    - stock_status: 'in_stock', 'out_of_stock', or 'unknown'
    - live_price: int or None
    - mrp: int or None
    - rating: float or None
    - review_count: int or None
    - coupon: str or None
    - bank_offer: str or None
    - hi_res_image: str or None
    """
    result = {
        "stock_status": "unknown",
        "live_price": None,
        "mrp": None,
        "rating": None,
        "review_count": None,
        "coupon": None,
        "coupon_discount": None,
        "effective_price": None,
        "promo_code": None,
        "bank_offer": None,
        "bank_discount_amount": None,
        "hi_res_image": None,
    }
    if not url:
        result["stock_status"] = "out_of_stock"
        return result

    try:
        headers = dict(DEFAULT_STOCK_HEADERS)
        headers["User-Agent"] = random.choice(CHROME_USER_AGENTS)
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code in [404, 410]:
            result["stock_status"] = "out_of_stock"
            return result
        if resp.status_code in [403, 429, 503] or resp.status_code != 200:
            result["stock_status"] = "unknown"
            return result

        text = resp.text
        # Explicit bot/CAPTCHA detection
        if any(p.search(text[:80000]) for p in CAPTCHA_PATTERNS):
            result["stock_status"] = "unknown"
            return result

        # Check stock status
        oos_matched = any(p.search(text[:80000]) for p in AMAZON_OOS_INDICATORS)
        buyable = bool(re.search(r"add to cart|buy now|(?<!back )in stock", text[:80000], re.IGNORECASE))
        if oos_matched and not buyable:
            result["stock_status"] = "out_of_stock"
            return result
        elif buyable:
            result["stock_status"] = "in_stock"
        else:
            result["stock_status"] = "unknown"

        # 1. Live Price
        from dealbot.models import parse_rupees
        price_patterns = [
            r'class="a-price-whole"[^>]*>([\d,]+)',
            r'class="priceToPay"[^>]*>.*?class="a-offscreen"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
            r'class="apexPriceToPay"[^>]*>.*?class="a-offscreen"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
            r'id="corePriceDisplay_desktop_feature_div".*?class="a-price-whole"[^>]*>([\d,]+)',
            r'id="priceblock_dealprice"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
            r'id="priceblock_ourprice"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
        ]
        for pattern in price_patterns:
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                p = parse_rupees(m.group(1))
                if p and p > 0:
                    result["live_price"] = p
                    break

        # 2. MRP / Original Price
        mrp_patterns = [
            r'class="basisPrice"[^>]*>.*?class="a-offscreen"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
            r'class="a-price a-text-price"[^>]*>.*?class="a-offscreen"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
            r'id="corePriceDisplay_desktop_feature_div".*?class="a-text-price"[^>]*>.*?class="a-offscreen"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
            r'M\.R\.P\.:.*?class="a-offscreen"[^>]*>₹?\s*([\d,]+(?:\.\d+)?)',
        ]
        for pattern in mrp_patterns:
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                p = parse_rupees(m.group(1))
                if p and p > 0:
                    result["mrp"] = p
                    break

        # 3. Customer Rating
        rating_match = re.search(r'(?:id="acrPopover"[^>]*title="|class="a-icon-alt"[^>]*>)\s*([\d\.]+)\s*(?:out of 5|stars)', text, re.IGNORECASE)
        if rating_match:
            try:
                r_val = float(rating_match.group(1))
                if 1.0 <= r_val <= 5.0:
                    result["rating"] = round(r_val, 1)
            except ValueError:
                pass

        # 4. Review Count
        review_match = re.search(r'(?:id="acrCustomerReviewText"[^>]*>|id="averageCustomerReviews"[^>]*>[\s\S]*?>)\s*([\d,]+)\s*(?:ratings|reviews|customer reviews)', text, re.IGNORECASE)
        if review_match:
            try:
                result["review_count"] = int(review_match.group(1).replace(",", ""))
            except ValueError:
                pass

        if result["rating"] == 0 or result["rating"] == 0.0:
            result["rating"] = None
        if result["review_count"] == 0:
            result["review_count"] = None

        # 5. On-page Coupons & Effective Price
        coupon_patterns = [
            (r'Apply\s*(?:₹|Rs\.?)\s*(\d+)\s*coupon', "flat"),
            (r'Save\s*(?:₹|Rs\.?)\s*(\d+)\s*(?:with|at checkout|coupon)', "flat"),
            (r'Save\s*(\d+)\s*%\s*(?:with|at checkout|coupon)', "percent"),
            (r'Apply\s*(\d+)\s*%\s*coupon', "percent"),
            (r'id="couponBadge"[^>]*>([^<]+)', "badge"),
        ]
        for pattern, kind in coupon_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                raw_c = m.group(0).strip()
                clean_c = re.sub(r"\s+", " ", raw_c)
                if len(clean_c) <= 60:
                    result["coupon"] = clean_c
                    if kind == "flat":
                        try:
                            c_amt = int(m.group(1))
                            if c_amt > 0:
                                result["coupon_discount"] = c_amt
                        except (ValueError, IndexError):
                            pass
                    elif kind == "percent":
                        try:
                            pct = float(m.group(1))
                            if 0 < pct < 100 and result.get("live_price"):
                                pct_disc = int(round(result["live_price"] * pct / 100))
                                result["coupon_discount"] = min(pct_disc, 2000)
                        except (ValueError, IndexError):
                            pass
                    elif kind == "badge":
                        badge_txt = m.group(1)
                        flat_m = re.search(r'(?:₹|Rs\.?)\s*(\d+)', badge_txt)
                        pct_m = re.search(r'(\d+)\s*%', badge_txt)
                        if flat_m:
                            try:
                                result["coupon_discount"] = int(flat_m.group(1))
                            except ValueError:
                                pass
                        elif pct_m and result.get("live_price"):
                            try:
                                pct_disc = int(round(result["live_price"] * float(pct_m.group(1)) / 100))
                                result["coupon_discount"] = min(pct_disc, 2000)
                            except ValueError:
                                pass
                    break

        if result.get("live_price") and result.get("coupon_discount"):
            disc = result["coupon_discount"]
            lp = result["live_price"]
            if disc < lp:
                result["effective_price"] = max(1, lp - disc)

        # Compute live discount_pct and effective_discount_pct
        lp = result.get("live_price")
        mrp = result.get("mrp")
        eff_p = result.get("effective_price") or lp
        if mrp and lp and mrp > lp > 0:
            result["discount_pct"] = int(round(((mrp - lp) / mrp) * 100))
            result["effective_discount_pct"] = int(round(((mrp - eff_p) / mrp) * 100))
        elif mrp and lp and mrp <= lp:
            result["discount_pct"] = 0
            result["effective_discount_pct"] = 0

        # 6. Promo Code Extraction
        promo_patterns = [
            r'(?:use|enter|apply)\s+code\s+([A-Za-z0-9_-]{3,20})\b',
            r'\bpromo\s+code\s*[:=]?\s*([A-Za-z0-9_-]{3,20})\b',
            r'\bcoupon\s+code\s*[:=]?\s*([A-Za-z0-9_-]{3,20})\b',
        ]
        ignored_codes = {"THE", "AND", "WITH", "FOR", "GET", "APPLY", "SAVE", "OFF", "FLAT", "AT", "CHECKOUT"}
        for p_pat in promo_patterns:
            p_m = re.search(p_pat, text, re.IGNORECASE)
            if p_m:
                cand = p_m.group(1).upper()
                if cand not in ignored_codes:
                    result["promo_code"] = cand
                    break

        # 7. Bank Offers & Flat Card Discount
        bank_match = re.search(
            r'(?:Flat\s*(?:₹|Rs\.?)\s*[\d,]+\s*(?:Instant\s+Discount|off)\s+on\s+[A-Za-z0-9 ]+|'
            r'Instant Discount up to ₹?[\d,]+ on [A-Za-z0-9 ]+|'
            r'Bank Offer: [^<]{5,80}|'
            r'10% Instant Discount on [A-Za-z0-9 ]+|'
            r'(?:₹|Rs\.?)\s*[\d,]+\s*(?:Instant Discount|off)\s+on\s+[A-Za-z0-9 ]+)',
            text,
            re.IGNORECASE,
        )
        if bank_match:
            b_text = re.sub(r"\s+", " ", bank_match.group(0).strip())[:80]
            result["bank_offer"] = b_text
            # Parse unambiguous flat rupee discount
            flat_m = re.search(r'(?:Flat\s*(?:₹|Rs\.?)\s*([\d,]+)|(?:₹|Rs\.?)\s*([\d,]+)\s*off|Instant Discount up to (?:₹|Rs\.?)\s*([\d,]+))', b_text, re.IGNORECASE)
            if flat_m:
                raw_amt = flat_m.group(1) or flat_m.group(2) or flat_m.group(3)
                try:
                    amt = int(raw_amt.replace(",", ""))
                    if 50 <= amt <= 25000:
                        result["bank_discount_amount"] = amt
                except ValueError:
                    pass

        # 8. High-Res Image
        hires_match = re.search(r'data-old-hires="([^"]+)"', text)
        if hires_match and hires_match.group(1).startswith("http"):
            result["hi_res_image"] = hires_match.group(1)
        else:
            dyn_match = re.search(r'data-a-dynamic-image=\'({[^}]+})\'', text)
            if dyn_match:
                try:
                    import json
                    img_dict = json.loads(dyn_match.group(1))
                    if isinstance(img_dict, dict) and img_dict:
                        best_img = max(img_dict.items(), key=lambda kv: kv[1][0] * kv[1][1] if isinstance(kv[1], list) and len(kv[1]) >= 2 else 0)
                        if best_img[0].startswith("http"):
                            result["hi_res_image"] = best_img[0]
                except Exception:
                    pass

        return result
    except requests.RequestException as e:
        logger.warning(f"Enrichment request failed for {url}: {e}")
        return result


def mark_telegram_deal_price_spike(
    token: str,
    chat_id: str,
    message_id: int,
    original_caption: str,
    new_price: int,
    is_photo: bool = True,
) -> bool:
    """
    Update a previously posted Telegram message to prepend:
    ⚠️ [DEAL ENDED — PRICE INCREASED TO ₹{new_price:,}]
    """
    if not token or not chat_id or not message_id:
        return False

    spike_prefix = f"⚠️ <b>[DEAL ENDED — PRICE INCREASED TO ₹{new_price:,}]</b>\n\n"
    cleaned_caption = re.sub(
        r"^(?:❌\s*<b>\[OFFER EXPIRED / OUT OF STOCK\]</b>|⚠️\s*<b>\[DEAL ENDED[^\]]*\]</b>)\s*",
        "",
        original_caption,
        flags=re.IGNORECASE,
    ).strip()
    spike_caption = f"{spike_prefix}{cleaned_caption}"
    if len(spike_caption) > 1024:
        spike_caption = spike_caption[:1020] + "..."

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
        payload["caption"] = spike_caption
    else:
        payload["text"] = spike_caption

    try:
        from dealbot.publisher import safe_telegram_post
        r = safe_telegram_post(endpoint, payload=payload, timeout=10)
        if r.status_code == 200:
            logger.info(f"Successfully updated Telegram message {message_id} as PRICE SPIKE (₹{new_price:,}).")
            return True
        else:
            logger.warning(f"Telegram edit message failed ({r.status_code}): {r.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Error calling Telegram edit message: {e}")
        return False


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
