# dealbot/sales_engine/whatsapp_broadcaster.py
"""WhatsApp Channels & Webhook Broadcaster for High-Converting Loot Deals.

WhatsApp boasts 4x higher open rates and instant push engagement in India.
Supports:
1. Meta Cloud API (Official WhatsApp Business Cloud API)
2. WhatsApp Webhook / Evolution API / Custom Bot Gateway
3. Automatic graceful fallback when credentials are not configured.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests

from dealbot.models import Deal, parse_rupees
from dealbot.monetizer import monetize_url
from dealbot.sales_engine.bank_offers import calculate_bank_offer

logger = logging.getLogger(__name__)


def format_whatsapp_deal(deal: Deal) -> str:
    """Format deal text using WhatsApp markdown (*bold*, ~strike~, _italics_)."""
    retailer = deal.retailer or "Amazon"
    is_loot = getattr(deal, "is_loot", False) or (deal.discount_percent or 0) >= 50
    badge = "🚨 *LOOT ALERT / PRICE DROP*" if is_loot else "⚡ *HOT DEAL*"

    price = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
    mrp = parse_rupees(deal.original_price)
    disc = deal.discount_percent or (int(round((mrp - price) / mrp * 100)) if mrp and price and mrp > price else None)

    title = (deal.title or "Loot Deal").strip()
    if len(title) > 90:
        title = title[:87] + "..."

    lines = [
        f"{badge} · {retailer}",
        f"*{title}*",
        "",
    ]

    price_str = f"₹{price:,}" if price else ""
    if price_str:
        disc_str = f" · *{disc}% OFF*" if disc else ""
        lines.append(f"💰 Price: *{price_str}*{disc_str}")

    if mrp and price and mrp > price:
        savings = mrp - price
        lines.append(f"MRP: ~₹{mrp:,}~ (Save ₹{savings:,})")

    # Bank Offer callout
    bank_cand = getattr(deal, "bank_offer", None) or getattr(deal, "description", "")
    if bank_cand and price:
        bank_res = calculate_bank_offer(price, bank_cand)
        if bank_res:
            lines.append(f"⚡ *Net Checkout Price: ₹{bank_res.net_price:,}* (Extra ₹{bank_res.discount_amount:,} off with {bank_res.bank_name})")

    # Coupon / Promo Code
    coupon = getattr(deal, "coupon_code", None) or getattr(deal, "promo_code", None)
    if coupon:
        lines.append(f"🎟️ Coupon: *{coupon}* (Apply at checkout)")

    lines.append("")

    # Direct Affiliate URL with WhatsApp attribution SubID
    raw_url = deal.url or ""
    res = monetize_url(raw_url, channel="wa", category=getattr(deal, "category", "gen"), deal_id=deal.id)
    monetized = res.monetized_url or raw_url
    lines.append(f"🛒 *Buy on {retailer}:*")
    lines.append(monetized)

    # BharatBazaro Product / Comparison Link
    clean_id = deal.canonical_product_id or deal.id
    if clean_id.startswith("amz-"):
        clean_id = clean_id.replace("amz-", "")
    lines.append("")
    lines.append("📊 *Price History & Alternatives:*")
    lines.append(f"https://bharatbazaro.com/deal/{clean_id}")

    lines.append("")
    lines.append("_Forward to your friends before price resets!_")
    lines.append("_#ad Affiliate Link._")

    return "\n".join(lines)


def send_whatsapp_message(
    text: str,
    image_url: Optional[str] = None,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """Dispatch message to WhatsApp via configured channel/webhook/API."""
    if dry_run:
        logger.info(f"[WhatsApp DRY RUN] Would broadcast:\n{text[:160]}...")
        return True, "dry_run_success"

    # 1. Check Meta Cloud API
    token = os.getenv("WHATSAPP_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    recipient = os.getenv("WHATSAPP_RECIPIENT_ID") or os.getenv("WHATSAPP_CHANNEL_ID")

    # 2. Check Generic Webhook
    webhook_url = os.getenv("WHATSAPP_WEBHOOK_URL")

    if not token and not webhook_url:
        logger.debug("[WhatsApp] Neither WHATSAPP_TOKEN nor WHATSAPP_WEBHOOK_URL configured. Skipping.")
        return False, "unconfigured"

    # Meta Cloud API Execution
    if token and phone_id and recipient:
        api_url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if image_url:
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "image",
                "image": {"link": image_url, "caption": text},
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": text},
            }

        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=12)
            if resp.status_code in (200, 201):
                logger.info(f"[WhatsApp] Successfully broadcasted to {recipient}")
                return True, "meta_api_success"
            logger.warning(f"[WhatsApp] Meta API returned {resp.status_code}: {resp.text}")
            return False, f"http_{resp.status_code}"
        except Exception as e:
            logger.warning(f"[WhatsApp] Meta API exception: {e}")
            return False, str(e)

    # Webhook Gateway Execution
    if webhook_url:
        payload = {
            "message": text,
            "image": image_url,
            "channel": recipient,
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 201, 202, 204):
                logger.info("[WhatsApp] Successfully dispatched to webhook")
                return True, "webhook_success"
            return False, f"webhook_http_{resp.status_code}"
        except Exception as e:
            logger.warning(f"[WhatsApp] Webhook exception: {e}")
            return False, str(e)

    return False, "missing_recipient"


def broadcast_loot_to_whatsapp(
    deal: Deal,
    min_discount: int = 40,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """Broadcast high-discount Loot deal to WhatsApp."""
    disc = deal.discount_percent or 0
    is_loot = getattr(deal, "is_loot", False)

    if disc < min_discount and not is_loot:
        return False, f"discount_{disc}_below_threshold_{min_discount}"

    msg_text = format_whatsapp_deal(deal)
    image_url = getattr(deal, "image_url", None)

    return send_whatsapp_message(text=msg_text, image_url=image_url, dry_run=dry_run)
