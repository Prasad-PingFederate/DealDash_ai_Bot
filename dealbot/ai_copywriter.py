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
# Free Gemma 4 26B A4B on OpenRouter. Near-31B quality, but only ~4B parameters
# run per token, so a short JSON pick or one-sentence caption returns inside
# the 12s limit. The free 31B route is often queued past that limit, and
# openrouter/free swaps models every call, which makes the JSON pick unreliable.
GEMMA_BACKUP_MODEL = "google/gemma-4-26b-a4b-it:free"
DEFAULT_HERMES_MODEL = GEMMA_BACKUP_MODEL
UNSAFE_BACKUP_MODELS = {
    "openrouter/free",
    "google/gemma-4-31b-it:free",
    "nousresearch/hermes-3-llama-3.1-8b",
}
DEFAULT_HERMES_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
HERMES_PROVIDERS = {"hermes", "nous-hermes", "openrouter", "hf", "gemma"}
_PLACEHOLDER_KEYS = {"", "replace_me"}


def _usable_key(value: Optional[str]) -> bool:
    return bool(value) and value.strip().lower() not in _PLACEHOLDER_KEYS


def resolve_backup_model(raw: Optional[str]) -> str:
    """Pin the OpenRouter backup to the free 26B Gemma route."""
    model = (raw or "").strip()
    if model.lower() in UNSAFE_BACKUP_MODELS or not model:
        if model and model != GEMMA_BACKUP_MODEL:
            logger.warning(
                "HERMES_MODEL %s is ignored. Use %s. The free 31B route is often "
                "queued past the 12s timeout, and openrouter/free changes model on every call.",
                model,
                GEMMA_BACKUP_MODEL,
            )
        return GEMMA_BACKUP_MODEL
    return model


def gemini_quota_failure(status: int, body: str) -> bool:
    """True when Gemini will not answer until the key or quota is fixed."""
    if status in (401, 403, 429):
        return True
    blob = (body or "").lower()
    markers = (
        "quota",
        "resource_exhausted",
        "resource has been exhausted",
        "billing",
        "api key not valid",
        "api_key_invalid",
        "permission_denied",
        "permission denied",
        "exceeded your current quota",
    )
    return any(marker in blob for marker in markers)

def ensure_valid_telegram_html(text: str) -> str:
    """
    Sanitize and balance HTML tags to adhere strictly to Telegram parse_mode='HTML' constraints.
    - Replaces <br>, <p>, <div> with newlines/whitespace.
    - Strips unsupported tags (<span>, <font>, <h1>, etc.) while preserving inner text.
    - Validates and balances allowed Telegram tags (<b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>, <code>, <pre>, <a>, <blockquote>).
    - Escapes unescaped naked brackets (<, >) and ampersands (&) and malicious tags (<script> -> &lt;script&gt;).
    - Auto-closes any unclosed tags at the end of the text.
    """
    if not text:
        return ""

    # 1. Normalize linebreaks and common LLM block tags
    t = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    t = re.sub(r"</?p\s*>", "\n\n", t, flags=re.IGNORECASE)
    t = re.sub(r"</?div\s*>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"</?span(?:\s+[^>]*)?>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"</?h[1-6]\s*>", "", t, flags=re.IGNORECASE)

    # 2. Markdown conversions (bold, italics, bullets)
    t = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"^[*-]\s+", "• ", t, flags=re.MULTILINE)

    # 3. Tokenize by allowed Telegram tags
    allowed_tag_pattern = re.compile(
        r"""(</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote)(?:\s+href=['"][^'"]*['"])?>)""",
        re.IGNORECASE,
    )
    tokens = allowed_tag_pattern.split(t)
    sanitized_parts = []
    open_tags = []

    for token in tokens:
        match = allowed_tag_pattern.fullmatch(token)
        if match:
            tag_str = match.group(1)
            is_closing = tag_str.startswith("</")
            tag_name_match = re.search(r"</?([a-zA-Z]+)", tag_str)
            raw_tag_name = tag_name_match.group(1).lower() if tag_name_match else ""
            tag_map = {"strong": "b", "em": "i", "strike": "s", "del": "s", "ins": "u"}
            tag_name = tag_map.get(raw_tag_name, raw_tag_name)

            if is_closing:
                if tag_name in open_tags:
                    while open_tags and open_tags[-1] != tag_name:
                        sanitized_parts.append(f"</{open_tags.pop()}>")
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
            sanitized_parts.append(html.escape(token, quote=False))

    # 4. Auto-close remaining open tags in reverse order
    while open_tags:
        closing = open_tags.pop()
        sanitized_parts.append(f"</{closing}>")

    result = "".join(sanitized_parts)
    return result

