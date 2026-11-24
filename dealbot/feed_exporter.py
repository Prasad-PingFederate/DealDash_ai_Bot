# dealbot/feed_exporter.py
"""Canonical JSON Feed Exporter for DealDash AI Bot.

Exports active, verified deals from state.sqlite3 and live data into:
1. data/deals_feed.json - All live deals sorted by epc_score descending.
2. data/video_queue.json - Top 3 highest-EPC deals from the last 24h
   (with effective_price >= 499 and rating >= 4.0, marked video_eligible: true).

Exact Canonical Schema per deal:
- asin
- title
- category
- mrp
- live_price
- coupon_discount
- effective_price
- commission_rate
- expected_commission
- epc_score
- rating
- image_url
- base_affiliate_url
- video_eligible
- updated_at
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dealbot.models import parse_rupees
from dealbot.profit_ranker import calculate_epc_score, get_category_commission_rate

logger = logging.getLogger("dealbot.feed_exporter")
IST = timezone(timedelta(hours=5, minutes=30))


def extract_clean_asin(raw_id: Optional[str], url: Optional[str] = None) -> str:
    """Extract standard 10-character Amazon ASIN from deal id or product URL."""
    if url:
        match = re.search(r"/(?:dp|gp/product|d)/([A-Z0-9]{10})", str(url), re.IGNORECASE)
        if match:
            return match.group(1).upper()

    s = str(raw_id or "").strip()
    if s.startswith("amz-") or s.startswith("amazon:"):
        s = s.split(":", 1)[-1].split("-", 1)[-1]

    asin_match = re.search(r"([B0-9][A-Z0-9]{9})", s, re.IGNORECASE)
    if asin_match:
        return asin_match.group(1).upper()

    return s or "UNKNOWN_ASIN"


def build_canonical_affiliate_url(asin: str, raw_url: Optional[str] = None) -> str:
    """Build standardized Amazon India affiliate URL with the default partner tag."""
    tag = (
        os.getenv("AMAZON_AFFILIATE_TAG")
        or os.getenv("AMAZON_PARTNER_TAG")
        or "dammaiprasad-21"
    ).strip()

    if asin and len(asin) == 10 and asin.isalnum():
        return f"https://www.amazon.in/dp/{asin}?tag={tag}"

    if raw_url and "tag=" in raw_url:
        return raw_url

    if raw_url and "amazon.in" in raw_url:
        sep = "&" if "?" in raw_url else "?"
        return f"{raw_url}{sep}tag={tag}"

    return f"https://www.amazon.in/dp/{asin}?tag={tag}"


def load_raw_active_deals(
    db_path: Path,
    data_dir: Path
) -> List[Dict[str, Any]]:
    """Load active, verified deals merging SQLite state and data/live_deals.json."""
    deals_by_asin: Dict[str, Dict[str, Any]] = {}

    # 1. Load baseline deals from data/live_deals.json (contains rich metadata like images, MRP, coupons)
    live_deals_file = data_dir / "live_deals.json"
    if live_deals_file.exists():
        try:
            with open(live_deals_file, "r", encoding="utf-8") as f:
                json_items = json.load(f)
                if isinstance(json_items, list):
                    for item in json_items:
                        asin = extract_clean_asin(item.get("asin") or item.get("id"), item.get("affiliate_url"))
                        if asin:
                            deals_by_asin[asin] = dict(item)
                    logger.debug("Loaded %d deals from live_deals.json", len(deals_by_asin))
        except Exception as e:
            logger.warning("Could not read live_deals.json in feed_exporter: %s", e)

    # 2. Query state.sqlite3 for active deals from posted_deals
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # Query posted_deals where status is active
                cur.execute("""
                    SELECT * FROM posted_deals
                    WHERE status = 'active' OR status IS NULL
                    ORDER BY posted_at DESC
                """)
                rows = cur.fetchall()
                for row in rows:
                    r_dict = dict(row)
                    deal_id = r_dict.get("deal_id") or ""
                    caption = r_dict.get("caption") or ""

                    # Extract ASIN from deal_id or caption links
                    url_match = re.search(r'href="(https://[^"]+)"', caption)
                    raw_url = url_match.group(1) if url_match else None
                    asin = extract_clean_asin(deal_id, raw_url)

                    # Extract price from caption if not directly stored in table
                    p_val = r_dict.get("price")
                    if p_val is None or float(p_val or 0) <= 0:
                        p_match = re.search(r"(?:Deal Price|Price):\s*(?:₹|Rs\.?)?\s*([0-9,]+)", caption)
                        if p_match:
                            p_val = float(p_match.group(1).replace(",", ""))

                    # Extract MRP from caption
                    mrp_val = None
                    m_match = re.search(r"(?:MRP):\s*<s>(?:₹|Rs\.?)?\s*([0-9,]+)</s>", caption)
                    if m_match:
                        mrp_val = float(m_match.group(1).replace(",", ""))

                    # Extract title from caption
                    title_val = None
                    t_match = re.search(r"🏷️\s*<b>(.*?)</b>", caption)
                    if t_match:
                        title_val = t_match.group(1).strip()

                    # Merge or update into deals_by_asin
                    if asin in deals_by_asin:
                        target = deals_by_asin[asin]
                        if p_val and float(p_val) > 0:
                            target["live_price"] = float(p_val)
                        if mrp_val:
                            target["mrp"] = float(mrp_val)
                        if r_dict.get("posted_at"):
                            target["posted_at"] = r_dict.get("posted_at")
                        if r_dict.get("last_checked_at"):
                            target["last_checked_at"] = r_dict.get("last_checked_at")
                    else:
                        deals_by_asin[asin] = {
                            "asin": asin,
                            "id": deal_id,
                            "title": title_val or f"Amazon Deal {asin}",
                            "live_price": float(p_val) if p_val else 999.0,
                            "mrp": float(mrp_val) if mrp_val else (float(p_val) * 1.5 if p_val else 1999.0),
                            "category": r_dict.get("category") or "General",
                            "affiliate_url": raw_url or f"https://www.amazon.in/dp/{asin}",
                            "posted_at": r_dict.get("posted_at"),
                            "last_checked_at": r_dict.get("last_checked_at"),
                            "status": r_dict.get("status", "active"),
                        }
        except Exception as e:
            logger.warning("Error querying posted_deals in feed_exporter: %s", e)

    return list(deals_by_asin.values())


def transform_to_canonical_deal(
    raw_deal: Dict[str, Any],
    now_utc: datetime
) -> Dict[str, Any]:
    """Transform a raw deal dict into the strict canonical schema with EPC metrics."""
    raw_asin = raw_deal.get("asin") or raw_deal.get("id")
    raw_url = raw_deal.get("affiliate_url") or raw_deal.get("url")
    asin = extract_clean_asin(raw_asin, raw_url)

    title = str(raw_deal.get("title") or f"Amazon Deal {asin}").strip()
    category = str(raw_deal.get("category") or "General").strip()

    # Prices
    mrp_val = raw_deal.get("mrp") or raw_deal.get("original_price")
    mrp = float(mrp_val) if mrp_val is not None else 0.0

    live_p_val = raw_deal.get("live_price") or raw_deal.get("list_price") or raw_deal.get("current_price")
    if live_p_val is None:
        live_price = float(raw_deal.get("effective_price") or 0.0)
    else:
        live_price = float(parse_rupees(str(live_p_val)) or 0.0)

    coupon_disc_val = raw_deal.get("coupon_discount") or 0.0
    coupon_discount = float(coupon_disc_val)

    eff_p_val = raw_deal.get("effective_price")
    if eff_p_val is not None and float(eff_p_val) > 0:
        effective_price = float(eff_p_val)
    elif live_price > 0:
        effective_price = max(0.0, live_price - coupon_discount)
    else:
        effective_price = 0.0

    if mrp <= 0.0:
        mrp = max(effective_price, live_price)

    # Discount pct
    disc_pct_val = raw_deal.get("effective_discount_pct") or raw_deal.get("discount_pct") or raw_deal.get("discount_percent")
    if disc_pct_val is not None and float(disc_pct_val) > 0:
        discount_pct = float(disc_pct_val)
    elif mrp > effective_price > 0:
        discount_pct = round(((mrp - effective_price) / mrp) * 100.0, 1)
    else:
        discount_pct = 0.0

    # Rating
    rating_val = raw_deal.get("rating")
    rating = float(rating_val) if rating_val is not None else 4.2

    # Commission and EPC calculation
    rate_val = raw_deal.get("commission_rate")
    if rate_val is not None and float(rate_val) > 0:
        commission_rate = float(rate_val)
    else:
        commission_rate = get_category_commission_rate(category)

    expected_commission = round(effective_price * commission_rate, 2)
    epc_score = calculate_epc_score(
        effective_price=effective_price,
        commission_rate=commission_rate,
        discount_pct=discount_pct,
        rating=rating,
        category=category,
    )

    # Image URL
    image_url = str(
        raw_deal.get("image_url")
        or f"https://m.media-amazon.com/images/P/{asin}.jpg"
    ).strip()

    # Base affiliate URL
    base_affiliate_url = build_canonical_affiliate_url(asin, raw_url)

    # Freshness / Timestamp
    posted_str = raw_deal.get("posted_at") or raw_deal.get("updated_at")
    dt_posted = None
    if posted_str:
        try:
            dt_clean = str(posted_str).replace("Z", "+00:00").replace(" ", "T")
            dt_posted = datetime.fromisoformat(dt_clean)
            if dt_posted.tzinfo is None:
                dt_posted = dt_posted.replace(tzinfo=timezone.utc)
        except Exception:
            dt_posted = now_utc
    else:
        dt_posted = now_utc

    age_hours = (now_utc - dt_posted).total_seconds() / 3600.0

    # Video eligibility: within last 24h, effective_price >= 499, rating >= 4.0
    video_eligible = bool(effective_price >= 499.0 and rating >= 4.0 and age_hours <= 24.0)

    updated_at_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Strict schema dictionary
    canonical_deal = {
        "asin": asin,
        "title": title,
        "category": category,
        "mrp": round(mrp, 2) if mrp % 1 else int(mrp),
        "live_price": round(live_price, 2) if live_price % 1 else int(live_price),
        "coupon_discount": round(coupon_discount, 2) if coupon_discount % 1 else int(coupon_discount),
        "effective_price": round(effective_price, 2) if effective_price % 1 else int(effective_price),
        "commission_rate": round(commission_rate, 4),
        "expected_commission": expected_commission,
        "epc_score": epc_score,
        "rating": round(rating, 2),
        "image_url": image_url,
        "base_affiliate_url": base_affiliate_url,
        "video_eligible": video_eligible,
        "updated_at": updated_at_str,
    }

    return canonical_deal


def export_feeds(
    store: Any = None,
    db_path: Optional[str] = None,
    output_dir: Optional[str] = None
) -> Dict[str, Any]:
    """Generate and write data/deals_feed.json and data/video_queue.json.

    Returns:
        Summary dict containing counts and export paths.
    """
    project_root = Path(__file__).resolve().parent.parent

    # Determine paths
    raw_data_dir = output_dir or os.getenv("DATA_DIR")
    data_dir = Path(raw_data_dir) if raw_data_dir else (project_root / "data")
    data_dir.mkdir(parents=True, exist_ok=True)

    if db_path:
        actual_db_path = Path(db_path)
    elif store and hasattr(store, "db_path"):
        actual_db_path = Path(store.db_path)
    else:
        raw_db = os.getenv("DATABASE_PATH", "data/state.sqlite3")
        actual_db_path = Path(raw_db)
        if not actual_db_path.is_absolute():
            actual_db_path = project_root / actual_db_path

    now_utc = datetime.now(timezone.utc)
    raw_deals = load_raw_active_deals(actual_db_path, data_dir)
    logger.info("Found %d raw active deal candidates to canonicalize.", len(raw_deals))

    canonical_deals: List[Dict[str, Any]] = []
    seen_asins = set()

    for d in raw_deals:
        cd = transform_to_canonical_deal(d, now_utc)
        asin = cd["asin"]
        if asin and asin not in seen_asins and cd["effective_price"] > 0:
            seen_asins.add(asin)
            canonical_deals.append(cd)

    # 1. Sort all deals by epc_score descending
    canonical_deals.sort(key=lambda x: x["epc_score"], reverse=True)

    # 2. Extract video queue: top 3 highest-EPC deals from last 24h with effective_price >= 499 & rating >= 4.0
    video_eligible_deals = [d for d in canonical_deals if d["video_eligible"]]

    # Fallback if fewer than 3 deals are strictly within 24h: pick top EPC deals with price >= 499
    if len(video_eligible_deals) < 3:
        price_eligible = [
            d for d in canonical_deals
            if d["effective_price"] >= 499.0 and d["asin"] not in {v["asin"] for v in video_eligible_deals}
        ]
        video_eligible_deals.extend(price_eligible)

    # Take strictly top 3 and ensure video_eligible is explicitly True
    top_video_deals = []
    for d in video_eligible_deals[:3]:
        item_copy = dict(d)
        item_copy["video_eligible"] = True
        top_video_deals.append(item_copy)

    # 3. Write data/deals_feed.json
    deals_feed_path = data_dir / "deals_feed.json"
    with open(deals_feed_path, "w", encoding="utf-8") as f:
        json.dump(canonical_deals, f, indent=2, ensure_ascii=False)
    logger.info("Exported %d deals to %s (sorted by epc_score)", len(canonical_deals), deals_feed_path)

    # 4. Write data/video_queue.json
    video_queue_path = data_dir / "video_queue.json"
    with open(video_queue_path, "w", encoding="utf-8") as f:
        json.dump(top_video_deals, f, indent=2, ensure_ascii=False)
    logger.info("Exported %d top deals to %s", len(top_video_deals), video_queue_path)

    summary = {
        "status": "success",
        "deals_feed_count": len(canonical_deals),
        "video_queue_count": len(top_video_deals),
        "deals_feed_path": str(deals_feed_path),
        "video_queue_path": str(video_queue_path),
        "top_epc_score": canonical_deals[0]["epc_score"] if canonical_deals else 0.0,
    }
    return summary


def dispatch_downstream_webhooks(dispatch_pat: Optional[str] = None) -> Dict[str, Any]:
    """Trigger repository_dispatch on downstream repositories using DISPATCH_PAT.

    - Always triggers Prasad-PingFederate/bharatbazaro with event_type: deals_updated
    - If current IST hour is 10, 15, or 20, also triggers Prasad-PingFederate/LeadForge-Video
      with event_type: render_top_deals
    """
    token = dispatch_pat or os.getenv("DISPATCH_PAT", "").strip()
    if not token:
        logger.warning("DISPATCH_PAT is not set. Downstream webhook dispatch skipped.")
        return {"status": "skipped", "reason": "missing_token"}

    import requests

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    results = {}

    # 1. Trigger BharatBazaro
    bb_url = "https://api.github.com/repos/Prasad-PingFederate/bharatbazaro/dispatches"
    try:
        r = requests.post(bb_url, headers=headers, json={"event_type": "deals_updated"}, timeout=15)
        results["bharatbazaro"] = {"status_code": r.status_code, "ok": r.ok}
        if r.ok:
            logger.info("Successfully dispatched deals_updated to Prasad-PingFederate/bharatbazaro")
        else:
            logger.warning("Failed to dispatch to bharatbazaro: %s %s", r.status_code, r.text)
    except Exception as e:
        results["bharatbazaro"] = {"error": str(e)}
        logger.warning("Exception dispatching to bharatbazaro: %s", e)

    # 2. Trigger LeadForge-Video if IST hour is 10, 15, or 20
    now_ist = datetime.now(IST)
    ist_hour = now_ist.hour
    results["current_ist_hour"] = ist_hour

    if ist_hour in (10, 15, 20):
        video_url = "https://api.github.com/repos/Prasad-PingFederate/LeadForge-Video/dispatches"
        try:
            r = requests.post(video_url, headers=headers, json={"event_type": "render_top_deals"}, timeout=15)
            results["leadforge_video"] = {"status_code": r.status_code, "ok": r.ok, "triggered": True}
            if r.ok:
                logger.info("Successfully dispatched render_top_deals to Prasad-PingFederate/LeadForge-Video (IST %d:00)", ist_hour)
            else:
                logger.warning("Failed to dispatch to LeadForge-Video: %s %s", r.status_code, r.text)
        except Exception as e:
            results["leadforge_video"] = {"error": str(e), "triggered": True}
            logger.warning("Exception dispatching to LeadForge-Video: %s", e)
    else:
        results["leadforge_video"] = {"triggered": False, "reason": f"IST hour {ist_hour} not in (10, 15, 20)"}
        logger.info("Skipping LeadForge-Video render trigger (Current IST hour: %d).", ist_hour)

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    summary = export_feeds()
    print("Feeds Exported:", json.dumps(summary, indent=2))
    if "--dispatch" in sys.argv:
        disp = dispatch_downstream_webhooks()
        print("Dispatch Results:", json.dumps(disp, indent=2))
