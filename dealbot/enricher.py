"""
Deal enrichment utilities for brand, category, and metadata resolution.
"""
from typing import Optional, Union
from dealbot.models import Deal
from dealbot.agent import classify_category
from dealbot.web_exporter import detect_brand, map_ui_category


def detect_category(deal_or_title: Union[Deal, str]) -> str:
    """Classify category from a Deal object or raw title string."""
    if isinstance(deal_or_title, Deal):
        return classify_category(deal_or_title)
    deal = Deal(id="temp", retailer="Amazon", title=str(deal_or_title or ""))
    return classify_category(deal)


__all__ = ["detect_brand", "detect_category", "map_ui_category"]