sanitize_telegram_html = ensure_valid_telegram_html

def validate_critic(copy_text: str, deal: Deal) -> bool:
    """
    Strict Critic: verify that the LLM did not hallucinate a price,
    discount percentage, coupon code, or bank offer absent from deal metadata.
    Normalizes Indian number formatting (₹1,24,999) and decimals (.00).
    """
    if not copy_text:
        return False

    # Validate that Telegram HTML parses and is balanced
    valid_html = ensure_valid_telegram_html(copy_text)
    if not valid_html:
        logger.info("Critic rejected AI copy: invalid or empty Telegram HTML.")
        return False

    from dealbot.models import parse_rupees
    cur_p = parse_rupees(deal.current_price)
    eff_p = getattr(deal, "effective_price", None)
    org_p = parse_rupees(deal.original_price)
    sav_p = deal.savings_rupees()
    valid_prices = {p for p in (cur_p, eff_p, org_p, sav_p) if p is not None}

    # Normalize copy text: remove trailing .00 from currency amounts
    clean_copy = re.sub(r"\.00\b", "", copy_text)

    # 1. Price check: deal price digits (current or effective price) must be present
    if cur_p is not None or eff_p is not None:
        clean_digits = re.sub(r"\D", "", clean_copy)
        price_found = (cur_p is not None and str(cur_p) in clean_digits) or (
            eff_p is not None and str(eff_p) in clean_digits
        )
        if not price_found:
            logger.info(f"Critic rejected AI copy: real deal price digits ({cur_p} / {eff_p}) missing.")
            return False

    # Extract all prices mentioned in copy (e.g. ₹1,24,999 or Rs. 1,499.00 or ₹899)
    mentioned_prices = []
    for match in re.findall(r"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)", copy_text, re.IGNORECASE):
        try:
            val = int(round(float(match.replace(",", ""))))
            mentioned_prices.append(val)
        except ValueError:
            pass

    if mentioned_prices and valid_prices:
        for p in mentioned_prices:
            if not any(abs(p - vp) <= 2 for vp in valid_prices):
                logger.info(f"Critic rejected AI copy: hallucinated price ₹{p} (valid: {valid_prices})")
                return False

    # 2. Discount check: reject wild discount hallucination
    stated_discount = deal.discount_percent
    for disc_match in re.findall(r"(\d{1,2})\s*%\s*(?:off|discount)?", copy_text, re.IGNORECASE):
        disc_val = int(disc_match)
        if stated_discount and abs(disc_val - stated_discount) > 15:
            logger.info(f"Critic rejected AI copy: hallucinated discount {disc_val}% vs stated {stated_discount}%")
            return False

    # 3. Coupon check: reject fake coupon codes
    ignored_coupon_words = {
        "savings", "discount", "discounts", "offer", "offers", "deal", "deals",
        "available", "applied", "checkout", "page", "box", "apply", "voucher", "code", "extra"
    }
    coupon_matches = re.findall(
        r"\b(?:coupon|promo\s*code|code)\s*[:=]?\s*['\"]?([A-Za-z0-9_-]{3,20})['\"]?",
        copy_text,
        re.IGNORECASE,
    )
    valid_coupon_str = f"{deal.coupon_code or ''} {getattr(deal, 'promo_code', '') or ''}".upper().strip()
    for c_code in coupon_matches:
        if c_code.lower() in ignored_coupon_words:
            continue
        if not valid_coupon_str or c_code.upper() not in valid_coupon_str:
            logger.info(f"Critic rejected AI copy: hallucinated coupon '{c_code}'")
            return False

    # 4. Bank offer check: verify mentioned banks exist in deal metadata
    known_banks = ["hdfc", "icici", "sbi", "axis", "kotak", "citi", "bob", "yes bank", "federal"]
    bank_offer_lower = (deal.bank_offer or "").lower()
    for bank in known_banks:
        if re.search(r"\b" + re.escape(bank) + r"\b", copy_text, re.IGNORECASE):
            if bank not in bank_offer_lower:
                logger.info(f"Critic rejected AI copy: hallucinated bank offer '{bank}' (metadata: {deal.bank_offer})")
                return False

    return True

