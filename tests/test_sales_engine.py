# tests/test_sales_engine.py
"""Comprehensive unit tests for the High-Conversion Sales & Conversion Engine:
1. Automated Bank & UPI Offer Calculator (percentage with cap, flat, and UPI cashback).
2. Curated Budget Loot Bundle generator (cart fillers under ₹99 / ₹199 / ₹299 / ₹499).
3. WhatsApp Channels Broadcaster & formatting.
4. Twitter / X deals bot (280-char enforcement, hashtags, tracking links).
"""
import os
from unittest.mock import MagicMock, patch
import pytest

from dealbot.models import Deal
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
from dealbot.sales_engine.whatsapp_broadcaster import (
    broadcast_loot_to_whatsapp,
    format_whatsapp_deal,
    send_whatsapp_message,
)
from dealbot.sales_engine.twitter_bot import (
    format_x_tweet,
    post_to_twitter,
)


# ==============================================================================
# 1. Bank & UPI Offer Calculator Tests
# ==============================================================================

def test_bank_offer_calculator_percentage_with_cap():
    # Example: 10% instant discount on SBI Card up to ₹1,500 on ₹14,999 item
    res = calculate_bank_offer(14999, "10% Instant Discount on SBI Credit Card up to ₹1,500")
    assert res is not None
    assert res.bank_name == "SBI Card"
    assert res.discount_amount == 1499 or res.discount_amount == 1500  # 10% is 1499.9, capped at 1500
    assert res.net_price == 14999 - res.discount_amount
    assert "⚡ Net Checkout Price:" in res.callout_text
    assert "SBI Card" in res.callout_text


def test_bank_offer_calculator_flat_and_upi():
    # Flat discount
    flat_res = calculate_bank_offer(4999, "Flat ₹500 off on HDFC Bank Credit Card")
    assert flat_res is not None
    assert flat_res.bank_name == "HDFC Bank Card"
    assert flat_res.discount_amount == 500
    assert flat_res.net_price == 4499

    # UPI Cashback
    upi_res = calculate_bank_offer(799, "Flat ₹50 Cashback on Amazon Pay UPI")
    assert upi_res is not None
    assert "UPI" in upi_res.bank_name
    assert upi_res.discount_amount == 50
    assert upi_res.net_price == 749


def test_enrich_deal_with_bank_offer():
    deal = Deal(
        id="amz-test1",
        retailer="Amazon",
        title="Samsung Galaxy M15 5G",
        url="https://www.amazon.in/dp/B0CX234567",
        current_price="₹12,999",
        original_price="₹15,999",
        bank_offer="10% Instant Discount on ICICI Bank Cards up to ₹1,000",
    )
    enriched = enrich_deal_with_bank_offer(deal)
    assert enriched.bank_discount_amount == 1000
    assert enriched.effective_price == 11999
    callout = format_bank_offer_callout(enriched, html_mode=True)
    assert "⚡ Net Checkout Price: ₹11,999" in callout


# ==============================================================================
# 2. Curated Budget Loot Bundle Tests
# ==============================================================================

