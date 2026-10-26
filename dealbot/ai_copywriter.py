import html
import json
import logging
import os
import re
from typing import Optional
import requests
from dealbot.models import Deal

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_HERMES_MODEL = "nousresearch/hermes-3-llama-3.1-8b"
DEFAULT_HERMES_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

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
    tag_pattern = re.compile(r"""(</?(?:b|i|s|u|code|pre|a)(?:\s+href=['"][^'"]*['"])?>)""", re.IGNORECASE)
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
                    href_match = re.search(r"""href=['"]([^'"]*)['"]""", tag_str, re.IGNORECASE)
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
    """
    Dual-Provider AI Engine supporting:
    1. Google Gemini Flash (default, free API)
    2. Nous Hermes (via OpenRouter or Hugging Face Inference API)
    3. Seasonal Festival / Sale Mode Boosters (Diwali, Great Indian Festival, Prime Day)
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        hermes_endpoint: Optional[str] = None,
    ):
        self.provider = (provider or os.getenv("AI_PROVIDER", "gemini")).strip().lower()
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = (model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)).strip()

        # Nous Hermes configuration
        self.hermes_api_key = (
            os.getenv("HERMES_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("HF_TOKEN", "")
        ).strip()
        self.hermes_model = (os.getenv("HERMES_MODEL", DEFAULT_HERMES_MODEL)).strip()
        self.hermes_endpoint = (
            hermes_endpoint or os.getenv("HERMES_ENDPOINT", DEFAULT_HERMES_ENDPOINT)
        ).strip()

        self.timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "12"))
        self.sale_mode = os.getenv("SALE_MODE", "").strip().lower()

    def is_configured(self) -> bool:
        if self.provider in ["hermes", "nous-hermes", "openrouter", "hf"]:
            return bool(self.hermes_api_key and self.hermes_api_key != "replace_me")
        return bool(self.api_key and self.api_key != "replace_me")

    def _get_sale_mode_directive(self) -> str:
        if not self.sale_mode:
            return ""

        if self.sale_mode == "diwali":
            return (
                "\nSPECIAL EVENT - DIWALI MEGA SALE:\n"
                "• Headline must start with '🪔 DIWALI MEGA LOOT:'\n"
                "• Use festive, high-urgency tone (Diwali gift / festival lights / festive special).\n"
            )
        elif self.sale_mode in ["festival", "great_indian_festival", "gif"]:
            return (
                "\nSPECIAL EVENT - GREAT INDIAN FESTIVAL:\n"
                "• Headline must start with '🛍️ GREAT INDIAN FESTIVAL DEAL:'\n"
                "• Highlight massive festival price drop and limited festive stock.\n"
            )
        elif self.sale_mode == "prime_day":
            return (
                "\nSPECIAL EVENT - PRIME DAY:\n"
                "• Headline must start with '👑 PRIME DAY EXCLUSIVE:'\n"
                "• Emphasize limited-time lightning discount.\n"
            )
        elif self.sale_mode == "flash":
            return (
                "\nSPECIAL EVENT - LIGHTNING FLASH SALE:\n"
                "• Headline must start with '⚡ FLASH LOOT DEAL:'\n"
                "• Emphasize lightning urgency (price may jump in minutes).\n"
            )
        return ""

    def generate_text(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        """
        Generic text generation supporting Gemini or Nous Hermes.
        Used for deal copywriting as well as Growth Advisor strategic analysis.
        """
        if not self.is_configured():
            return None

        if self.provider in ["hermes", "nous-hermes", "openrouter", "hf"]:
            return self._call_hermes(prompt, system_prompt)
        return self._call_gemini(prompt, system_prompt)

    def _call_gemini(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        full_text = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        url = f"{GEMINI_API_BASE}/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": full_text}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1000,
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

            return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()

        except requests.Timeout:
            logger.warning("Gemini API call timed out.")
            return None
        except requests.RequestException as e:
            logger.warning(f"Gemini API request failed: {e}.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in Gemini AICopywriter: {e}.")
            return None

    def _call_hermes(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {self.hermes_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Prasad-PingFederate/DealDash_ai_Bot",
            "X-Title": "DealDash AI Bot (Nous Hermes Engine)",
        }

        sys_msg = system_prompt or "You are Hermes, an autonomous AI assistant."
        payload = {
            "model": self.hermes_model,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 1000
        }

        try:
            resp = requests.post(
                self.hermes_endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )
            if resp.status_code != 200:
                logger.warning(f"Nous Hermes API returned status {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return None

            return choices[0].get("message", {}).get("content", "").strip()

        except requests.Timeout:
            logger.warning("Nous Hermes API call timed out.")
            return None
        except requests.RequestException as e:
            logger.warning(f"Nous Hermes API request failed: {e}.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in Hermes AICopywriter: {e}.")
            return None

    def craft_deal_copy(self, deal: Deal) -> Optional[str]:
        """
        Generate high-converting, punchy Telegram deal copy using Gemini or Nous Hermes.
        Returns None on any error or if unconfigured, triggering automatic template fallback.
        """
        if not self.is_configured():
            return None

        sale_directive = self._get_sale_mode_directive()
        system_prompt = (
            "You are an expert e-commerce copywriter for an Indian Telegram deal channel.\n"
            "Your goal is to write high-converting, viral Telegram posts for Indian online shoppers."
        )

        user_prompt = (
            f"Write ONE sentence for an Indian Telegram deal post.\n"
            f"Title: {deal.title}\n"
            f"Retailer: {deal.retailer}\n"
            f"Deal Price: {deal.current_price or 'the deal price'}\n"
            f"MRP: {deal.original_price or 'N/A'}\n"
            f"Discount: {f'{deal.discount_percent}% OFF' if deal.discount_percent else 'Special Offer'}\n"
            f"{sale_directive}"
            f"Rules:\n"
            f"1. One sentence only, under 140 characters, include the deal price digits.\n"
            f"2. No URL, no hashtag, no buy button.\n"
            f"3. Plain text or <b>/<i> only.\n"
        )

        raw_text = self.generate_text(prompt=user_prompt, system_prompt=system_prompt)
        if not raw_text:
            return None

        cleaned = sanitize_telegram_html(raw_text)
        return cleaned