class AICopywriter:
    """
    Gemini first. If that key is empty or the quota is dead, the same call
    continues on the free Gemma model through OpenRouter. A template is used
    only when both are unavailable.
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

        self.hermes_api_key = (
            os.getenv("HERMES_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("DEALS_AGENT_OPENROUTER")
            or os.getenv("HF_TOKEN", "")
        ).strip()
        self.hermes_model = resolve_backup_model(os.getenv("HERMES_MODEL", DEFAULT_HERMES_MODEL))
        self.hermes_endpoint = (
            hermes_endpoint or os.getenv("HERMES_ENDPOINT", DEFAULT_HERMES_ENDPOINT)
        ).strip()

        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        self.groq_endpoint = os.getenv("GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions").strip()

        self.timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "12"))
        self.sale_mode = os.getenv("SALE_MODE", "").strip().lower()
        self.last_engine = None
        self._gemini_quota_dead = False

    def _gemini_ready(self) -> bool:
        return _usable_key(self.api_key)

    def _hermes_ready(self) -> bool:
        return _usable_key(self.hermes_api_key)

    def _groq_ready(self) -> bool:
        return _usable_key(self.groq_api_key)

    def _uses_hermes_only(self) -> bool:
        return self.provider in HERMES_PROVIDERS

    def is_configured(self) -> bool:
        return self._gemini_ready() or self._groq_ready() or self._hermes_ready()

    def describe(self) -> str:
        parts = []
        if self._gemini_ready():
            parts.append(f"Gemini {self.model}")
        if self._hermes_ready():
            parts.append(f"Gemma backup {self.hermes_model}")
        return " + ".join(parts) or "unconfigured"

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

    def generate_text(
        self, prompt: str, system_prompt: Optional[str] = None, response_json: bool = False
    ) -> Optional[str]:
        """Smart rate-limit & fallback chain: Gemini (primary) -> Groq (secondary) -> OpenRouter Hermes (tertiary)."""
        if not self.is_configured():
            return None

        self.last_engine = None
        self._gemini_quota_dead = False

        # 1. Primary: Gemini
        if self._gemini_ready():
            text = self._call_gemini(prompt, system_prompt, response_json=response_json)
            if text:
                self.last_engine = "gemini"
                return text
            if not self._gemini_quota_dead and not self._uses_hermes_only() and not self._groq_ready():
                return None

        # 2. Secondary: Groq (if provided)
        if self._groq_ready():
            logger.info("Using secondary LLM provider: Groq (%s)", self.groq_model)
            text = self._call_groq(prompt, system_prompt)
            if text:
                self.last_engine = "groq"
                return text

        # 3. Tertiary: OpenRouter (Hermes / Gemma)
        if self._hermes_ready():
            logger.info("Using tertiary LLM provider: OpenRouter (%s)", self.hermes_model)
            text = self._call_hermes(prompt, system_prompt)
            if text:
                self.last_engine = "gemma"
                return text

        # 4. Final Fallback: Returns None so caller falls back to deterministic heuristic template
        return None

    def _call_groq(self, prompt: str, system_prompt: Optional[str] = None) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        sys_msg = system_prompt or "You are a concise deals editor. Reply with only what was asked."
        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 1000,
        }
        try:
            resp = requests.post(self.groq_endpoint, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
            else:
                logger.warning(f"Groq API returned status {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Groq API call failed: {e}")
        return None

    def _call_gemini(
        self, prompt: str, system_prompt: Optional[str] = None, response_json: bool = False
    ) -> Optional[str]:
        full_text = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        url = f"{GEMINI_API_BASE}/{self.model}:generateContent?key={self.api_key}"
        gen_config = {
            "temperature": 0.2 if response_json else 0.3,
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingBudget": 0},
        }
        if response_json:
            gen_config["responseMimeType"] = "application/json"

        payload = {
            "contents": [{"parts": [{"text": full_text}]}],
            "generationConfig": gen_config,
        }

        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
            # If 400 Bad Request, retry without thinkingConfig/responseMimeType
            if resp.status_code == 400 and ("thinkingConfig" in gen_config or "responseMimeType" in gen_config):
                logger.info("Retrying Gemini without thinkingConfig/responseMimeType.")
                gen_config.pop("thinkingConfig", None)
                gen_config.pop("responseMimeType", None)
                payload["generationConfig"] = gen_config
                resp = requests.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )

            if resp.status_code != 200:
                body = resp.text[:500]
                if gemini_quota_failure(resp.status_code, body):
                    self._gemini_quota_dead = True
                    logger.warning("Gemini quota or key failure (%s): %s", resp.status_code, body[:200])
                else:
                    logger.warning(f"Gemini API returned status {resp.status_code}: {body[:200]}")
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
            "X-Title": "DealDash AI Bot",
        }

        sys_msg = system_prompt or "You are a concise deals editor. Reply with only what was asked."
        
        # Multi-model fallback sequence for OpenRouter
        candidate_models = []
        for m in [
            self.hermes_model,
            "google/gemma-3-27b-it:free",
            "nousresearch/hermes-3-llama-3.1-405b:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "openrouter/free",
        ]:
            if m and m not in candidate_models:
                candidate_models.append(m)

        for model_name in candidate_models:
            payload = {
                "model": model_name,
                "models": candidate_models,  # OpenRouter multi-model fallback array
                "messages": [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 1000,
            }
            if "gemma" in model_name.lower():
                payload["reasoning"] = {"effort": "none", "exclude": True}

            try:
                resp = requests.post(
                    self.hermes_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        out = choices[0].get("message", {}).get("content", "").strip()
                        if out:
                            logger.info(f"OpenRouter answered successfully via model: {model_name}")
                            return out
                elif resp.status_code in (429, 404, 500, 502, 503, 504):
                    logger.warning(
                        f"OpenRouter model {model_name} failed ({resp.status_code}), attempting next fallback model..."
                    )
                    continue
                else:
                    logger.warning(f"OpenRouter API returned status {resp.status_code}: {resp.text[:200]}")
            except requests.Timeout:
                logger.warning(f"OpenRouter model {model_name} timed out, trying next fallback model...")
                continue
            except requests.RequestException as e:
                logger.warning(f"OpenRouter request error on {model_name}: {e}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error on {model_name}: {e}")
                continue

        return None

    def craft_deal_copy(self, deal: Deal) -> Optional[str]:
        """
        Generate high-converting, punchy Telegram deal copy.
        Returns cached batched copy if available, or calls LLM with Critic validation.
        """
        cached = getattr(deal, "_cached_ai_copy", None)
        if cached and validate_critic(cached, deal):
            return sanitize_telegram_html(cached)

        if not self.is_configured():
            return None

        sale_directive = self._get_sale_mode_directive()
        system_prompt = (
            "You are an expert e-commerce copywriter for an Indian Telegram deal channel.\n"
            "Your goal is to write high-converting, viral Telegram posts for Indian online shoppers."
        )

        extra_ctx = []
        if getattr(deal, "coupon_discount", None) or getattr(deal, "coupon_text", None):
            extra_ctx.append(f"Coupon: Tick '{deal.coupon_text or f'Apply ₹{deal.coupon_discount} coupon'}' on Amazon page.")
        if getattr(deal, "extra_offers", None):
            extra_ctx.append(f"Bonus Offers: {', '.join(deal.extra_offers[:2])}")
        extra_info = ("\n" + "\n".join(extra_ctx) + "\n") if extra_ctx else "\n"

        user_prompt = (
            f"Write ONE sentence for an Indian Telegram deal post.\n"
            f"Title: {deal.title}\n"
            f"Retailer: {deal.retailer}\n"
            f"Deal Price: {deal.current_price or 'the deal price'}\n"
            f"MRP: {deal.original_price or 'N/A'}\n"
            f"Discount: {f'{deal.discount_percent}% OFF' if deal.discount_percent else 'Special Offer'}\n"
            f"{sale_directive}"
            f"{extra_info}"
            f"Rules:\n"
            f"1. One sentence only, under 140 characters, include the deal price digits.\n"
            f"2. No URL, no hashtag, no buy button.\n"
            f"3. Plain text or <b>/<i> only.\n"
        )

        raw_text = self.generate_text(prompt=user_prompt, system_prompt=system_prompt)
        if not raw_text:
            return None

        if not validate_critic(raw_text, deal):
            logger.info("Critic rejected crafted copy due to hallucinated price/discount/coupon.")
            return None

        cleaned = sanitize_telegram_html(raw_text)
        return cleaned
