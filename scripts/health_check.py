#!/usr/bin/env python3
"""
Daily AI & System Health-Check Diagnostic Suite.
Tests:
  1. Gemini API (gemini-2.5-flash)
  2. Groq API (llama-3.3-70b-versatile)
  3. OpenRouter Gemma / Hermes fallback chain
  4. Typesafe AI Critic & HTML Sanitizer in-memory validation
  5. Deal Discovery Feeds (sources.py)
  6. Live Amazon.in Scraper (enrich_amazon_deal)
  7. SQLite Database Integrity (PRAGMA integrity_check)
  8. bharatbazaro.com Live JSON Feed Health
  9. Telegram Bot & Channel Connectivity

Outputs:
  - data/health.json
  - GitHub Actions Step Summary ($GITHUB_STEP_SUMMARY)
  - Telegram Admin Alert (optional via --notify-admin)
"""

import argparse
import html
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("health_check")


def run_check_gemini() -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    if not api_key or api_key == "replace_me":
        return {
            "name": "Gemini API",
            "status": "skipped",
            "model": model,
            "latency_ms": None,
            "detail": "GEMINI_API_KEY not configured",
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": 'Reply with JSON: {"status": "ok"}'}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 256,
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
        },
    }
    start = time.perf_counter()
    try:
        resp = requests.post(url, json=payload, timeout=12)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code == 200:
            return {
                "name": "Gemini API",
                "status": "ok",
                "model": model,
                "latency_ms": latency_ms,
                "detail": "HTTP 200 OK (Thinking tokens disabled)",
            }
        elif resp.status_code == 429:
            return {
                "name": "Gemini API",
                "status": "rate_limited",
                "model": model,
                "latency_ms": latency_ms,
                "detail": "HTTP 429 Quota Exceeded (Fallback active)",
            }
        else:
            return {
                "name": "Gemini API",
                "status": "failed",
                "model": model,
                "latency_ms": latency_ms,
                "detail": f"HTTP {resp.status_code}: {resp.text[:120]}",
            }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "Gemini API",
            "status": "error",
            "model": model,
            "latency_ms": latency_ms,
            "detail": str(e)[:120],
        }


def run_check_groq() -> Dict[str, Any]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
    if not api_key or api_key == "replace_me":
        return {
            "name": "Groq API",
            "status": "skipped",
            "model": model,
            "latency_ms": None,
            "detail": "GROQ_API_KEY not configured",
        }

    url = os.getenv("GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": 'Reply with JSON: {"status": "ok"}'}],
        "temperature": 0.1,
        "max_tokens": 128,
        "response_format": {"type": "json_object"},
    }
    start = time.perf_counter()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code == 200:
            return {
                "name": "Groq API",
                "status": "ok",
                "model": model,
                "latency_ms": latency_ms,
                "detail": "HTTP 200 OK",
            }
        else:
            return {
                "name": "Groq API",
                "status": "failed",
                "model": model,
                "latency_ms": latency_ms,
                "detail": f"HTTP {resp.status_code}: {resp.text[:120]}",
            }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "Groq API",
            "status": "error",
            "model": model,
            "latency_ms": latency_ms,
            "detail": str(e)[:120],
        }


def run_check_openrouter() -> Dict[str, Any]:
    api_key = (
        os.getenv("HERMES_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("DEALS_AGENT_OPENROUTER")
        or ""
    ).strip()
    primary_model = os.getenv("HERMES_MODEL", "google/gemma-4-26b-a4b-it:free").strip()
    if not api_key or api_key == "replace_me":
        return {
            "name": "OpenRouter Fallback",
            "status": "skipped",
            "model": primary_model,
            "latency_ms": None,
            "detail": "HERMES_API_KEY not configured",
        }

    candidate_models = [
        primary_model,
        "google/gemma-3-27b-it:free",
        "nousresearch/hermes-3-llama-3.1-405b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openrouter/free",
    ]
    url = os.getenv("HERMES_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Prasad-PingFederate/DealDash_ai_Bot",
    }

    start = time.perf_counter()
    for m in candidate_models:
        payload = {
            "model": m,
            "models": candidate_models,
            "messages": [{"role": "user", "content": 'Reply with JSON: {"status": "ok"}'}],
            "max_tokens": 128,
        }
        if "gemma" in m.lower():
            payload["reasoning"] = {"effort": "none", "exclude": True}

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=12)
            if resp.status_code == 200:
                latency_ms = int((time.perf_counter() - start) * 1000)
                return {
                    "name": "OpenRouter Fallback",
                    "status": "ok",
                    "model": m,
                    "latency_ms": latency_ms,
                    "detail": f"HTTP 200 OK via {m}",
                }
        except Exception:
            continue

    latency_ms = int((time.perf_counter() - start) * 1000)
    return {
        "name": "OpenRouter Fallback",
        "status": "failed",
        "model": primary_model,
        "latency_ms": latency_ms,
        "detail": "All candidate models in fallback chain failed",
    }