def test_generate_budget_bundle():
    deals = [
        Deal(
            id="amz-cable",
            retailer="Amazon",
            title="boAt Rugged v3 Micro USB Cable (1.5 Meter)",
            url="https://www.amazon.in/dp/B0B1234567",
            current_price="₹149",
            original_price="₹499",
            discount_percent=70,
            category="Cables",
        ),
        Deal(
            id="amz-tea",
            retailer="Amazon",
            title="Tata Tea Gold 250g Pack",
            url="https://www.amazon.in/dp/B0B7654321",
            current_price="₹110",
            original_price="₹170",
            discount_percent=35,
            category="Grocery",
        ),
        Deal(
            id="amz-soap",
            retailer="Amazon",
            title="Dettol Original Soap (Pack of 3)",
            url="https://www.amazon.in/dp/B0B9999999",
            current_price="₹95",
            original_price="₹150",
            discount_percent=36,
            category="Personal Care",
        ),
        Deal(
            id="amz-laptop",
            retailer="Amazon",
            title="HP Pavilion Gaming Laptop",
            url="https://www.amazon.in/dp/B0B8888888",
            current_price="₹65,000",
            original_price="₹85,000",
            discount_percent=23,
            category="Laptops",
        ),
    ]

    bundle = generate_budget_bundle(deals, max_price=199, count=5)
    assert bundle is not None
    assert bundle.max_price == 199
    assert len(bundle.deals) == 3  # Laptop excluded (> ₹199)
    assert "https://bharatbazaro.com/collections/under-199" in bundle.collection_url

    # Check Telegram and WhatsApp formatting
    tg_msg = format_bundle_telegram_message(bundle)
    assert "Today's Top 3 Deals Under ₹199" in tg_msg
    assert "boAt Rugged" in tg_msg
    assert "Tata Tea" in tg_msg
    assert "https://bharatbazaro.com/collections/under-199" in tg_msg

    wa_msg = format_bundle_whatsapp_message(bundle)
    assert "Today's Top 3 Deals Under ₹199" in wa_msg
    assert "• 🛒 Link:" in wa_msg


# ==============================================================================
# 3. WhatsApp Broadcaster Tests
# ==============================================================================

def test_format_whatsapp_deal_and_broadcast():
    deal = Deal(
        id="amz-loot",
        retailer="Amazon",
        title="Philips MultiGroom All-in-One Trimmer",
        url="https://www.amazon.in/dp/B08XYZ1234",
        current_price="₹899",
        original_price="₹1,995",
        discount_percent=55,
        is_loot=True,
        bank_offer="Flat ₹100 Cashback on Amazon Pay UPI",
    )

    wa_text = format_whatsapp_deal(deal)
    assert "🚨 *LOOT ALERT / PRICE DROP*" in wa_text
    assert "*₹899*" in wa_text
    assert "Save ₹1,096" in wa_text
    assert "⚡ *Net Checkout Price: ₹799*" in wa_text
    assert "https://bharatbazaro.com/deal/" in wa_text

    # Dry run broadcast
    ok, reason = broadcast_loot_to_whatsapp(deal, dry_run=True)
    assert ok is True
    assert reason == "dry_run_success"

    # Unconfigured broadcast returns false gracefully
    with patch.dict(os.environ, {"WHATSAPP_TOKEN": "", "WHATSAPP_WEBHOOK_URL": ""}):
        ok_unconf, reason_unconf = broadcast_loot_to_whatsapp(deal, dry_run=False)
        assert ok_unconf is False
        assert reason_unconf == "unconfigured"


# ==============================================================================
# 4. Twitter / X Bot Tests
# ==============================================================================

def test_format_x_tweet_and_post():
    deal = Deal(
        id="amz-earbuds",
        retailer="Amazon",
        title="boAt Airdopes 141 ANC True Wireless Earbuds with 42H Playtime and Low Latency Beast Mode",
        url="https://www.amazon.in/dp/B0B1234567",
        current_price="₹999",
        original_price="₹4,490",
        discount_percent=78,
    )

    tweet = format_x_tweet(deal)
    assert len(tweet) <= 280
    assert "🚨 LOOT DEAL ALERT" in tweet
    assert "₹999" in tweet
    assert "#LootDeal" in tweet
    assert "https://bharatbazaro.com/deal/" in tweet

    # Dry run post
    ok, reason = post_to_twitter(deal, dry_run=True)
    assert ok is True
    assert reason == "dry_run_success"

    # Unconfigured post returns false gracefully
    with patch.dict(os.environ, {"TWITTER_API_KEY": "", "TWITTER_BEARER_TOKEN": ""}):
        ok_unconf, reason_unconf = post_to_twitter(deal, dry_run=False)
        assert ok_unconf is False
        assert reason_unconf == "unconfigured"
