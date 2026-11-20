# dealbot/sales_engine/bundles.py
"""Automated Daily Budget Loot & Cookie Activator Bundle Generator.

Impulse buys under ₹99 / ₹199 / ₹299 convert at 10x the rate of big electronics
and instantly activate the 24-hour Amazon/Flipkart affiliate cookie window.
"""
from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# High-Conversion Impulse Categories for Cart Fillers
PRIORITY_KEYWORDS = [
    "cable", "adapter", "charger", "otg", "earphone", "biscuit", "tea", "coffee",
    "soap", "shampoo", "toothpaste", "pen", "notebook", "organizer", "bottle",
    "cloth", "sponge", "kitchen", "knife", "scrubber", "masala", "dry fruit",
    "snack", "socks", "towel", "cover", "protector"
]


@dataclass
class CuratedBundle:
    max_price: int
    title: str
    deals: List[Deal] = field(default_factory=list)
    collection_url: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(IST).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_price": self.max_price,
            "title": self.title,
            "deals_count": len(self.deals),
            "collection_url": self.collection_url,
            "created_at": self.created_at,
            "deal_ids": [d.id for d in self.deals],
        }


def _score_bundle_candidate(deal: Deal, max_price: int) -> float:
    """Score candidate deal for impulse conversion suitability."""
    price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
    if not price or price > max_price or price < 15:
        return -1.0

    score = 50.0

    # Discount depth score
    disc = deal.discount_percent or 0
    score += min(disc, 80) * 0.5

    # Priority category bonus
    title_lower = (deal.title or "").lower()
    cat_lower = (deal.category or "").lower()
    combined = f"{title_lower} {cat_lower}"

    if any(kw in combined for kw in PRIORITY_KEYWORDS):
        score += 25.0

    # Rating bonus
    if getattr(deal, "rating", None) and deal.rating >= 4.0:
        score += 10.0

    # Cheaper items convert faster (elasticity)
    if price <= 99:
        score += 15.0
    elif price <= 199:
        score += 10.0

    return score


def generate_budget_bundle(
    deals: List[Deal],
    max_price: int = 199,
    count: int = 5,
    collection_slug: Optional[str] = None,
) -> Optional[CuratedBundle]:
    """Curate top N impulse deals under max_price for a single bundle post."""
    if not deals:
        return None

    candidates: List[Tuple[Deal, float]] = []
    seen_products = set()

    for d in deals:
        # Check stock / expiry
        if getattr(d, "is_expired", lambda: False)():
            continue

        pid = getattr(d, "canonical_product_id", None) or d.id
        if pid in seen_products:
            continue

        score = _score_bundle_candidate(d, max_price)
        if score > 0:
            candidates.append((d, score))
            seen_products.add(pid)

    if not candidates:
        return None

    # Sort descending by impulse conversion score
    candidates.sort(key=lambda x: x[1], reverse=True)
    selected_deals = [c[0] for c in candidates[:count]]

    if len(selected_deals) < 2:
        return None  # Need at least 2 deals to make a valid bundle

    slug = collection_slug or f"under-{max_price}"
    collection_url = f"https://bharatbazaro.com/collections/{slug}"

    title = f"🔥 Today's Top {len(selected_deals)} Deals Under ₹{max_price} (Free Delivery Eligible)"

    return CuratedBundle(
        max_price=max_price,
        title=title,
        deals=selected_deals,
        collection_url=collection_url,
    )


def format_bundle_telegram_message(bundle: CuratedBundle) -> str:
    """Format slick Telegram post combining 3-5 cart filler deals into one message."""
    lines = [
        f"<b>{bundle.title}</b>",
        "<i>🛒 Cart Fillers & Impulse Steals — Activates Free Delivery!</i>",
        "",
    ]

    for idx, deal in enumerate(bundle.deals, start=1):
        price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
        mrp = parse_rupees(deal.original_price)
        disc = deal.discount_percent or (int(round((mrp - price) / mrp * 100)) if mrp and price and mrp > price else None)

        title_short = (deal.title or "Loot Deal").strip()
        if len(title_short) > 65:
            title_short = title_short[:62] + "..."

        aff_url = getattr(deal, "url", "#")
        # Ensure SubID has bundle attribution
        if "ascsubtag=" not in aff_url:
            res = monetize_url(aff_url, channel="bundle", category=getattr(deal, "category", "bundle"), deal_id=deal.id)
            if res.monetized_url:
                aff_url = res.monetized_url

        price_str = f"₹{price:,}" if price else "Loot Price"
        mrp_str = f" <s>₹{mrp:,}</s>" if mrp and price and mrp > price else ""
        disc_str = f" · <b>{disc}% OFF</b>" if disc else ""

        lines.append(f"<b>{idx}. {html.escape(title_short, quote=False)}</b>")
        lines.append(f"💰 <b>{price_str}</b>{mrp_str}{disc_str}")
        lines.append(f"🛒 <a href=\"{aff_url}\"><b>Buy on {html.escape(deal.retailer or 'Amazon')}</b></a>")
        lines.append("")

    lines.append(f"👉 <b>Explore 100+ Deals Under ₹{bundle.max_price}:</b>")
    lines.append(f"<a href=\"{bundle.collection_url}\"><b>View Full BharatBazaro Under-₹{bundle.max_price} Collection</b></a>")
    lines.append("")
    lines.append("<i>#ad Affiliate links. Prices & availability accurate at post time.</i>")

    return "\n".join(lines)


def format_bundle_whatsapp_message(bundle: CuratedBundle) -> str:
    """Format clean WhatsApp post combining cart filler deals."""
    lines = [
        f"*{bundle.title}*",
        "_🛒 Cart Fillers & Impulse Steals — Activates Free Delivery!_",
        "",
    ]

    for idx, deal in enumerate(bundle.deals, start=1):
        price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
        mrp = parse_rupees(deal.original_price)
        disc = deal.discount_percent

        title_short = (deal.title or "Loot Deal").strip()
        if len(title_short) > 60:
            title_short = title_short[:57] + "..."

        aff_url = getattr(deal, "url", "#")
        if "ascsubtag=" not in aff_url:
            res = monetize_url(aff_url, channel="wa_bundle", category=getattr(deal, "category", "bundle"), deal_id=deal.id)
            if res.monetized_url:
                aff_url = res.monetized_url

        price_str = f"₹{price:,}" if price else "Loot"
        mrp_str = f" ~₹{mrp:,}~" if mrp and price and mrp > price else ""
        disc_str = f" ({disc}% OFF)" if disc else ""

        lines.append(f"{idx}. *{title_short}*")
        lines.append(f"• *{price_str}*{mrp_str}{disc_str}")
        lines.append(f"• 🛒 Link: {aff_url}")
        lines.append("")

    lines.append(f"👉 *Explore All Under ₹{bundle.max_price}:*")
    lines.append(bundle.collection_url)

    return "\n".join(lines)