def run_check_critic_and_sanitizer() -> Dict[str, Any]:
    from dealbot.models import Deal
    from dealbot.ai_copywriter import validate_critic, ensure_valid_telegram_html

    sample_deal = Deal(
        id="amz-B0HEALTH01",
        retailer="Amazon",
        title="Sony WH-1000XM5 Wireless Headphones",
        url="https://www.amazon.in/dp/B0HEALTH01",
        current_price="₹24,990",
        original_price="₹34,990",
        discount_percent=29,
        coupon_code="SONY2000",
        bank_offer="Instant Discount on HDFC Cards",
    )

    passed_count = 0
    total_count = 4

    # 1. Valid caption with Indian commas
    valid_copy = "Grab Sony WH-1000XM5 at deal price ₹24,990 with HDFC Bank instant offer!"
    if validate_critic(valid_copy, sample_deal):
        passed_count += 1

    # 2. Hallucinated price (₹999 instead of ₹24,990) must be rejected
    fake_price_copy = "Insane steal deal at just ₹999 on Amazon right now!"
    if not validate_critic(fake_price_copy, sample_deal):
        passed_count += 1

    # 3. Hallucinated bank offer (ICICI instead of HDFC) must be rejected
    fake_bank_copy = "Buy Sony WH-1000XM5 for ₹24,990 with ICICI Bank 10% instant discount!"
    if not validate_critic(fake_bank_copy, sample_deal):
        passed_count += 1

    # 4. Unclosed/unsupported HTML auto-repair & sanitizer
    unclosed_html = "<b>Sony ANC Headphones <br> <span>Deal Price ₹24,990"
    repaired = ensure_valid_telegram_html(unclosed_html)
    if repaired.endswith("</b>") and "<span>" not in repaired and "<br>" not in repaired:
        passed_count += 1

    all_passed = (passed_count == total_count)
    return {
        "name": "Typesafe Critic & Sanitizer",
        "status": "ok" if all_passed else "failed",
        "assertions_passed": f"{passed_count}/{total_count}",
        "latency_ms": 1,
        "detail": "All 4 safety assertions passed (Anti-hallucination + Tag Auto-Repair)"
        if all_passed
        else f"Failed {total_count - passed_count} assertions",
    }


def run_check_feeds() -> Dict[str, Any]:
    from dealbot.sources import fetch_live_feed_deals, fetch_reddit_deals

    start = time.perf_counter()
    candidates = []
    try:
        candidates = fetch_live_feed_deals(min_discount=20)
    except Exception as e:
        logger.debug(f"Live feeds check warning: {e}")

    if not candidates:
        try:
            candidates = fetch_reddit_deals(min_discount=20)
        except Exception as e:
            logger.debug(f"Reddit check warning: {e}")

    latency_ms = int((time.perf_counter() - start) * 1000)
    count = len(candidates)
    return {
        "name": "Deal Discovery Feeds",
        "status": "ok" if count > 0 else "warning",
        "candidate_count": count,
        "latency_ms": latency_ms,
        "detail": f"Discovered {count} candidates across live feeds"
        if count > 0
        else "Feeds reachable but 0 candidates qualified discount filter",
    }


def run_check_amazon_scraper() -> Dict[str, Any]:
    from dealbot.expiry_checker import enrich_amazon_deal

    asin = "B09K7RLCPL"  # Philips Air Fryer
    url = f"https://www.amazon.in/dp/{asin}"
    start = time.perf_counter()
    try:
        data = enrich_amazon_deal(url, timeout=10)
        latency_ms = int((time.perf_counter() - start) * 1000)
        status = data.get("stock_status", "unknown")
        live_price = data.get("live_price")
        return {
            "name": "Amazon.in Scraper",
            "status": "ok" if status in ("in_stock", "out_of_stock") else "warning",
            "stock_status": status,
            "live_price": f"₹{live_price:,}" if live_price else "N/A",
            "latency_ms": latency_ms,
            "detail": f"Status: {status}, Price: ₹{live_price or 'N/A'}",
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "Amazon.in Scraper",
            "status": "error",
            "stock_status": "error",
            "latency_ms": latency_ms,
            "detail": str(e)[:120],
        }


