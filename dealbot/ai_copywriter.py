import html
import json
import logging
import os
import re
from typing import Optional
import requests
from dealbot.models import Deal

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

def sanitize_telegram_html(text: str) -> str:
    """
    Sanitize text to ensure it adheres strictly to Telegram HTML parse_mode constraints.
    Prevents 400 Bad Request: can't parse entities errors from unescaped brackets or unbalanced tags.
    """
    if not text:
        return ""

    # Replace markdown bold **text** or *text* with <b>text</b> if LLM outputs markdown
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    # Replace markdown bullets '- ' or '* ' at start of lines with '• '
    text = re.sub(r"^[*-]\s+", "• ", text, flags=re.MULTILINE)

    # Protect valid HTML tags by tokenizing
    tag_pattern = re.compile(r"(</?(?:b|i|s|u|code|pre|a)(?:\s+href=[\"'][^\"']*[\"'])?>)", re.IGNORECASE)
    tokens = tag_pattern.split(text)

    sanitized_parts = []
    open_tags = []

    for token in tokens:
        match = tag_pattern.fullmatch(token)
        if match:
            tag_str = match.group(1)
            is_closing = tag_str.startswith("</")
            tag_name_match = re.search(r"</?([a-zA-Z]+)", tag_str)
            tag_name = tag_name_match.group(1).lower() if tag_name_match else ""

            if is_closing:
                if open_tags and open_tags[-1] == tag_name:
                    open_tags.pop()
                    sanitized_parts.append(f"</{tag_name}>")
            else:
                open_tags.append(tag_name)
                if tag_name == "a":
                    href_match = re.search(r'href=[\"\']([^\"\']*)[\"\']', tag_str, re.IGNORECASE)
                    href = href_match.group(1) if href_match else "#"
                    sanitized_parts.append(f'<a href="{html.escape(href, quote=True)}">')
                else:
                    sanitized_parts.append(f"<{tag_name}>")
        else:
            escaped_text = html.escape(token)
            sanitized_parts.append(escaped_text)

    # Close any unclosed tags
    while open_tags:
        closing = open_tags.pop()
        sanitized_parts.append(f"</{closing}>")

    return "".join(sanitized_parts)

class AICopywriter:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = (model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)).strip()
        self.timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "8"))

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "replace_me")

    def craft_deal_copy(self, deal: Deal) -> Optional[str]:
        """
        Generate high-converting, punchy Telegram deal copy using Gemini Flash.
        Returns None on any error or if unconfigured, triggering automatic template fallback.
        """
        if not self.is_configured():
            return None

        prompt = (
            f"You are an expert e-commerce copywriter for an Indian Telegram deal channel.\n"
            f"Transform this product deal into a viral Telegram post:\n\n"
            f"Title: {deal.title}\n"
            f"Retailer: {deal.retailer}\n"
            f"Deal Price: {deal.current_price or 'Best Deal'}\n"
            f"MRP: {deal.original_price or 'N/A'}\n"
            f"Discount: {f'{deal.discount_percent}% OFF' if deal.discount_percent else 'Special Offer'}\n\n"
            f"Rules:\n"
            f"1. First line: High-energy headline with emoji (e.g. 🔥 STEAL DEAL: Short Product Name!)\n"
            f"2. Exactly 2-3 bullet points with emojis highlighting key features and specs.\n"
            f"3. Price line: 💰 Deal Price: {deal.current_price or 'Best Price'} | ❌ MRP: <s>{deal.original_price or ''}</s> | ⚡ Discount: {deal.discount_percent or ''}% OFF\n"
            f"4. Output ONLY valid Telegram HTML tags: <b>, <i>, <s>. NEVER use markdown (* or ** or #).\n"
            f"5. Do NOT include any URLs, buy links, or disclosures (appended automatically).\n"
            f"6. Keep it concise (under 80 words total).\n"
        )

        url = f"{API_BASE}/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 600,
                "thinkingConfig": {"thinkingBudget": 0}
            }
        }

        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout
            )
            if resp.status_code != 200:
                logger.warning(f"Gemini API returned status {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None

            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            if not raw_text:
                return None

            cleaned = sanitize_telegram_html(raw_text)
            return cleaned

        except requests.Timeout:
            logger.warning("Gemini API call timed out. Falling back to template caption.")
            return None
        except requests.RequestException as e:
            logger.warning(f"Gemini API request failed: {e}. Falling back to template caption.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in AICopywriter: {e}. Falling back to template caption.")
            return None
