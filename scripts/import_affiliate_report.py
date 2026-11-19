#!/usr/bin/env python3
"""Affiliate Report Importer and Closed-Loop EPC Calculator.

Ingests affiliate payout CSV reports (Amazon Associates, Cuelinks, EarnKaro, Flipkart)
matched by SubID, updates deal telemetry in SQLite, and computes empirical EPC
(Earnings Per Click) by category and merchant to refine deal ranking weights.

Usage:
    python scripts/import_affiliate_report.py path/to/report.csv [--db data/state.sqlite3] [--output data/epc_summary.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dealbot.import_affiliate_report")


def find_column(headers: List[str], aliases: List[str]) -> Optional[str]:
    """Find column header matching any alias (case-insensitive)."""
    norm_aliases = [a.lower().replace(" ", "").replace("_", "") for a in aliases]
    for h in headers:
        norm_h = h.lower().replace(" ", "").replace("_", "")
        if norm_h in norm_aliases:
            return h
    return None


def parse_numeric(val: Any) -> float:
    """Parse numeric strings with currency symbols and commas."""
    if val is None:
        return 0.0
    s = str(val).replace("₹", "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def import_report_csv(csv_path: str, db_path: str = "data/state.sqlite3") -> Dict[str, Any]:
    """Import affiliate report CSV and match SubIDs to deal_telemetry."""
    csv_file = Path(csv_path)
    if not csv_file.exists():
        raise FileNotFoundError(f"Report CSV not found: {csv_path}")

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deal_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                subid TEXT,
                canonical_id TEXT,
                merchant TEXT,
                category TEXT,
                price REAL,
                expected_payout REAL,
                profit_score REAL,
                deal_url TEXT,
                clicks INTEGER DEFAULT 0,
                conversions INTEGER DEFAULT 0,
                actual_payout REAL DEFAULT 0.0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_subid ON deal_telemetry (subid)")

    subid_aliases = ["subid", "sub_id", "trackingid", "subtrackingid", "customid", "ascsubtag", "affextparam1"]
    clicks_aliases = ["clicks", "clickcount", "clickscount", "visitors", "traffic"]
    orders_aliases = ["orders", "items", "itemsshipped", "conversions", "actions"]
    payout_aliases = ["commission", "earnings", "estimatedfee", "income", "payout"]

    matched_records = 0
    total_clicks = 0
    total_orders = 0
    total_commission = 0.0

    category_stats: Dict[str, Dict[str, float]] = {}
    merchant_stats: Dict[str, Dict[str, float]] = {}

    with open(csv_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        col_subid = find_column(headers, subid_aliases)
        col_clicks = find_column(headers, clicks_aliases)
        col_orders = find_column(headers, orders_aliases)
        col_payout = find_column(headers, payout_aliases)

        if not col_subid:
            logger.warning("Could not identify SubID column. Available headers: %s", headers)
            return {"error": "Missing SubID column", "headers": headers}

        with sqlite3.connect(db_file) as conn:
            for row in reader:
                subid = str(row.get(col_subid) or "").strip()
                if not subid:
                    continue

                clicks = int(parse_numeric(row.get(col_clicks))) if col_clicks else 1
                orders = int(parse_numeric(row.get(col_orders))) if col_orders else 0
                payout = parse_numeric(row.get(col_payout)) if col_payout else 0.0

                total_clicks += clicks
                total_orders += orders
                total_commission += payout

                # Update SQLite telemetry record
                cur = conn.execute(
                    """
                    UPDATE deal_telemetry
                    SET clicks = clicks + ?,
                        conversions = conversions + ?,
                        actual_payout = actual_payout + ?
                    WHERE subid = ?
                    """,
                    (clicks, orders, payout, subid),
                )
                if cur.rowcount > 0:
                    matched_records += cur.rowcount

                    # Fetch category and merchant for EPC aggregation
                    c_row = conn.execute(
                        "SELECT category, merchant FROM deal_telemetry WHERE subid = ? LIMIT 1",
                        (subid,),
                    ).fetchone()
                    if c_row:
                        cat, merch = c_row[0] or "default", c_row[1] or "amazon"
                        # Category
                        cat_stat = category_stats.setdefault(cat, {"clicks": 0, "payout": 0.0, "orders": 0})
                        cat_stat["clicks"] += clicks
                        cat_stat["payout"] += payout
                        cat_stat["orders"] += orders
                        # Merchant
                        m_stat = merchant_stats.setdefault(merch, {"clicks": 0, "payout": 0.0, "orders": 0})
                        m_stat["clicks"] += clicks
                        m_stat["payout"] += payout
                        m_stat["orders"] += orders

    # Calculate empirical EPC per category
    category_epc = {}
    for cat, data in category_stats.items():
        epc = round(data["payout"] / data["clicks"], 2) if data["clicks"] > 0 else 0.0
        cvr = round((data["orders"] / data["clicks"]) * 100, 2) if data["clicks"] > 0 else 0.0
        category_epc[cat] = {
            "epc_inr": epc,
            "cvr_pct": cvr,
            "clicks": data["clicks"],
            "orders": data["orders"],
            "payout_inr": round(data["payout"], 2),
        }

    merchant_epc = {}
    for merch, data in merchant_stats.items():
        epc = round(data["payout"] / data["clicks"], 2) if data["clicks"] > 0 else 0.0
        cvr = round((data["orders"] / data["clicks"]) * 100, 2) if data["clicks"] > 0 else 0.0
        merchant_epc[merch] = {
            "epc_inr": epc,
            "cvr_pct": cvr,
            "clicks": data["clicks"],
            "orders": data["orders"],
            "payout_inr": round(data["payout"], 2),
        }

    summary = {
        "matched_records": matched_records,
        "total_clicks": total_clicks,
        "total_orders": total_orders,
        "total_commission_inr": round(total_commission, 2),
        "overall_epc_inr": round(total_commission / total_clicks, 2) if total_clicks > 0 else 0.0,
        "by_category": category_epc,
        "by_merchant": merchant_epc,
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description="Import affiliate report CSV and update empirical EPC.")
    parser.add_argument("csv_path", help="Path to affiliate report CSV file")
    parser.add_argument("--db", default="data/state.sqlite3", help="Path to SQLite database")
    parser.add_argument("--output", default="data/epc_summary.json", help="Path to output EPC summary JSON")
    args = parser.parse_args()

    summary = import_report_csv(args.csv_path, db_path=args.db)
    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info("Report imported successfully!")
    logger.info("Total Matched: %d | Total Payout: ₹%.2f | Overall EPC: ₹%.2f",
                summary.get("matched_records", 0),
                summary.get("total_commission_inr", 0.0),
                summary.get("overall_epc_inr", 0.0))
    logger.info("Saved empirical EPC weights to %s", out_file)


if __name__ == "__main__":
    main()