def run_check_database() -> Dict[str, Any]:
    db_candidates = [
        PROJECT_ROOT / "data" / "state.sqlite3",
        PROJECT_ROOT / "data" / "deals.db",
    ]
    target_db = next((p for p in db_candidates if p.exists()), None)
    if not target_db:
        return {
            "name": "SQLite Integrity",
            "status": "warning",
            "active_deals": 0,
            "latency_ms": 0,
            "detail": "Database file not yet created",
        }

    start = time.perf_counter()
    try:
        conn = sqlite3.connect(str(target_db))
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        integrity = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM posted_deals WHERE status = 'active';")
        active_count = cur.fetchone()[0]
        conn.close()
        latency_ms = int((time.perf_counter() - start) * 1000)

        is_ok = (integrity == "ok")
        return {
            "name": "SQLite Integrity",
            "status": "ok" if is_ok else "failed",
            "active_deals": active_count,
            "integrity": integrity,
            "latency_ms": latency_ms,
            "detail": f"PRAGMA: {integrity}, Active Deals: {active_count}",
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "SQLite Integrity",
            "status": "error",
            "latency_ms": latency_ms,
            "detail": str(e)[:120],
        }


def run_check_bharatbazaro_live() -> Dict[str, Any]:
    url = "https://bharatbazaro.com/data/stats.json"
    start = time.perf_counter()
    try:
        resp = requests.get(url, timeout=10)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            total_deals = data.get("total_live_deals", 0)
            epoch = data.get("last_updated_epoch", 0)
            age_min = int((time.time() - epoch) / 60) if epoch else 0
            return {
                "name": "BharatBazaro Live",
                "status": "ok" if total_deals > 0 else "warning",
                "live_deals": total_deals,
                "feed_age_min": age_min,
                "latency_ms": latency_ms,
                "detail": f"{total_deals} live deals online (synced {age_min}m ago)",
            }
        else:
            return {
                "name": "BharatBazaro Live",
                "status": "warning",
                "latency_ms": latency_ms,
                "detail": f"HTTP {resp.status_code}",
            }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "BharatBazaro Live",
            "status": "warning",
            "latency_ms": latency_ms,
            "detail": f"Feed endpoint unreachable: {e}"[:100],
        }


def run_check_telegram() -> Dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or token == "replace_me":
        return {
            "name": "Telegram Bot API",
            "status": "skipped",
            "latency_ms": None,
            "detail": "TELEGRAM_BOT_TOKEN not configured",
        }

    start = time.perf_counter()
    try:
        r_me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if r_me.status_code == 200:
            me_data = r_me.json().get("result", {})
            bot_name = me_data.get("username", "UnknownBot")
            chat_title = ""
            if chat_id:
                try:
                    r_chat = requests.get(
                        f"https://api.telegram.org/bot{token}/getChat",
                        params={"chat_id": chat_id},
                        timeout=8,
                    )
                    if r_chat.status_code == 200:
                        chat_title = r_chat.json().get("result", {}).get("title", "")
                except Exception:
                    pass
            detail = f"@{bot_name}"
            if chat_title:
                detail += f" -> {chat_title}"
            return {
                "name": "Telegram Bot API",
                "status": "ok",
                "bot_username": bot_name,
                "latency_ms": latency_ms,
                "detail": detail,
            }
        else:
            return {
                "name": "Telegram Bot API",
                "status": "failed",
                "latency_ms": latency_ms,
                "detail": f"HTTP {r_me.status_code}: {r_me.text[:100]}",
            }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "Telegram Bot API",
            "status": "error",
            "latency_ms": latency_ms,
            "detail": str(e)[:120],
        }


