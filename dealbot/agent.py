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
        "headphone", "earbud", "earphone", "speaker", "watch", "phone", "charger",
        "laptop", "keyboard", "mouse", "camera", "trimmer", "tv", "monitor",
        "power bank", "bluetooth", "ssd", "router", "printer",
    ),
    "home": (
        "mixer", "grinder", "cooker", "fan", "vacuum", "kettle", "iron", "bottle",
        "bedsheet", "blanket", "cookware", "air fryer", "induction", "lamp",
        "storage", "pressure cooker",
    ),
    "fashion": (
        "shoe", "shirt", "jeans", "saree", "kurta", "t-shirt", "tshirt", "dress",
        "jacket", "sandal", "sneaker", "handbag", "wallet",
    ),
    "beauty": (
        "cream", "shampoo", "lipstick", "serum", "perfume", "face wash", "sunscreen",
        "moisturizer", "conditioner",
    ),
    "grocery": (
        "oil", "rice", "atta", "snack", "coffee", "tea", "ghee", "spice", "biscuit",
    ),
    "kids": ("toy", "diaper", "kids", "baby", "school bag"),
    "books": ("book", "novel", "kindle"),
}

SLOT_BOOSTS = {
    "morning": {"home": 8, "grocery": 8, "beauty": 4},
    "afternoon": {"electronics": 4, "fashion": 4, "home": 4},
    "evening": {"electronics": 10, "fashion": 8, "beauty": 4},
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
)

TIER_2_BRANDS = (
    "boat", "noise", "boult", "fire-boltt", "fireboltt", "ptron", "realme", "xiaomi",
    "redmi", "poco", "iqoo", "vivo", "oppo", "tecno", "infinix", "zebronics", "portronics",
    "ambrane", "lifelong", "agaro", "wipro", "syska", "solimo", "amazon basics",
    "amazfit", "garmin", "mamaearth", "fire tv", "alexa",
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
    if 17 <= hour <= 22:
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
        elif savings >= 1500:
            score += 18
            reasons.append(f"saves ₹{savings}")
        elif savings >= 600:
            score += 10
            reasons.append(f"saves ₹{savings}")
        elif savings >= 250:
            score += 4
            reasons.append(f"saves ₹{savings}")
        else:
            score += 6
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

    # Social Proof Boost
    if getattr(deal, "rating", None) is not None:
        r_val = deal.rating
        rev_count = getattr(deal, "review_count", None) or 0
        if r_val >= 4.2 and rev_count >= 500:
            score += 10
            reasons.append(f"strong social proof ({r_val}★, {rev_count} rev)")
        elif r_val >= 4.0 and rev_count >= 50:
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

    boost = SLOT_BOOSTS.get(slot, {}).get(category, 0)
    if boost:
        score += boost
        reasons.append(f"fits the {slot} slot")

    return ScoredDeal(deal=deal, score=int(score), category=category, reason="; ".join(reasons))


def _extract_json(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("json was not an object")
    return data


class DealAgent:
    def __init__(self, copywriter=None, min_score: Optional[int] = None):
        self.copywriter = copywriter
        self.min_score = min_score if min_score is not None else int(os.getenv("AGENT_MIN_SCORE", "40"))
        self.use_llm = os.getenv("AGENT_LLM", "true").lower() != "false"

    def plan(self, deals: Sequence[Deal], recent_categories: Sequence[str], max_posts: int) -> AgentPlan:
        slot = current_slot()
        scored = [score_deal(deal, recent_categories, slot=slot) for deal in deals]
        scored.sort(key=lambda item: item.score, reverse=True)

        qualified = [item for item in scored if item.score >= self.min_score]
        rejected = [item for item in scored if item.score < self.min_score]
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

        if len(chosen) < max_posts:
            fill = self._greedy([item for item in shortlist if item not in chosen], max_posts - len(chosen))
            for item in fill:
                item.action = "post"
                if not item.reason.startswith("llm pick"):
                    item.reason = f"heuristic pick; {item.reason}"
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

        plan = AgentPlan(posts=chosen, skips=skips, source=source if chosen else "heuristic")
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
            )
            if not raw:
                return []
            data = _extract_json(raw)
            posts = data.get("posts") or []
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
