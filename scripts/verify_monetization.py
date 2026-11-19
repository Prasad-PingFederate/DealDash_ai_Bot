#!/usr/bin/env python3
"""CLI Monetization & Pipeline Verification Script.

Runs a non-destructive end-to-end smoke test of the monetization, attribution,
and compliance pipeline using real-world URL formats and configuration tables.

Verifies:
1. All required affiliate environment variables (flags missing/placeholder).
2. Competitor tags are 100% stripped and OUR tags + dynamic SubIDs are injected.
3. Amazon 'th=1&psc=1' and Flipkart 'pid'/'lid' variant parameters survive canonicalization.
4. Amazon URLs are NOT cloaked by any 3rd-party shortener and pass compliance checks.
5. Excluded 0%-commission items get score=0 and are blocked from posting.
6. Category caps are respected on high-ticket items (e.g. ₹60,000 laptop, ₹20,000 apparel).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

# Ensure project root is in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Load local .env if present
try:
    import dotenv
    dotenv.load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

from dealbot.compliance import (
    append_compliance_disclaimer,
    format_associates_disclosure,
    format_compliance_disclaimer,
    validate_amazon_url_compliance,
)
from dealbot.monetizer import (
    MonetizationResult,
    build_subid,
    can_publish_monetization,
    detect_store,
    extract_asin,
    extract_flipkart_params,
    monetize_url,
    strip_tracking_params,
    validate_amazon_cloaking,
)
from dealbot.profit_ranker import (
    compute_effective_payout,
    compute_expected_profit,
    compute_profit_score,
    get_category_cap,
    get_category_commission_rate,
    is_zero_commission_item,
    load_commission_config,
    should_publish,
)

# Terminal formatting
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'='*72}{RESET}")
    print(f"{BOLD}{CYAN} {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*72}{RESET}")


def pass_line(msg: str) -> None:
    print(f"  {GREEN}[PASS]{RESET} {msg}")


def fail_line(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn_line(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def info_line(msg: str) -> None:
    print(f"  {CYAN}[INFO]{RESET} {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Affiliate Environment Variables Audit
# ─────────────────────────────────────────────────────────────────────────────

def test_environment_variables() -> bool:
    header("1. Live Affiliate Environment Variables Audit")
    passed = True

    # Primary Amazon Tags
    amz_tag = (
        os.getenv("AMAZON_TAG")
        or os.getenv("AMAZON_AFFILIATE_TAG")
        or os.getenv("AMAZON_PARTNER_TAG")
    )
    if amz_tag and amz_tag != "replace_me" and not amz_tag.startswith("your"):
        pass_line(f"Amazon Associate Tag active: '{amz_tag}'")
    else:
        fail_line("Amazon Associate Tag missing or placeholder (AMAZON_TAG / AMAZON_AFFILIATE_TAG)")
        passed = False

    # Flipkart Affiliate ID
    fk_affid = os.getenv("FLIPKART_AFFILIATE_ID") or os.getenv("FLIPKART_AFFID")
    if fk_affid and fk_affid != "yourflipkartid":
        pass_line(f"Flipkart Direct Affiliate ID active: '{fk_affid}'")
    else:
        warn_line(
            "Flipkart Direct Affiliate ID (FLIPKART_AFFILIATE_ID) not configured; "
            "will use Aggregator fallback."
        )

    # Aggregators Waterfall
    cuelinks_id = os.getenv("CUELINKS_PUB_ID")
    earnkaro_id = os.getenv("EARNKARO_ID")
    admitad_id = os.getenv("ADMITAD_ID")
    template = os.getenv("AFFILIATE_REDIRECT_TEMPLATE")

    aggregators_found = []
    if cuelinks_id:
        aggregators_found.append(f"Cuelinks (CID: {cuelinks_id})")
    if earnkaro_id:
        aggregators_found.append(f"EarnKaro (PID: {earnkaro_id})")
    if admitad_id:
        aggregators_found.append(f"Admitad (ID: {admitad_id})")
    if template:
        aggregators_found.append(f"Custom Template ('{template[:30]}...')")

    if aggregators_found:
        pass_line(f"Aggregator Fallback active: {', '.join(aggregators_found)}")
    else:
        warn_line(
            "No aggregator IDs (CUELINKS_PUB_ID, EARNKARO_ID, AFFILIATE_REDIRECT_TEMPLATE) set. "
            "Non-Amazon stores (Myntra, Ajio) will log to unmonetized gap log."
        )

    # Internal Sync & Storefront Integration
    sync_secret = os.getenv("INTERNAL_SYNC_SECRET", "").strip()
    sync_url = os.getenv("BHARATBAZARO_SYNC_URL", "").strip()
    if sync_secret and sync_secret != "replace_with_strong_internal_sync_secret":
        pass_line(f"INTERNAL_SYNC_SECRET configured ({sync_secret[:4]}***)")
    else:
        warn_line("INTERNAL_SYNC_SECRET missing or using default placeholder in .env")

    if sync_url:
        pass_line(f"BHARATBAZARO_SYNC_URL active: '{sync_url}'")
    else:
        warn_line("BHARATBAZARO_SYNC_URL missing in .env (will use local JSON export only)")

    # Environment Variables Status Matrix
    print(f"\n  {BOLD}Configured vs Missing Environment Variables in .env:{RESET}")
    env_audit = [
        ("Amazon Tag (AMAZON_TAG / AMAZON_AFFILIATE_TAG)", amz_tag, True),
        ("Flipkart Affiliate ID (FLIPKART_AFFILIATE_ID)", fk_affid, False),
        ("Cuelinks CID (CUELINKS_PUB_ID)", cuelinks_id, False),
        ("EarnKaro PID (EARNKARO_ID)", earnkaro_id, False),
        ("Internal Sync Secret (INTERNAL_SYNC_SECRET)", sync_secret, False),
        ("BharatBazaro Sync URL (BHARATBAZARO_SYNC_URL)", sync_url, False),
    ]
    for label, val, is_req in env_audit:
        is_set = bool(val and val != "replace_me" and not str(val).startswith("replace_"))
        status_txt = f"{GREEN}Configured ('{str(val)[:24]}'){RESET}" if is_set else f"{YELLOW}Missing / Placeholder{RESET}"
        req_tag = f"{RED}[Required]{RESET}" if is_req else "[Optional/Fallback]"
        print(f"    • {label} {req_tag}: {status_txt}")

    # Profit Gating Thresholds
    min_payout = os.getenv("MIN_EXPECTED_PAYOUT_INR", "20.0")
    min_score = os.getenv("MIN_PROFIT_SCORE", "50.0")
    pass_line(f"Profit Ranking Gating Thresholds: Min Payout = ₹{min_payout}, Min Score = {min_score}")

    return passed


# ─────────────────────────────────────────────────────────────────────────────
# 2. Competitor Tag Stripping & Attribution Injection
# ─────────────────────────────────────────────────────────────────────────────

def test_competitor_tag_stripping() -> bool:
    header("2. Competitor Tag Stripping & SubID Attribution Injection")
    passed = True

    # Real-world dirty Amazon URL from Telegram competitor deal channel
    dirty_amz_url = (
        "https://www.amazon.in/Sony-WH-1000XM5-Cancelling-Headphones/dp/B0BS1QCFHX?"
        "tag=badcompetitor-21&affid=competitor99&ascsubtag=old_channel_drop&"
        "utm_source=telegram&utm_medium=affiliate&ref=sr_1_1&pf_rd_r=XYZ789"
    )

    res = monetize_url(
        dirty_amz_url,
        channel="tg",
        category="electronics",
        deal_id="B0BS1QCFHX",
        allow_unshorten=False,
    )

    # Verify monetization status
    if not res.is_monetized:
        fail_line(f"Failed to monetize Amazon deal URL: {res.error_reason}")
        return False

    out_url = res.monetized_url

    # Check competitor param removal
    leaked_params = []
    for bad in ["badcompetitor-21", "competitor99", "old_channel_drop", "utm_source", "pf_rd_"]:
        if bad in out_url:
            leaked_params.append(bad)

    if leaked_params:
        fail_line(f"Competitor parameters leaked into monetized URL: {leaked_params}")
        passed = False
    else:
        pass_line("Competitor tags (tag, affid, ascsubtag, utm_*, pf_rd_*) 100% stripped")

    # Check our tag injection
    amz_tag = (
        os.getenv("AMAZON_TAG")
        or os.getenv("AMAZON_AFFILIATE_TAG")
        or os.getenv("AMAZON_PARTNER_TAG")
        or "dammaiprasad-21"
    )
    if f"tag={amz_tag}" in out_url:
        pass_line(f"Our partner tag successfully injected: 'tag={amz_tag}'")
    else:
        fail_line(f"Our partner tag missing from output URL: {out_url}")
        passed = False

    # Check dynamic SubID injection
    expected_subid_prefix = "bot_tg_electro"
    if "ascsubtag=" in out_url and expected_subid_prefix in out_url:
        pass_line(f"Dynamic SubID attribution injected: '{res.subid}'")
    else:
        fail_line(f"SubID attribution missing or invalid in output URL: {out_url}")
        passed = False

    info_line(f"Sample Output URL: {out_url}")
    return passed


# ─────────────────────────────────────────────────────────────────────────────
# 3. Product Variant Preservation
# ─────────────────────────────────────────────────────────────────────────────

def test_variant_preservation() -> bool:
    header("3. Product Variant Parameter Preservation (Amazon & Flipkart)")
    passed = True

    # Amazon with variant (th=1, psc=1) and seller merchant ID (smid)
    amz_variant_url = (
        "https://www.amazon.in/dp/B0BS1QCFHX?tag=other-21&th=1&psc=1&smid=A14URN98ASR2V1"
    )
    amz_res = monetize_url(
        amz_variant_url,
        channel="tg",
        category="electronics",
        deal_id="B0BS1QCFHX",
        allow_unshorten=False,
    )

    if "th=1" in amz_res.monetized_url and "psc=1" in amz_res.monetized_url:
        pass_line("Amazon variant parameters ('th=1&psc=1') preserved cleanly")
    else:
        fail_line(f"Amazon variant parameters lost in canonicalization: {amz_res.monetized_url}")
        passed = False

    if "smid=A14URN98ASR2V1" in amz_res.monetized_url:
        pass_line("Amazon merchant/seller ID ('smid=A14URN98ASR2V1') preserved cleanly")
    else:
        warn_line(f"Amazon smid parameter not present in output: {amz_res.monetized_url}")

    # Flipkart with product id (pid) and listing id (lid)
    fk_variant_url = (
        "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4?"
        "pid=MOBGTAGPTB3VS24W&lid=LSTMOBGTAGPTB3VS24WVUQZQX&affid=competitor"
    )
    fk_res = monetize_url(
        fk_variant_url,
        channel="tg",
        category="mobiles",
        deal_id="MOBGTAGPTB3VS24W",
        allow_unshorten=False,
    )

    pid, lid = extract_flipkart_params(fk_res.monetized_url)
    if pid == "MOBGTAGPTB3VS24W" or "pid=MOBGTAGPTB3VS24W" in fk_res.monetized_url:
        pass_line("Flipkart Product ID ('pid=MOBGTAGPTB3VS24W') preserved cleanly")
    else:
        fail_line(f"Flipkart pid lost: {fk_res.monetized_url}")
        passed = False

    if lid == "LSTMOBGTAGPTB3VS24WVUQZQX" or "lid=LSTMOBGTAGPTB3VS24WVUQZQX" in fk_res.monetized_url:
        pass_line("Flipkart Listing ID ('lid=LSTMOBGTAGPTB3VS24WVUQZQX') preserved cleanly")
    else:
        fail_line(f"Flipkart lid lost: {fk_res.monetized_url}")
        passed = False

    return passed


# ─────────────────────────────────────────────────────────────────────────────
# 4. Amazon Anti-Cloaking & Compliance Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_amazon_anti_cloaking_and_compliance() -> bool:
    header("4. Amazon Anti-Cloaking Policy & Compliance Verification")
    passed = True

    # 4a. Verify disallowed third-party shortener cloaking is caught
    disallowed_urls = [
        "https://bit.ly/3xyzAmazonDeal",
        "https://tinyurl.com/amz-steal",
        "https://cutt.ly/amz-drop",
        "https://rb.gy/amz-sale",
    ]
    for bad_url in disallowed_urls:
        is_compliant, reason = validate_amazon_url_compliance(bad_url)
        if not is_compliant and "Associates Violation" in reason:
            pass_line(f"Blocked illegal Amazon link cloaker: {bad_url}")
        else:
            fail_line(f"Failed to catch cloaked Amazon link: {bad_url} (Reason: {reason})")
            passed = False

    # 4b. Verify canonical Amazon affiliate URL passes
    canonical_amz = "https://www.amazon.in/dp/B0BS1QCFHX?tag=dammaiprasad-21&ascsubtag=bot_tg_mob_B0BS1QCFHX"
    is_ok, reason_ok = validate_amazon_url_compliance(canonical_amz)
    if is_ok:
        pass_line(f"Canonical direct Amazon affiliate link approved: {reason_ok}")
    else:
        fail_line(f"Canonical Amazon URL falsely rejected: {reason_ok}")
        passed = False

    # 4c. Verify Associates disclosure & IST disclaimer formatting
    disclaimer = format_compliance_disclaimer()
    disclosure = format_associates_disclosure("amazon")
    caption = "🔥 Super Deal: Apple iPhone 15 @ ₹54,999"
    formatted = append_compliance_disclaimer(caption, store="amazon")

    if "#ad Affiliate link" in formatted:
        pass_line("Amazon Associates #ad disclosure appended cleanly")
    else:
        fail_line("Amazon Associates #ad disclosure missing from caption")
        passed = False

    if "Price & availability accurate as of" in formatted and "IST" in formatted:
        pass_line("Amazon Associates accurate IST timestamp disclaimer appended cleanly")
    else:
        fail_line("IST timestamp disclaimer missing or invalid in caption")
        passed = False

    return passed


# ─────────────────────────────────────────────────────────────────────────────
# 5. Excluded 0%-Commission Item Gating
# ─────────────────────────────────────────────────────────────────────────────

def test_zero_commission_exclusions() -> bool:
    header("5. Excluded 0%-Commission Item Gating")
    passed = True

    excluded_test_cases = [
        ("Amazon Pay eGift Card ₹2,000", "gift_cards", 2000.0),
        ("Kundan 24k 5g Gold Coin 999.9 Purity", "jewelry", 38000.0),
        ("MMTC-PAMP 100g Silver Bar 999.9 Purity", "bullion", 8500.0),
        ("Flipkart Gift Voucher ₹1,000", "default", 1000.0),
    ]

    for title, cat, price in excluded_test_cases:
        is_excluded = is_zero_commission_item(title, cat)
        payout = compute_effective_payout(price, cat, title=title)
        profit_score = compute_expected_profit(price, cat, title=title)
        can_pub, reason, metrics = should_publish(price, cat, title=title)

        if is_excluded and payout == 0.0 and profit_score == 0.0 and not can_pub:
            pass_line(f"Blocked 0%-commission item: '{title}' (Payout: ₹{payout}, Score: {profit_score})")
        else:
            fail_line(
                f"0%-commission item NOT blocked: '{title}' "
                f"(is_excluded={is_excluded}, payout={payout}, can_pub={can_pub})"
            )
            passed = False

    return passed


# ─────────────────────────────────────────────────────────────────────────────
# 6. Category Commission Caps on High-Ticket Items
# ─────────────────────────────────────────────────────────────────────────────

def test_category_commission_caps() -> bool:
    header("6. Category Commission Caps on High-Ticket Items")
    passed = True

    test_cases = [
        # (item_description, price, category, expected_uncapped, expected_cap)
        ("High-End Gaming Laptop", 60000.0, "electronics", 900.0, 1500.0),   # 1.5% of 60k = 900 (< 1500 cap)
        ("Ultra Premium MacBook Pro", 150000.0, "electronics", 2250.0, 1500.0), # 1.5% of 150k = 2250 -> CAPPED to 1500
        ("Designer Saree / Luxury Apparel", 20000.0, "fashion", 1600.0, 1000.0), # 8.0% of 20k = 1600 -> CAPPED to 1000
        ("Double Door Inverter Refrigerator", 45000.0, "appliances", 2700.0, 2000.0), # 6.0% of 45k = 2700 -> CAPPED to 2000
    ]

    for desc, price, cat, uncapped, cap in test_cases:
        rate = get_category_commission_rate(cat)
        configured_cap = get_category_cap(cat)
        payout = compute_effective_payout(price, cat)

        expected_effective = min(uncapped, cap)
        if abs(payout - expected_effective) < 0.01:
            pass_line(
                f"{desc} (₹{price:,.0f} in {cat}): "
                f"Uncapped ₹{uncapped:,.1f} -> Effective Payout ₹{payout:,.1f} (Cap: ₹{cap:,.0f})"
            )
        else:
            fail_line(
                f"{desc} payout mismatch: Expected ₹{expected_effective}, got ₹{payout} "
                f"(Configured Cap: ₹{configured_cap})"
            )
            passed = False

    return passed


# ─────────────────────────────────────────────────────────────────────────────
# Main Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"\n{BOLD}DealDash AI & BharatBazaro Pipeline Smoke Test{RESET}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Root: {ROOT_DIR}")

    results = [
        ("Affiliate Environment Variables", test_environment_variables()),
        ("Competitor Parameter Stripping & SubID Injection", test_competitor_tag_stripping()),
        ("Variant Parameter Preservation", test_variant_preservation()),
        ("Amazon Anti-Cloaking & Compliance", test_amazon_anti_cloaking_and_compliance()),
        ("0%-Commission Exclusion Gating", test_zero_commission_exclusions()),
        ("Category Caps on High-Ticket Items", test_category_commission_caps()),
    ]

    header("Summary of Pipeline Verification")
    all_passed = True
    for name, ok in results:
        status_tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  [{status_tag}] {name}")
        if not ok:
            all_passed = False

    print()
    if all_passed:
        print(f"{BOLD}{GREEN}ALL 6 PIPELINE VERIFICATION SUITES PASSED!{RESET}")
        return 0
    else:
        print(f"{BOLD}{RED}SOME PIPELINE CHECKS FAILED - REVIEW LOGS ABOVE.{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