def format_markdown_summary(results: List[Dict[str, Any]], overall_status: str) -> str:
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S IST")
    status_icon = "🟢 Healthy" if overall_status == "HEALTHY" else ("🟡 Degraded" if overall_status == "DEGRADED" else "🔴 Unhealthy")
    lines = [
        f"### 🏥 Daily AI & System Health Scorecard: {status_icon}",
        f"**Timestamp:** `{now_ist}`",
        "",
        "| Component | Status | Latency | Details |",
        "| :--- | :---: | :---: | :--- |",
    ]
    status_icons = {
        "ok": "🟢 OK",
        "skipped": "⚪ Skipped",
        "warning": "🟡 Warning",
        "rate_limited": "🟡 Rate-Limited",
        "failed": "🔴 Failed",
        "error": "🔴 Error",
    }
    for check in results:
        icon = status_icons.get(check.get("status"), "⚪")
        lat = f"{check['latency_ms']} ms" if check.get("latency_ms") is not None else "--"
        det = check.get("detail", "")
        lines.append(f"| **{check['name']}** | {icon} | `{lat}` | {det} |")
    return "\n".join(lines)


def format_telegram_alert(results: List[Dict[str, Any]], overall_status: str) -> str:
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M IST")
    header_mark = "🟢" if overall_status == "HEALTHY" else ("🟡" if overall_status == "DEGRADED" else "🔴")
    lines = [
        f"{header_mark} <b>Daily System & AI Health Scorecard</b>",
        f"⏱️ <code>{now_ist}</code>",
        "",
    ]
    status_icons = {
        "ok": "✅",
        "skipped": "⚪",
        "warning": "⚠️",
        "rate_limited": "⏳",
        "failed": "❌",
        "error": "❌",
    }
    for c in results:
        icon = status_icons.get(c.get("status"), "•")
        lat_str = f" ({c['latency_ms']}ms)" if c.get("latency_ms") is not None else ""
        lines.append(f"{icon} <b>{html.escape(c['name'])}</b>{lat_str}")
        lines.append(f"   <i>{html.escape(str(c.get('detail', '')))}</i>")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run 9-point system & AI health checks.")
    parser.add_argument("--notify-admin", action="store_true", help="Send health scorecard to ADMIN_CHAT_ID")
    parser.add_argument("--notify-telegram", action="store_true", help="Alias for --notify-admin")
    args = parser.parse_args()

    logger.info("Starting 9-point AI & System Health Diagnostics...")
    checks = [
        run_check_gemini(),
        run_check_groq(),
        run_check_openrouter(),
        run_check_critic_and_sanitizer(),
        run_check_feeds(),
        run_check_amazon_scraper(),
        run_check_database(),
        run_check_bharatbazaro_live(),
        run_check_telegram(),
    ]

    has_critical_failure = any(
        c["name"] in ("Typesafe Critic & Sanitizer", "SQLite Integrity") and c["status"] in ("failed", "error")
        for c in checks
    )
    all_ai_failed = all(
        c["status"] in ("failed", "error")
        for c in checks
        if c["name"] in ("Gemini API", "Groq API", "OpenRouter Fallback")
    )
    if has_critical_failure or all_ai_failed:
        overall_status = "UNHEALTHY"
    elif any(c["status"] in ("warning", "rate_limited", "failed", "error") for c in checks):
        overall_status = "DEGRADED"
    else:
        overall_status = "HEALTHY"

    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    health_payload = {
        "timestamp_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "timestamp_epoch": int(time.time()),
        "overall_status": overall_status,
        "checks": checks,
    }

    out_dir = PROJECT_ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    health_file = out_dir / "health.json"
    with open(health_file, "w", encoding="utf-8") as f:
        json.dump(health_payload, f, indent=2)
    logger.info("Health telemetry written to %s", health_file)

    md_summary = format_markdown_summary(checks, overall_status)
    print("\n" + md_summary + "\n")

    step_summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        try:
            with open(step_summary_path, "a", encoding="utf-8") as f:
                f.write("\n" + md_summary + "\n")
            logger.info("Appended health summary to $GITHUB_STEP_SUMMARY")
        except Exception as e:
            logger.warning("Could not write to GITHUB_STEP_SUMMARY: %s", e)

    if args.notify_admin or args.notify_telegram:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        admin_id = os.getenv("ADMIN_CHAT_ID", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and token != "replace_me" and admin_id:
            try:
                tg_msg = format_telegram_alert(checks, overall_status)
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": admin_id,
                        "text": tg_msg,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10,
                )
                logger.info("Sent health alert to Telegram admin chat %s", admin_id)
            except Exception as e:
                logger.warning("Failed to send Telegram admin health alert: %s", e)

    if overall_status == "UNHEALTHY":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
