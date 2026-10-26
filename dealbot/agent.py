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

from dealbot.models import Deal
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


def score_deal(deal: Deal, recent_categories: Sequence[str], slot: Optional[str] = None) -> ScoredDeal:
    slot = slot or current_slot()
    category = classify_category(deal)
    reasons: List[str] = []
    score = 18

    discount = int(deal.discount_percent or 0)
    if discount:
        score += int(min(discount, 75) * 0.65)
        reasons.append(f"{discount}% off")
    else:
        reasons.append("no discount figure")
        score -= 8

    if deal.current_price and deal.original_price:
        score += 10
        reasons.append("price and MRP shown")
    elif deal.current_price:
        score += 4
        reasons.append("deal price only")
    else:
        score -= 6
        reasons.append("missing price")

    if deal.image_url:
        score += 6
        reasons.append("has photo")

    title = (deal.title or "").strip()
    if len(title) < 8 or title.lower() in {"special deal", "deal"}:
        score -= 12
        reasons.append("weak title")
    elif len(title) > 140:
        score -= 4
        reasons.append("title too long")
    else:
        score += 4

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
        self.min_score = min_score if min_score is not None else int(os.getenv("AGENT_MIN_SCORE", "32"))
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

        shortlist = (qualified or scored)[:12]
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
            "Prefer a real discount, a clear price, and a mix of categories. "
            "Do not pick two near-duplicate products. Skip weak or repetitive deals.\n"
            "Reply with JSON only, no markdown:\n"
            '{"posts":[{"id":"<id from the list>","why":"short reason"}]}\n\n'
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
            for entry in posts:
                deal_id = entry.get("id") if isinstance(entry, dict) else entry
                why = entry.get("why", "") if isinstance(entry, dict) else ""
                if isinstance(deal_id, str) and deal_id in allowed and deal_id not in [row[0] for row in ids]:
                    ids.append((deal_id, str(why)[:180]))
                if len(ids) >= max_posts:
                    break
            return ids
        except Exception as exc:
            logger.warning(f"Agent LLM plan failed, using heuristic: {exc}")
            return []
