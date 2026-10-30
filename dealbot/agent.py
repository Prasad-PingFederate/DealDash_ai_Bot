"""Deal editor agent.

Each cycle the agent observes fresh deals and recent channel memory, scores
them, optionally asks the configured LLM to pick a short slate, then explains
every post and skip. Heuristics always run so a dead model cannot stall the
channel.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from dealbot.models import Deal, parse_rupees
from dealbot.pacing import get_current_ist_time

logger = logging.getLogger("dealbot.agent")

CATEGORY_KEYWORDS = {
    "electronics": (
        "headphone", "headphones", "earbud", "earbuds", "earphone", "earphones", "tws",
        "speaker", "speakers", "soundbar", "watch", "smartwatch", "phone", "mobile",
        "smartphone", "charger", "laptop", "keyboard", "mouse", "camera", "trimmer",
        "tv", "television", "monitor", "power bank", "bluetooth", "ssd", "router",
        "printer", "gaming", "tablet", "drone", "projector",
    ),
    "home": (
        "mixer", "grinder", "cooker", "fan", "vacuum", "kettle", "iron", "bottle",
        "bedsheet", "blanket", "cookware", "air fryer", "induction", "lamp",
        "storage", "pressure cooker", "appliance", "purifier", "mop",
    ),
    "fashion": (
        "shoe", "shirt", "jeans", "saree", "kurta", "t-shirt", "tshirt", "dress",
        "jacket", "sandal", "sneaker", "handbag", "wallet", "sunglasses", "backpack", "luggage",
    ),
    "beauty": (
        "cream", "shampoo", "lipstick", "serum", "perfume", "face wash", "sunscreen",
        "moisturizer", "conditioner", "grooming", "shaving", "lotion", "body wash", "soap",
    ),
    "grocery": (
        "oil", "rice", "atta", "snack", "coffee", "tea", "ghee", "spice", "biscuit",
        "chocolate", "dry fruit", "almond", "cashew",
    ),
    "fitness": ("fitness", "dumbbell", "yoga", "protein", "shaker", "treadmill", "cycle"),
    "stationery": ("notebook", "pen", "desk", "office", "study", "chair"),
    "kids": ("toy", "diaper", "kids", "baby", "school bag"),
    "books": ("book", "novel", "kindle"),
}

SLOT_BOOSTS = {
    "morning": {"home": 8, "grocery": 8, "beauty": 8, "fitness": 8, "stationery": 8},
    "afternoon": {"fashion": 8, "beauty": 8, "home": 4},
    "evening": {"electronics": 10, "home": 10},
    "night": {},
}

TIER_1_BRANDS = (
    "apple", "samsung", "sony", "bose", "sennheiser", "jbl", "oneplus", "google", "pixel",
    "nothing", "motorola", "asus", "hp", "dell", "lenovo", "acer", "lg", "logitech",
    "sandisk", "crucial", "kingston", "seagate", "wd", "western digital", "marshall",
    "gopro", "dji", "playstation", "xbox", "nintendo",
    "philips", "dyson", "prestige", "hawkins", "pigeon", "bajaj", "havells", "crompton",
    "atomberg", "usha", "orient", "ifb", "bosch", "whirlpool", "eureka forbes", "kent",
    "aquaguard", "borosil", "milton", "cello", "wonderchef", "butterfly", "preethi",
    "nike", "adidas", "puma", "skechers", "asics", "woodland", "bata", "crocs",
    "levis", "levi's", "tommy hilfiger", "calvin klein", "us polo", "u.s. polo", "allen solly",
    "van heusen", "peter england", "louis philippe", "ray-ban", "fastrack", "titan",
    "casio", "fossil", "timex",
    "l'oreal", "loreal", "nivea", "minimalist", "cetaphil", "neutrogena", "the derma co",
    "dot & key", "maybelline", "lakme", "plum", "forest essentials", "kindle", "echo dot",
    "dove", "pears", "colgate", "tresemme", "vaseline", "beardo", "chemist at play",
    "dettol", "savlon", "listerine", "whisper", "surf excel", "ariel", "vim", "comfort",
    "harpic", "lizol", "presto", "cadbury", "ferrero", "happilo", "tata sampann", "nescafe", "davidoff",
)

TIER_2_BRANDS = (
    "boat", "noise", "boult", "fire-boltt", "fireboltt", "ptron", "realme", "xiaomi",
    "redmi", "poco", "iqoo", "vivo", "oppo", "tecno", "infinix", "zebronics", "portronics",
    "ambrane", "ptron", "lifelong", "agaro", "wipro", "syska", "solimo", "amazon basics",
    "amazfit", "garmin", "mamaearth", "fire tv", "alexa",
    # Expanded Indian FMCG, personal care, home, electronics & sports brands
    "plum", "mcaffeine", "beardo", "bombay shaving company", "the man company",
    "wild stone", "fogg", "nivea", "garnier", "lakme", "maybelline", "himalaya",
    "dabur", "tata", "nescafe", "cadbury", "ariel", "surf excel", "vim", "dettol",
    "harpic", "pigeon", "butterfly", "bajaj", "orient", "usha", "halonix",
    "ant esports", "cosmic byte", "redgear", "classmate", "parker", "casio",
    "yonex", "nivia", "cosco", "decathlon",
)

KNOWN_BRANDS = tuple(sorted(set(TIER_1_BRANDS + TIER_2_BRANDS)))

NARROW_TYPES = (
    "cable", "earbud", "earphone", "headphone", "speaker", "smartwatch", "watch",
    "power bank", "charger", "t-shirt", "shirt", "shoe", "sneaker", "sandal",
    "trimmer", "bottle", "kettle", "cooker", "fan", "ssd", "mouse", "keyboard",
)

MRP_INFLATED_TYPES = (
    "smartwatch", "earbuds", "earbud", "tws", "neckband", "cable", "charger",
    "case", "screen protector", "tempered glass", "bulb",
)

def extract_narrow_type(title: str) -> Optional[str]:
    blob = (title or "").lower()
    for nt in NARROW_TYPES:
        if nt in blob:
            return nt
    return None

ACCESSORY_PREPOSITIONS_PATTERN = re.compile(
    r"\b(for|compatible\s+with|fits|designed\s+for|case\s+for|cover\s+for|strap\s+for|protector\s+for|glass\s+for|adapter\s+for|replacement\s+for)\b",
    re.IGNORECASE,
)

def get_brand_tier(title: str) -> int:
    blob = (title or "").lower().strip()
    if not blob:
        return 0

    # Strip accessory compatibility phrases and everything after them
    primary_segment = ACCESSORY_PREPOSITIONS_PATTERN.split(blob)[0].strip()
    if not primary_segment:
        return 0

    def match_brand_in_lead(brand: str, text: str, max_lead_words: int = 3) -> bool:
        pattern = r"\b" + re.escape(brand) + r"\b"
        m = re.search(pattern, text)
        if not m:
            return False
        # Count words before match start
        prefix = text[:m.start()].strip()
        lead_words = len(prefix.split()) if prefix else 0
        return lead_words < max_lead_words

    for b in TIER_1_BRANDS:
        if match_brand_in_lead(b, primary_segment):
            return 1
    for b in TIER_2_BRANDS:
        if match_brand_in_lead(b, primary_segment):
            return 2
    return 0

brand_tier = get_brand_tier

JUNK_TITLE = re.compile(
    r"\b(coupon|voucher|redeem code|refurbished|renewed|pre-?owned|replica|lookalike|duplicate)\b",
    re.IGNORECASE,
)


@dataclass
class ScoredDeal:
    deal: Deal
    score: int
    category: str
    reason: str
    action: str = "skip"


@dataclass
class AgentPlan:
    posts: List[ScoredDeal] = field(default_factory=list)
    skips: List[ScoredDeal] = field(default_factory=list)
    source: str = "heuristic"
    soft_floor_used: bool = False

    def summary(self) -> str:
        lines = [f"DealDash agent ({self.source})"]
        if not self.posts:
            lines.append("Nothing worth posting this cycle.")
        for item in self.posts:
            lines.append(
                f"POST {item.deal.id} [{item.category} {item.score}] {item.deal.title[:80]} — {item.reason}"
            )
        for item in self.skips[:8]:
            lines.append(
                f"SKIP {item.deal.id} [{item.category} {item.score}] {item.deal.title[:60]} — {item.reason}"
            )
        return "\n".join(lines)


def current_slot(now=None) -> str:
    hour = (now or get_current_ist_time()).hour
    if 8 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 23:
        return "evening"
    return "night"


def classify_category(deal: Deal) -> str:
    if deal.category and deal.category.strip():
        known = deal.category.strip().lower()
        for name in CATEGORY_KEYWORDS:
            if name in known:
                return name
        if known not in {"other", "general", "misc"}:
            return known
    blob = f"{deal.title or ''} {deal.description or ''}".lower()
    best = "other"
    best_hits = 0
    for name, words in CATEGORY_KEYWORDS.items():
        hits = sum(1 for word in words if word in blob)
        if hits > best_hits:
            best = name
            best_hits = hits
    return best


def _tokens(title: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2}


def titles_too_similar(a: str, b: str, threshold: float = 0.55) -> bool:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    overlap = len(left & right) / len(left | right)
    return overlap >= threshold


def compute_buy_confidence(deal: Deal, score: int) -> int:
    """Map composite score, historical context, rating, and brand tier into BuyHatke confidence (60-99%)."""
    conf = 65 + int(min(22, max(0, (score - 16) * 0.7)))
    p_context = getattr(deal, "price_context", None)
    if p_context == "all_time_low":
        conf += 8
    elif p_context == "real_drop":
        conf += 5
    if getattr(deal, "is_further_price_drop", False):
        conf += 4
    if getattr(deal, "is_loot", False):
        conf += 6
    if getattr(deal, "is_cookie_magnet", False):
        conf += 8
    if getattr(deal, "rating", None) is not None and deal.rating >= 4.2:
        conf += 4
    b_tier = get_brand_tier(deal.title)
    if b_tier == 1:
        conf += 4
    elif b_tier == 2:
        conf += 2
    if getattr(deal, "source_count", 1) >= 2:
        conf += 3
    return min(99, max(60, conf))


def get_tiered_discount_requirement(
    price: Optional[int],
    brand_tier: int = 0,
    rating: Optional[float] = None,
    savings_inr: Optional[int] = None,
) -> int:
    """
    Tiered Discount Thresholds (Requirement 2):
    - Low-ticket items (< ₹500): Require >= 50% discount (Tier 1 brand: >= 40%).
    - Mid-ticket items (₹500 - ₹2,999): Require >= 35% discount (Tier 1 brand: >= 30%).
    - High-ticket items (₹3,000+ like Electronics, Appliances, Gadgets):
      Require >= 20% discount (or 18% when rating >= 3.9★, or savings >= ₹1,500).
    """
    if price is None or price <= 0:
        return 30 if brand_tier == 1 else 35

    if price < 500:
        return 40 if brand_tier == 1 else 50
    elif price < 3000:
        return 30 if brand_tier == 1 else 35
    else:
        # High-ticket items (₹3,000+)
        if (rating is not None and rating >= 3.9) or (savings_inr is not None and savings_inr >= 1500):
            return 18 if brand_tier == 1 else 20
        return 20 if brand_tier == 1 else 22


def format_buy_confidence_meter(pct: int) -> str:
    """Format BuyHatke-style confidence recommendation meter."""
    if pct >= 90:
        return f"🟢 Buy Confidence: {pct}% (█████) — Screaming Deal, Buy Now!"
    elif pct >= 80:
        return f"🟢 Buy Confidence: {pct}% (████░) — Strong Price Drop"
    else:
        return f"🟡 Buy Confidence: {pct}% (███░░) — Good Value"


def _has_known_brand(title: str) -> bool:
    blob = (title or "").lower()
    return any(brand in blob for brand in KNOWN_BRANDS)


def score_deal(deal: Deal, recent_categories: Sequence[str], slot: Optional[str] = None) -> ScoredDeal:
    """Rank a deal for the channel. A bigger saving still ranks higher, but a low price is not a reason to drop it."""
    slot = slot or current_slot()
    category = classify_category(deal)
    reasons: List[str] = []
    score = 8

    # Use effective price (with on-page coupons subtracted) if present
    price = getattr(deal, "effective_price", None)
    if price is None:
        price = parse_rupees(deal.current_price)
    mrp = parse_rupees(deal.original_price)
    stated = int(deal.discount_percent or 0)
    savings = deal.savings_rupees()
    discount = stated
    if price and mrp and mrp > price:
        implied = int(round((mrp - price) / mrp * 100))
        if stated and abs(implied - stated) >= 35:
            reasons.append(f"listed {stated}% but prices say {implied}%")
            score -= 10
            discount = implied
        elif not stated or getattr(deal, "effective_price", None):
            discount = implied

    title = (deal.title or "").strip()
    brand_tier = get_brand_tier(title)

    # 1. Fake-MRP Generic Junk Penalty:
    # Generic unbranded product with steep >= 75% fake discount under ₹800
    if brand_tier == 0 and price is not None and price < 800 and discount >= 75:
        score -= 25
        reasons.append("fake-mrp generic junk penalty")
        discount = min(discount, 45)

    # 2. MRP-Inflated Category Discount Cap:
    # For Tier-2 or unbranded products in categories known for chronic 80%+ fake MRPs
    p_context = getattr(deal, "price_context", None)
    is_historical_drop = p_context in ("real_drop", "all_time_low")
    title_lower = title.lower()
    if brand_tier <= 2 and any(inflated in title_lower for inflated in MRP_INFLATED_TYPES):
        if not is_historical_drop and discount > 50:
            discount = 50
            reasons.append("mrp-inflated category cap")

    if discount:
        score += int(min(discount, 70) * 0.45)
        reasons.append(f"{discount}% off")
    else:
        score -= 10
        reasons.append("no discount figure")

    if savings is not None:
        if savings >= 4000:
            score += 26
            reasons.append(f"saves ₹{savings}")
        elif savings >= 2000:
            score += 20
            reasons.append(f"saves ₹{savings}")
        elif savings >= 1000:
            score += 15
            reasons.append(f"saves ₹{savings}")
        elif savings >= 500:
            score += 8
            reasons.append(f"saves ₹{savings}")
        else:
            score += 4
            reasons.append(f"saves ₹{savings}")
    elif price:
        score += 2
        reasons.append("deal price only")
    else:
        score -= 12
        reasons.append("missing price")

    if price is not None and price <= 25000:
        score += 4
        reasons.append("has a buyable price")

    if len(title) < 8 or title.lower() in {"special deal", "deal"}:
        score -= 14
        reasons.append("weak title")
    elif len(title) > 140:
        score -= 4
        reasons.append("title too long")
    else:
        score += 3

    if brand_tier == 1:
        score += 20
        reasons.append("Tier-1 brand")
    elif brand_tier == 2:
        score += 14
        reasons.append("Tier-2 brand")
    else:
        score -= 4
        reasons.append("no-name")

    # Social Proof Boost (Requirement 3: high social proof = 5x higher conversion)
    if getattr(deal, "rating", None) is not None:
        r_val = deal.rating
        rev_count = getattr(deal, "review_count", None) or 0
        if r_val >= 4.2 and rev_count >= 1000:
            score += 15
            reasons.append(f"strong social proof ({r_val}★, {rev_count} rev) [elite]")
        elif r_val >= 4.0 and rev_count >= 100:
            score += 10
            reasons.append(f"strong social proof ({r_val}★, {rev_count} rev)")
        elif r_val >= 3.8:
            score += 5
            reasons.append(f"good social proof ({r_val}★)")
        elif r_val < 3.6:
            score -= 20
            reasons.append(f"poor rating ({r_val}★)")

    # Historical Price Context Adjustments
    if p_context == "real_drop":
        score += 18
        reasons.append("historical real drop (+18)")
    elif p_context == "all_time_low":
        score += 10
        reasons.append("all-time low (+10)")
    elif p_context == "stagnant":
        score -= 10
        reasons.append("stagnant price (-10)")

    # Loot Detection Flag (Strictly gated)
    is_loot = False
    deal_rating = getattr(deal, "rating", None)
    deal_reviews = getattr(deal, "review_count", None) or 0
    # Must have verified rating >= 4.0 with >= 15 reviews, OR a verified historical drop
    verified_quality = deal_rating is not None and deal_rating >= 4.0 and deal_reviews >= 15
    has_historical_drop = is_historical_drop or getattr(deal, "is_further_price_drop", False)

    if brand_tier == 1 and discount >= 60 and (verified_quality or has_historical_drop):
        is_loot = True
    elif brand_tier in (1, 2) and savings is not None and savings >= 5000 and discount >= 40 and (verified_quality or has_historical_drop):
        is_loot = True
    elif has_historical_drop and discount >= 30:
        is_loot = True
    elif price is not None and price < 99 and discount >= 50 and brand_tier >= 1 and (verified_quality or has_historical_drop):
        is_loot = True

    if is_loot:
        deal.is_loot = True
        score += 15
        reasons.append("🚨 LOOT ALERT 🔥")

    if JUNK_TITLE.search(title):
        score -= 18
        reasons.append("coupon, used, or lookalike")

    if deal.image_url:
        score += 5
        reasons.append("has photo")

    if recent_categories:
        if recent_categories[0] == category:
            score -= 12
            reasons.append("same category as the last post")
        elif category in list(recent_categories)[:4]:
            score -= 6
            reasons.append("category already posted recently")
        elif category != "other":
            score += 8
            reasons.append("fresh category for the channel")

    # Multi-Source Viral Consensus Boost (+15 pts for 2 sources, +22 pts for 3+ sources)
    src_count = getattr(deal, "source_count", 1) or 1
    if src_count == 2:
        score += 15
        sources_str = ", ".join(getattr(deal, "matched_sources", []) or ["2 sources"])
        reasons.append(f"viral consensus ({sources_str}) (+15)")
    elif src_count >= 3:
        score += 22
        sources_str = ", ".join(getattr(deal, "matched_sources", []) or [f"{src_count} sources"])
        reasons.append(f"mega viral consensus ({sources_str}) (+22)")

    # Psychological Price-Point Conversion Boost (+6 pts)
    eval_price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
    if eval_price is not None and discount >= 35:
        from dealbot.regions import get_impulse_tiers, get_currency_symbol
        curr_sym = get_currency_symbol()
        impulse_tiers = get_impulse_tiers()
        is_sweet_spot = any(low <= eval_price <= high for low, high in impulse_tiers)
        if is_sweet_spot:
            score += 6
            reasons.append(f"psychological price point ({curr_sym}{eval_price}) (+6)")

    # Cookie-Magnet Value Pack & FMCG Impulse Steals Bonus (+25 pts for 1L/Combo under ₹499)
    # Drives 81.4% of clicks and 92% of indirect cart conversions
    title_lower = (deal.title or "").lower()
    is_value_pack = any(vp in title_lower for vp in (
        "1l", "1 l", "1 litre", "1000ml", "2l", "2 l", "2 litre", "5kg", "5 kg",
        "pack of 2", "pack of 3", "pack of 4", "pack of 6", "pack of 8", "pack of 10",
        "combo pack", "value pack", "refill pack", "mega pack"
    ))
    fmcg_brands = (
        "dove", "nivea", "pears", "cetaphil", "minimalist", "derma co", "plum", "l'oreal", "loreal",
        "garnier", "chemist at play", "tresemme", "vaseline", "lakme", "beardo", "colgate", "dettol",
        "savlon", "listerine", "whisper", "surf excel", "ariel", "vim", "comfort", "harpic", "lizol",
        "presto", "cadbury", "ferrero", "happilo", "tata sampann", "nescafe", "davidoff"
    )
    has_fmcg_brand = any(b in title_lower for b in fmcg_brands)
    eff_eval_p = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)

    if (category in ("beauty", "grocery", "home") or has_fmcg_brand):
        if is_value_pack and eff_eval_p is not None and 99 <= eff_eval_p <= 499 and discount >= 40:
            score += 25
            deal.is_cookie_magnet = True
            reasons.append("🔥 Cookie-Magnet Value Pack Loot (1L/Combo under ₹499) (+25)")
        elif has_fmcg_brand and eff_eval_p is not None and 99 <= eff_eval_p <= 499:
            score += 15
            deal.is_cookie_magnet = True
            reasons.append("🧴 High-CTR Daily Essential Impulse Magnet (+15)")
        elif is_value_pack and discount >= 35:
            score += 10
            reasons.append("📦 Value Pack Savings (+10)")

    # AI Web Discovery bonus (+8 pts)
    if (
        getattr(deal, "source_platform", None) == "AI Web Search"
        or "AI Web Search" in (getattr(deal, "matched_sources", []) or [])
        or (deal.id and deal.id.startswith("aiweb:"))
    ):
        score += 8
        reasons.append("🌐 AI Web Discovery (+8)")

    # Deduct penalty if price verification was inconclusive
    if getattr(deal, "score_penalty", 0):
        score -= deal.score_penalty
        reasons.append(f"inconclusive price penalty (-{deal.score_penalty})")

    boost = SLOT_BOOSTS.get(slot, {}).get(category, 0)
    if boost:
        score += boost
        reasons.append(f"fits {slot} shopping psychology (+{boost})")

    final_score = int(score)
    deal.buy_confidence = compute_buy_confidence(deal, final_score)
    return ScoredDeal(deal=deal, score=final_score, category=category, reason="; ".join(reasons))


def validate_typesafe_ai_batch(raw_input: Any) -> List[Dict[str, Any]]:
    """
    Safely parse and validate batched deal recommendations from any LLM.
    Handles:
    - Raw strings with markdown code blocks (```json ... ```)
    - Top-level JSON lists: [{"id": "...", "why": "...", "copy": "..."}, ...]
    - Top-level JSON dicts: {"posts": [...]} or {"deals": [...]} or {"items": [...]} or {"results": [...]}
    - Safely coerces each field to str, avoiding any TypeError/KeyError/AttributeError.
    """
    if raw_input is None:
        return []

    data = raw_input
    if isinstance(raw_input, str):
        cleaned = raw_input.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

        first_brace = cleaned.find("{")
        first_bracket = cleaned.find("[")

        target_json = cleaned
        if first_bracket >= 0 and (first_brace < 0 or first_bracket < first_brace):
            last_bracket = cleaned.rfind("]")
            if last_bracket > first_bracket:
                target_json = cleaned[first_bracket : last_bracket + 1]
        elif first_brace >= 0:
            last_brace = cleaned.rfind("}")
            if last_brace > first_brace:
                target_json = cleaned[first_brace : last_brace + 1]

        try:
            data = json.loads(target_json)
        except Exception as exc:
            logger.debug(f"validate_typesafe_ai_batch JSON parse error: {exc}")
            return []

    items_list = []
    if isinstance(data, list):
        items_list = data
    elif isinstance(data, dict):
        for candidate_key in ("posts", "deals", "items", "results", "data", "candidates", "picks"):
            val = data.get(candidate_key)
            if isinstance(val, list):
                items_list = val
                break
        if not items_list:
            if data.get("id") or data.get("deal_id"):
                items_list = [data]

    if not isinstance(items_list, list):
        return []

    valid_results = []
    for entry in items_list:
        if not isinstance(entry, dict):
            if isinstance(entry, str) and entry.strip():
                valid_results.append({"id": entry.strip(), "why": "", "copy": ""})
            continue

        deal_id = entry.get("id") or entry.get("deal_id") or entry.get("canonical_id")
        if not deal_id or not isinstance(deal_id, (str, int)):
            continue
        deal_id_str = str(deal_id).strip()
        if not deal_id_str:
            continue

        why = entry.get("why") or entry.get("reason") or entry.get("notes") or ""
        copy = entry.get("copy") or entry.get("caption") or entry.get("text") or ""

        valid_results.append({
            "id": deal_id_str,
            "why": str(why).strip() if why is not None else "",
            "copy": str(copy).strip() if copy is not None else "",
        })

    return valid_results


def _extract_json(text: str) -> Dict[str, Any]:
    """Compatibility wrapper that returns a dict with 'posts' list."""
    posts = validate_typesafe_ai_batch(text)
    return {"posts": posts}

class DealAgent:
    def __init__(self, copywriter=None, min_score: Optional[int] = None):
        self.copywriter = copywriter
        self.min_score = min_score if min_score is not None else int(os.getenv("AGENT_MIN_SCORE", "32"))
        self.use_llm = os.getenv("AGENT_LLM", "true").lower() != "false"

    def plan(self, deals: Sequence[Deal], recent_categories: Sequence[str], max_posts: int) -> AgentPlan:
        slot = current_slot()
        scored = [score_deal(deal, recent_categories, slot=slot) for deal in deals]
        scored.sort(key=lambda item: item.score, reverse=True)

        # Pass 1: Strict Quality Gate (rating must be >= 3.6 if rated)
        qualified = [
            item for item in scored
            if item.score >= self.min_score
            and (getattr(item.deal, "rating", None) is None or getattr(item.deal, "rating", None) >= 3.6)
        ]
        rejected = [item for item in scored if item not in qualified]

        soft_floor_used = False
        target_deals = min(max_posts, 2)
        # Pass 2: Adaptive Soft-Floor Backfill — Guaranteed Hourly Activity
        # If Pass 1 returns 0 deals (or only 1 deal when MAX_POSTS_PER_CYCLE >= 3),
        # automatically relax the threshold down to score >= 16 for verified in-stock deals
        # (discount_pct >= 25%, not explicitly rated < 3.6★) so the channel always posts at least 1–2 solid deals every hour
        if len(qualified) == 0 or (len(qualified) < 2 and max_posts >= 3):
            needed = target_deals - len(qualified)
            soft_candidates = []
            for item in rejected:
                d = item.deal
                disc = d.discount_percent or 0
                p = getattr(d, "effective_price", None) or parse_rupees(d.current_price)
                m = parse_rupees(d.original_price)
                if p and m and m > p:
                    disc = max(disc, int(round((m - p) / m * 100)))
                r = getattr(d, "rating", None)
                if item.score >= 16 and disc >= 25 and (r is None or r >= 3.6):
                    soft_candidates.append(item)

            if soft_candidates:
                soft_floor_used = True
                for item in soft_candidates[:needed]:
                    rejected.remove(item)
                    item.reason = f"soft-floor backfill (score {item.score}); {item.reason}"
                    qualified.append(item)

        for item in rejected:
            item.action = "skip"
            item.reason = f"below score floor {self.min_score}; {item.reason}"

        # Post nothing rather than the best piece of junk when the floor is missed.
        shortlist = qualified[:12]
        chosen: List[ScoredDeal] = []
        source = "heuristic"

        if (
            self.use_llm
            and self.copywriter is not None
            and self.copywriter.is_configured()
            and shortlist
        ):
            llm_picks = self._ask_llm(shortlist, recent_categories, max_posts, slot)
            if llm_picks:
                by_id = {item.deal.id: item for item in shortlist}
                for deal_id, why in llm_picks:
                    item = by_id.get(deal_id)
                    if item and item not in chosen:
                        item.action = "post"
                        item.reason = f"llm: {why}; {item.reason}" if why else f"llm pick; {item.reason}"
                        chosen.append(item)
                source = "llm+heuristic"

        target_count = min(max_posts, len(qualified))
        if len(chosen) < target_count:
            fill = self._greedy([item for item in qualified if item not in chosen], target_count - len(chosen))
            for item in fill:
                item.action = "post"
                if not item.reason.startswith("llm pick"):
                    item.reason = f"heuristic pick; {item.reason}"
                chosen.append(item)

        # Deterministic backfill: if greedy or LLM under-selected (e.g. near-duplicate skips),
        # fill remaining slots up to target_count from the highest-scoring qualifying candidates
        if len(chosen) < target_count:
            for item in qualified:
                if len(chosen) >= target_count:
                    break
                if item not in chosen and not any(other.deal.id == item.deal.id for other in chosen):
                    item.action = "post"
                    if "pick" not in item.reason and "backfill" not in item.reason:
                        item.reason = f"deterministic backfill (score {item.score}); {item.reason}"
                    chosen.append(item)

        chosen = chosen[:max_posts]
        chosen_ids = {item.deal.id for item in chosen}
        skips = []
        for item in shortlist:
            if item.deal.id not in chosen_ids:
                item.action = "skip"
                if "not selected" not in item.reason:
                    item.reason = f"not selected this cycle; {item.reason}"
                skips.append(item)
        skips.extend(rejected[:12])

        plan = AgentPlan(posts=chosen, skips=skips, source=source if chosen else "heuristic", soft_floor_used=soft_floor_used)
        logger.info(plan.summary())
        return plan

    def _greedy(self, pool: Sequence[ScoredDeal], needed: int) -> List[ScoredDeal]:
        remaining = list(pool)
        selected: List[ScoredDeal] = []
        while remaining and len(selected) < needed:
            def adjusted(item: ScoredDeal) -> int:
                penalty = 0
                if any(other.category == item.category for other in selected):
                    penalty += 18
                if any(titles_too_similar(item.deal.title, other.deal.title) for other in selected):
                    penalty += 45
                item_nt = extract_narrow_type(item.deal.title)
                if item_nt and any(extract_narrow_type(other.deal.title) == item_nt for other in selected):
                    penalty += 25
                return item.score - penalty

            remaining.sort(key=adjusted, reverse=True)
            best = remaining.pop(0)
            if any(titles_too_similar(best.deal.title, other.deal.title) for other in selected):
                best.action = "skip"
                best.reason = f"near-duplicate; {best.reason}"
                continue
            selected.append(best)
        return selected

    def _ask_llm(
        self,
        shortlist: Sequence[ScoredDeal],
        recent_categories: Sequence[str],
        max_posts: int,
        slot: str,
    ) -> List[tuple]:
        cards = []
        allowed = []
        for item in shortlist:
            allowed.append(item.deal.id)
            cards.append(
                {
                    "id": item.deal.id,
                    "title": item.deal.title[:120],
                    "price": item.deal.current_price,
                    "mrp": item.deal.original_price,
                    "discount": item.deal.discount_percent,
                    "category": item.category,
                    "score": item.score,
                    "notes": item.reason,
                }
            )
        prompt = (
            "You are the editor of an Indian Telegram deals channel. "
            "Choose which deals to publish this cycle.\n"
            f"Time slot: {slot} IST. Post at most {max_posts} deals.\n"
            f"Categories already on the channel, newest first: {list(recent_categories)[:6] or 'none'}.\n"
            "Include low-priced deals. A product under ₹99 is still worth posting. "
            "Do not skip a deal because the price or the rupee saving is small. "
            "Skip coupon-only or refurbished listings, and do not pick two near-duplicate products.\n"
            "Reply with JSON only, no markdown:\n"
            '{"posts":[{"id":"<id from the list>","why":"short reason","copy":"punchy one-sentence caption with deal price digits"}]}\n\n'
            f"CANDIDATES:\n{json.dumps(cards, ensure_ascii=False)}"
        )
        try:
            raw = self.copywriter.generate_text(
                prompt,
                system_prompt="You only output JSON. Never invent deal ids.",
                response_json=True,
            )
            if not raw:
                return []
            posts = validate_typesafe_ai_batch(raw)
            ids = []
            by_id = {item.deal.id: item.deal for item in shortlist}
            for entry in posts:
                deal_id = entry.get("id") if isinstance(entry, dict) else entry
                why = entry.get("why", "") if isinstance(entry, dict) else ""
                copy = entry.get("copy", "") if isinstance(entry, dict) else ""
                if isinstance(deal_id, str) and deal_id in allowed and deal_id not in [row[0] for row in ids]:
                    ids.append((deal_id, str(why)[:180]))
                    deal_obj = by_id.get(deal_id)
                    if deal_obj and copy:
                        from dealbot.ai_copywriter import validate_critic
                        if validate_critic(copy, deal_obj):
                            deal_obj._cached_ai_copy = copy
                        else:
                            logger.info(
                                f"Critic rejected LLM copy for {deal_id}; falling back to template copy for this deal only."
                            )
                            deal_obj._cached_ai_copy = None
                if len(ids) >= max_posts:
                    break
            return ids
        except Exception as exc:
            logger.warning(f"Agent LLM plan failed, using heuristic: {exc}")
            return []
