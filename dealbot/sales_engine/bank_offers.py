# dealbot/sales_engine/bank_offers.py
"""Automated Bank & UPI Offer Calculator for Indian E-Commerce.

Calculates:
- Base Price: ₹14,999
- Extra Savings: ₹1,500 (SBI Credit Card)
- Net Checkout Price: ₹13,499
Generates high-contrast conversion callouts:
'⚡ Net Checkout Price: ₹13,499 (Save extra ₹1,500 with SBI Card)'
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union

from dealbot.models import Deal, parse_rupees

# Recognized Indian Banks & Payment Methods
BANK_PATTERNS = [
    (re.compile(r"\b(?:sbi|state\s+bank\s+of\s+india)\b", re.IGNORECASE), "SBI Card"),
    (re.compile(r"\bhdfc(?:\s+bank)?\b", re.IGNORECASE), "HDFC Bank Card"),
    (re.compile(r"\bicici(?:\s+bank)?\b", re.IGNORECASE), "ICICI Bank Card"),
    (re.compile(r"\baxis(?:\s+bank)?\b", re.IGNORECASE), "Axis Bank Card"),
    (re.compile(r"\bkotak(?:\s+bank)?\b", re.IGNORECASE), "Kotak Bank Card"),
    (re.compile(r"\b(?:bob|bank\s+of\s+baroda)\b", re.IGNORECASE), "Bank of Baroda Card"),
    (re.compile(r"\bfederal(?:\s+bank)?\b", re.IGNORECASE), "Federal Bank Card"),
    (re.compile(r"\bonecard\b", re.IGNORECASE), "OneCard"),
    (re.compile(r"\bindusind(?:\s+bank)?\b", re.IGNORECASE), "IndusInd Bank Card"),
    (re.compile(r"\bpnb|punjab\s+national\s+bank\b", re.IGNORECASE), "PNB Card"),
    (re.compile(r"\bidfc(?:\s+first)?\b", re.IGNORECASE), "IDFC FIRST Bank Card"),
    (re.compile(r"\bamazon\s+pay\s+icici\b", re.IGNORECASE), "Amazon Pay ICICI Card"),
    (re.compile(r"\bamazon\s+pay\s+upi\b", re.IGNORECASE), "Amazon Pay UPI"),
    (re.compile(r"\bflipkart\s+axis\b", re.IGNORECASE), "Flipkart Axis Card"),
    (re.compile(r"\bupi\b", re.IGNORECASE), "UPI"),
    (re.compile(r"\bcredit\s+card\b", re.IGNORECASE), "Credit Card"),
    (re.compile(r"\bdebit\s+card\b", re.IGNORECASE), "Debit Card"),
]

# Patterns for extracting discounts
# 1. Percentage with cap: "10% Instant Discount on SBI Credit Card up to ₹1,500"
RE_PERCENT_WITH_CAP = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:instant\s+)?(?:discount|off|cashback).*?(?:up\s+to|upto|max(?:imum)?)\s*(?:₹|rs\.?|inr)?\s*([0-9,]+)",
    re.IGNORECASE,
)
# 2. Percentage without explicit cap: "10% off on ICICI Bank Cards"
RE_PERCENT_BARE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:instant\s+)?(?:discount|off|cashback)",
    re.IGNORECASE,
)
# 3. Flat discount: "Flat ₹500 off on HDFC" or "Save ₹1,000 on SBI"
RE_FLAT_DISCOUNT = re.compile(
    r"(?:flat|extra|save|get)\s*(?:₹|rs\.?|inr)?\s*([0-9,]+)\s*(?:instant\s+)?(?:discount|off|cashback)",
    re.IGNORECASE,
)
# 4. UPI Cashback: "Flat ₹50 Cashback on Amazon Pay UPI"
RE_UPI_CASHBACK = re.compile(
    r"(?:flat\s+)?(?:₹|rs\.?|inr)?\s*([0-9,]+)\s*cashback\s+on\s+(?:amazon\s+pay\s+)?upi",
    re.IGNORECASE,
)


@dataclass
class BankOfferResult:
    bank_name: str
    discount_amount: int
    net_price: int
    base_price: int
    discount_type: str  # "percentage", "flat", "cashback"
    callout_text: str
    raw_offer: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bank_name": self.bank_name,
            "discount_amount": self.discount_amount,
            "net_price": self.net_price,
            "base_price": self.base_price,
            "discount_type": self.discount_type,
            "callout_text": self.callout_text,
            "raw_offer": self.raw_offer,
        }


def _detect_bank_name(text: str) -> str:
    """Identify the bank or payment method from the offer text."""
    for pattern, name in BANK_PATTERNS:
        if pattern.search(text):
            return name
    return "Bank Card"


def calculate_bank_offer(
    base_price: Union[int, float, str, None],
    offer_text: Optional[str],
) -> Optional[BankOfferResult]:
    """Parse offer description, calculate net checkout price, and generate callout."""
    if not offer_text or not isinstance(offer_text, str):
        return None

    clean_text = offer_text.strip()
    if clean_text.lower() in ("0", "none", "no", "na", "n/a", "false", ""):
        return None

    # Resolve base price
    num_price: Optional[int] = None
    if isinstance(base_price, (int, float)):
        num_price = int(round(base_price))
    elif isinstance(base_price, str):
        num_price = parse_rupees(base_price)

    if not num_price or num_price <= 0:
        return None

    bank_name = _detect_bank_name(clean_text)
    discount_amount: Optional[int] = None
    discount_type: str = "percentage"

    # 1. Check for Percentage with explicit cap
    pct_cap_m = RE_PERCENT_WITH_CAP.search(clean_text)
    if pct_cap_m:
        pct = float(pct_cap_m.group(1))
        cap = int(pct_cap_m.group(2).replace(",", ""))
        calculated = (num_price * pct) / 100.0
        discount_amount = int(min(calculated, cap))
        discount_type = "percentage"

    # 2. Check for Flat discount / UPI cashback
    if discount_amount is None:
        flat_m = RE_FLAT_DISCOUNT.search(clean_text)
        if flat_m:
            discount_amount = int(flat_m.group(1).replace(",", ""))
            discount_type = "flat"

    if discount_amount is None:
        upi_m = RE_UPI_CASHBACK.search(clean_text)
        if upi_m:
            discount_amount = int(upi_m.group(1).replace(",", ""))
            discount_type = "cashback"

    # 3. Check for bare percentage (cap at 15% of product price or ₹3,000)
    if discount_amount is None:
        bare_m = RE_PERCENT_BARE.search(clean_text)
        if bare_m:
            pct = float(bare_m.group(1))
            if 0 < pct <= 50:
                calculated = (num_price * pct) / 100.0
                discount_amount = int(min(calculated, 3000))
                discount_type = "percentage"

    # If couldn't parse numeric discount or discount is 0
    if not discount_amount or discount_amount <= 0:
        return None

    # Don't let discount exceed base price (sanity guard)
    if discount_amount >= num_price:
        discount_amount = int(num_price * 0.5)

    net_price = max(1, num_price - discount_amount)
    callout_text = f"⚡ Net Checkout Price: ₹{net_price:,} (Save extra ₹{discount_amount:,} with {bank_name})"

    return BankOfferResult(
        bank_name=bank_name,
        discount_amount=discount_amount,
        net_price=net_price,
        base_price=num_price,
        discount_type=discount_type,
        callout_text=callout_text,
        raw_offer=clean_text,
    )


def enrich_deal_with_bank_offer(deal: Deal) -> Deal:
    """Enrich a Deal instance with calculated bank discount and net checkout price."""
    offer_candidates = [
        getattr(deal, "bank_offer", None),
        getattr(deal, "description", None),
        getattr(deal, "title", None),
    ]

    base_p = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
    if not base_p:
        return deal

    for cand in offer_candidates:
        if not cand:
            continue
        res = calculate_bank_offer(base_p, cand)
        if res:
            deal.bank_discount_amount = res.discount_amount
            deal.effective_price = res.net_price
            setattr(deal, "bank_offer_result", res)
            setattr(deal, "bank_offer_callout", res.callout_text)
            if not getattr(deal, "bank_offer", None):
                deal.bank_offer = res.callout_text
            break

    return deal


def format_bank_offer_callout(deal: Deal, html_mode: bool = True) -> Optional[str]:
    """Return formatted callout for deal caption."""
    callout = getattr(deal, "bank_offer_callout", None)
    if not callout and getattr(deal, "bank_offer", None):
        base_p = getattr(deal, "effective_price", None) or parse_rupees(deal.current_price)
        res = calculate_bank_offer(base_p, deal.bank_offer)
        if res:
            callout = res.callout_text

    if not callout:
        return None

    if html_mode:
        return f"<b>{html.escape(callout, quote=False)}</b>"
    return f"*{callout}*"
