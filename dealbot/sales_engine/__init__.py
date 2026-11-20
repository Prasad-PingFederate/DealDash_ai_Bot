# dealbot/sales_engine/__init__.py
"""High-Conversion Sales & Conversion Engine.

Modules:
1. bundles: Curated impulse buy bundles (Under ₹99 / ₹199 / ₹299 / ₹499) to activate the 24h cookie attribution window.
2. bank_offers: Automated Indian bank & UPI discount calculator displaying bold net checkout prices.
3. whatsapp_broadcaster: Clean mobile WhatsApp channel broadcaster with rich formatting.
4. twitter_bot: Automated Twitter/X viral loot deals bot with hashtag optimization.
"""
from dealbot.sales_engine.bank_offers import (
    BankOfferResult,
    calculate_bank_offer,
    enrich_deal_with_bank_offer,
    format_bank_offer_callout,
)
from dealbot.sales_engine.bundles import (
    CuratedBundle,
    format_bundle_telegram_message,
    format_bundle_whatsapp_message,
    generate_budget_bundle,
)
from dealbot.sales_engine.twitter_bot import (
    format_x_tweet,
    post_to_twitter,
)
from dealbot.sales_engine.whatsapp_broadcaster import (
    broadcast_loot_to_whatsapp,
    format_whatsapp_deal,
    send_whatsapp_message,
)

__all__ = [
    "BankOfferResult",
    "calculate_bank_offer",
    "enrich_deal_with_bank_offer",
    "format_bank_offer_callout",
    "CuratedBundle",
    "generate_budget_bundle",
    "format_bundle_telegram_message",
    "format_bundle_whatsapp_message",
    "format_whatsapp_deal",
    "send_whatsapp_message",
    "broadcast_loot_to_whatsapp",
    "format_x_tweet",
    "post_to_twitter",
]
