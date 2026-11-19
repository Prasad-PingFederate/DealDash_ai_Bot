#!/usr/bin/env python3
"""Admin Revenue, Conversion & Affiliate Leakage Digest CLI.

Queries local SQLite telemetry and outputs:
1. 24h deal activity: Posted, Synced to BharatBazaro, Alert DMs dispatched.
2. Top 5 categories and merchants ranked by Expected Profit.
3. Outbound merchant domains and unmonetized traffic gap log.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Load .env if present
try:
    import dotenv
    dotenv.load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

DEFAULT_DB_PATH = ROOT_DIR / "data" / "state.sqlite3"


def get_revenue_digest(db_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Generate 24-hour revenue, telemetry, and leakage metrics."""
    path = Path(db_path or os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))
    if not path.exists():
        return {
            "error": f"Database file not found: {path}",
            "last_24h_posted": 0,
            "last_24h_synced": 0,
            "last_24h_alerts": 0,
            "top_categories": [],
            "top_merchants": [],
            "unmonetized_merchants": [],
        }

    now_utc = datetime.now(timezone.utc)
    one_day_ago = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row

        # 1. Posted deals in last 24h
        cur_post = conn.execute(
            "SELECT COUNT(*) as cnt FROM posted_deals WHERE posted_at >= ?",
            (one_day_ago,),
        )
        posted_count = cur_post.fetchone()["cnt"]

        # 2. Deals synced / live deals
        synced_count = 0
        try:
            cur_sync = conn.execute(
                "SELECT COUNT(*) as cnt FROM live_deals WHERE last_updated_at >= ?",
                (one_day_ago,),
            )
            synced_count = cur_sync.fetchone()["cnt"]
        except sqlite3.OperationalError:
            pass

        # 3. User alert DMs sent in last 24h
        alert_count = 0
        try:
            cur_alert = conn.execute(
                "SELECT COUNT(*) as cnt FROM user_alert_deliveries WHERE delivered_at >= ?",
                (one_day_ago,),
            )
            alert_count = cur_alert.fetchone()["cnt"]
        except sqlite3.OperationalError:
            pass

        # 4. Telemetry aggregation: Total expected payout & top categories
        top_cats = []
        top_merchants = []
        total_expected_payout = 0.0

        try:
            cur_payout = conn.execute(
                """
                SELECT SUM(expected_payout) as total_payout,
                       SUM(clicks) as total_clicks,
                       SUM(actual_payout) as total_actual
                FROM deal_telemetry
                WHERE timestamp >= ?
                """,
                (one_day_ago,),
            )
            row_p = cur_payout.fetchone()
            if row_p and row_p["total_payout"]:
                total_expected_payout = float(row_p["total_payout"] or 0.0)

            # Top categories by expected payout
            cur_cats = conn.execute(
                """
                SELECT category, COUNT(*) as deals_count, SUM(expected_payout) as total_payout, AVG(profit_score) as avg_score
                FROM deal_telemetry
                WHERE timestamp >= ?
                GROUP BY category
                ORDER BY total_payout DESC
                LIMIT 5
                """,
                (one_day_ago,),
            )
            top_cats = [dict(r) for r in cur_cats.fetchall()]

            # Top merchants
            cur_merch = conn.execute(
                """
                SELECT merchant, COUNT(*) as deals_count, SUM(expected_payout) as total_payout
                FROM deal_telemetry
                WHERE timestamp >= ?
                GROUP BY merchant
                ORDER BY total_payout DESC
                LIMIT 5
                """,
                (one_day_ago,),
            )
            top_merchants = [dict(r) for r in cur_merch.fetchall()]
        except sqlite3.OperationalError:
            pass

        # 5. Fallback aggregation from live_deals if telemetry empty
        if not top_cats:
            try:
                cur_ld_cats = conn.execute(
                    """
                    SELECT category, COUNT(*) as deals_count, SUM(expected_payout) as total_payout, AVG(score) as avg_score
                    FROM live_deals
                    GROUP BY category
                    ORDER BY total_payout DESC
                    LIMIT 5
                    """
                )
                top_cats = [dict(r) for r in cur_ld_cats.fetchall()]
            except sqlite3.OperationalError:
                pass

        if not top_merchants:
            try:
                cur_ld_m = conn.execute(
                    """
                    SELECT merchant, COUNT(*) as deals_count, SUM(expected_payout) as total_payout
                    FROM live_deals
                    GROUP BY merchant
                    ORDER BY total_payout DESC
                    LIMIT 5
                    """
                )
                top_merchants = [dict(r) for r in cur_ld_m.fetchall()]
            except sqlite3.OperationalError:
                pass

        # 6. Unmonetized merchant leakage
        unmonetized_merchants = []
        try:
            cur_unmon = conn.execute(
                """
                SELECT merchant, COUNT(*) as count, MAX(deal_url) as sample_url
                FROM deal_telemetry
                WHERE expected_payout = 0.0 AND merchant NOT IN ('amazon', 'flipkart')
                GROUP BY merchant
                ORDER BY count DESC
                """
            )
            unmonetized_merchants = [dict(r) for r in cur_unmon.fetchall()]
        except sqlite3.OperationalError:
            pass

        # Check aggregator configuration gaps
        configured_aggregators = []
        if os.getenv("CUELINKS_PUB_ID"):
            configured_aggregators.append("Cuelinks")
        if os.getenv("EARNKARO_ID"):
            configured_aggregators.append("EarnKaro")
        if os.getenv("ADMITAD_ID"):
            configured_aggregators.append("Admitad")

        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "database": str(path),
            "last_24h_posted": posted_count,
            "last_24h_synced": synced_count,
            "last_24h_alerts": alert_count,
            "total_expected_payout_inr": total_expected_payout,
            "top_categories": top_cats,
            "top_merchants": top_merchants,
            "unmonetized_merchants": unmonetized_merchants,
            "configured_aggregators": configured_aggregators,
        }


