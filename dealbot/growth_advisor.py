"""
Autonomous AI Growth & Strategy Advisor ("Hermes Advisor").
Analyzes historical deal performance from SQLite, evaluates discounts, categories,
and stock stability, and uses Nous Hermes (or Gemini) to generate an actionable
growth strategy report for the channel administrator.
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

from dealbot.ai_copywriter import AICopywriter

logger = logging.getLogger("dealbot.growth_advisor")

def get_deal_metrics(db_path: str) -> Dict[str, Any]:
    """Extract quantitative performance and distribution metrics from SQLite state."""
    metrics = {
        "total_deals": 0,
        "deals_last_24h": 0,
        "deals_last_7d": 0,
        "active_deals": 0,
        "expired_deals": 0,
        "retailers": {},
        "discounts": [],
        "sample_titles": [],
    }

    if not os.path.exists(db_path):
        return metrics

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT deal_id, retailer, caption, status, posted_at FROM posted_deals ORDER BY posted_at DESC").fetchall()

    metrics["total_deals"] = len(rows)
    for r in rows:
        posted = str(r["posted_at"] or "")
        status = r["status"] or "active"
        retailer = r["retailer"] or "Amazon"
        caption = r["caption"] or ""

        if status == "active":
            metrics["active_deals"] += 1
        else:
            metrics["expired_deals"] += 1

        if posted >= day_ago:
            metrics["deals_last_24h"] += 1
        if posted >= week_ago:
            metrics["deals_last_7d"] += 1

        metrics["retailers"][retailer] = metrics["retailers"].get(retailer, 0) + 1

        # Extract discount % if present in caption
        disc_match = re.search(r"(\d+)%\s*OFF", caption, re.IGNORECASE)
        if disc_match:
            metrics["discounts"].append(int(disc_match.group(1)))

        # Extract title from caption
        title_match = re.search(r"🏷️\s*<b>(.*?)</b>", caption)
        if title_match and len(metrics["sample_titles"]) < 10:
            metrics["sample_titles"].append(title_match.group(1).strip())

    return metrics

def build_advisor_prompt(metrics: Dict[str, Any], sale_mode: str) -> str:
    total = metrics["total_deals"]
    d24 = metrics["deals_last_24h"]
    d7 = metrics["deals_last_7d"]
    active = metrics["active_deals"]
    expired = metrics["expired_deals"]
    retailers = metrics["retailers"]
    discounts = metrics["discounts"]

    avg_discount = round(sum(discounts) / len(discounts), 1) if discounts else 0
    max_discount = max(discounts) if discounts else 0
    deals_over_50 = sum(1 for d in discounts if d >= 50)
    oos_rate = round((expired / total * 100), 1) if total > 0 else 0

    sample_list = "\n".join(f"- {t}" for t in metrics["sample_titles"][:8]) if metrics["sample_titles"] else "None recorded yet."

    return (
        f"You are Nous Hermes, an autonomous AI Growth Strategist & E-Commerce Auditor for an Indian Telegram deal channel.\n"
        f"Audit the following channel metrics and produce an executive growth report with actionable recommendations.\n\n"
        f"CHANNEL TELEMETRY:\n"
        f"• Total Deals in Database: {total}\n"
        f"• Deals Posted (Last 24 Hours): {d24}\n"
        f"• Deals Posted (Last 7 Days): {d7}\n"
        f"• Active Deals: {active} | Expired / Out of Stock Deals: {expired} (OOS Rate: {oos_rate}%)\n"
        f"• Retailer Breakdown: {retailers}\n"
        f"• Average Deal Discount: {avg_discount}%\n"
        f"• Highest Discount: {max_discount}%\n"
        f"• Deals with 50%+ Discount: {deals_over_50}\n"
        f"• Active Festive/Sale Mode: {sale_mode or 'Standard Operation'}\n\n"
        f"SAMPLE RECENT TITLES:\n{sample_list}\n\n"
        f"REPORT REQUIREMENTS:\n"
        f"Provide your analysis in clean GitHub-Flavored Markdown with the following sections:\n"
        f"### 1. 📊 Executive Performance Audit\n"
        f"(Assess discount quality, publishing volume, and out-of-stock rate)\n"
        f"### 2. 🎯 Trending Product Categories for Indian Shoppers\n"
        f"(Specific categories to target this week, like festive gifting, electronics, appliances)\n"
        f"### 3. ⚠️ Critical Vulnerabilities & Improvements\n"
        f"(Identify gaps in pricing, timing, or deal diversity)\n"
        f"### 4. 💡 Copywriting & Viral Hook Recommendations\n"
        f"(Suggest 3-4 punchy headline hooks and top trending hashtags e.g. #DiwaliLoot, #AmazonSteal)\n"
        f"### 5. 🚀 Actionable Growth Playbook to Reach 10,000+ Subscribers\n"
        f"(Concrete tactical steps the admin should take this week)\n"
    )

def run_growth_advisor(db_path: str = "data/state.sqlite3", output_path: str = "reports/GROWTH_DIGEST.md", notify_telegram: bool = False) -> str:
    """Run the complete Growth Advisor audit and output a strategic briefing."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("Initializing Autonomous AI Growth Advisor...")

    metrics = get_deal_metrics(db_path)
    sale_mode = os.getenv("SALE_MODE", "").strip().lower()

    copywriter = AICopywriter()
    provider_name = "Nous Hermes" if copywriter.provider in ["hermes", "nous-hermes", "openrouter"] else "Gemini AI"
    logger.info(f"Using AI Provider: {provider_name} ({copywriter.provider})")

    prompt = build_advisor_prompt(metrics, sale_mode)
    system_prompt = (
        "You are Nous Hermes, an elite autonomous e-commerce growth analyst. "
        "You deliver sharp, data-backed insights, constructive critiques, and high-ROI strategies for affiliate channels."
    )

    ai_analysis = copywriter.generate_text(prompt=prompt, system_prompt=system_prompt)
    if not ai_analysis:
        logger.warning("AI generation failed or not configured. Generating standard quantitative digest.")
        ai_analysis = (
            "### 1. 📊 Executive Performance Audit\n"
            f"- Total Deals: {metrics['total_deals']} | Active: {metrics['active_deals']} | Expired: {metrics['expired_deals']}\n"
            f"- Last 24 Hours: {metrics['deals_last_24h']} posts\n"
            "- Discount Quality: Good average margin across active inventory.\n\n"
            "### 2. 🎯 Recommendations\n"
            "- Continue focusing on Amazon India verified lightning deals.\n"
            "- Ensure festive sale mode is active during major festival sales."
        )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = (
        f"# 🧠 Autonomous DealDash AI Growth & Strategy Digest\n"
        f"*Generated on: {now_str} | Powered by: {provider_name}*\n\n"
        f"---\n\n"
        f"{ai_analysis}\n\n"
        f"---\n"
        f"*Autonomously generated by DealDash Growth Advisor.*"
    )

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report, encoding="utf-8")
    logger.info(f"Saved growth report to {out_file.resolve()}")

    if notify_telegram:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id and token != "replace_me":
            # Send condensed summary to Telegram
            condensed = (
                f"🧠 <b>DealDash AI Growth Briefing</b>\n"
                f"<i>Powered by {provider_name}</i>\n\n"
                f"📊 <b>Stats:</b> {metrics['total_deals']} deals logged ({metrics['deals_last_24h']} in 24h)\n"
                f"⚡ <b>Avg Discount:</b> {round(sum(metrics['discounts'])/len(metrics['discounts']), 1) if metrics['discounts'] else 0}% OFF\n"
                f"🪔 <b>Mode:</b> {sale_mode or 'Standard'}\n\n"
                f"📄 <i>Full strategic audit committed to reports/GROWTH_DIGEST.md</i>"
            )
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": condensed, "parse_mode": "HTML"},
                    timeout=10
                )
                logger.info(f"Delivered briefing notification to Telegram chat {chat_id}")
            except Exception as e:
                logger.warning(f"Failed to deliver Telegram notification: {e}")

    return report

if __name__ == "__main__":
    run_growth_advisor(notify_telegram=True)