def format_revenue_digest_markdown(digest: Dict[str, Any]) -> str:
    """Format revenue digest into HTML/Markdown for Telegram or terminal."""
    lines = [
        "📊 <b>DealDash AI — Revenue & Leakage Digest (Last 24h)</b>",
        f"<i>Generated: {digest.get('timestamp')}</i>\n",
        "<b>Activity Overview:</b>",
        f"• Deals Posted to Channel: <b>{digest.get('last_24h_posted', 0):,}</b>",
        f"• Deals Synced to BharatBazaro: <b>{digest.get('last_24h_synced', 0):,}</b>",
        f"• User Price-Drop DMs Sent: <b>{digest.get('last_24h_alerts', 0):,}</b>",
    ]

    payout = digest.get("total_expected_payout_inr", 0.0)
    if payout > 0:
        lines.append(f"• Expected Payout Generated: <b>₹{payout:,.2f}</b>")

    # Top Categories
    lines.append("\n<b>Top Categories by Expected Profit:</b>")
    top_cats = digest.get("top_categories", [])
    if top_cats:
        for idx, c in enumerate(top_cats[:5], 1):
            cat_name = str(c.get("category") or "general").title()
            p_val = float(c.get("total_payout") or 0.0)
            d_cnt = int(c.get("deals_count") or 0)
            lines.append(f"{idx}. {cat_name}: <b>₹{p_val:,.1f}</b> ({d_cnt} deals)")
    else:
        lines.append("• <i>No category telemetry recorded yet</i>")

    # Top Merchants
    lines.append("\n<b>Top Merchants:</b>")
    top_merch = digest.get("top_merchants", [])
    if top_merch:
        for idx, m in enumerate(top_merch[:5], 1):
            m_name = str(m.get("merchant") or "unknown").title()
            p_val = float(m.get("total_payout") or 0.0)
            d_cnt = int(m.get("deals_count") or 0)
            lines.append(f"{idx}. {m_name}: <b>₹{p_val:,.1f}</b> ({d_cnt} deals)")
    else:
        lines.append("• <i>No merchant telemetry recorded yet</i>")

    # Leakage Gaps
    lines.append("\n<b>Affiliate Leakage & Aggregator Gaps:</b>")
    unmon = digest.get("unmonetized_merchants", [])
    if unmon:
        for u in unmon:
            lines.append(f"⚠️ {u.get('merchant')}: {u.get('count')} unmonetized deals (Add to Cuelinks/EarnKaro)")
    else:
        lines.append("✅ <b>Zero leakage detected</b> on tracked outbound merchants.")

    aggs = digest.get("configured_aggregators", [])
    if not aggs:
        lines.append("\n💡 <i>Tip: Set CUELINKS_PUB_ID or EARNKARO_ID in .env to auto-monetize Myntra/Ajio.</i>")

    return "\n".join(lines)


def main() -> int:
    digest = get_revenue_digest()
    text = format_revenue_digest_markdown(digest)
    # Strip HTML tags for clean console display
    import re
    clean_console = re.sub(r"</?[a-zA-Z0-9]+>", "", text)
    print("\n" + "=" * 60)
    print(clean_console)
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
