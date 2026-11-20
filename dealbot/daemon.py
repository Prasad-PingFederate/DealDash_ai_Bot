# dealbot/daemon.py
"""24/7 Multi-Channel Ingestion & Autonomous Posting Daemon for DealDash_ai_Bot.
Features:
1. Concurrency Control: Single-instance cross-platform PID lockfile + SQLite checkpoint.
2. Pre-Posting Drift & Availability Check: Fast live stock and upward price drift verification before posting.
3. Multi-Channel Deal Sourcing: Autonomous parallel sourcing across Amazon, Flipkart, Aggregators, and Community channels.
4. Autonomous Pacing & Rate Limiting: Peak-hour priority queue (IST shopping windows) with anti-mute spacing.
5. Built-in HTTP Healthcheck Server: Serves /healthz and / status dashboard for container/cloud platforms.
6. Crash Alerting: Automatically notifies ADMIN_CHAT_ID on Telegram on unhandled critical exceptions.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

from dealbot.models import Deal, parse_rupees
from dealbot.publisher import StateStore, send_admin_note, send_telegram_deal
from dealbot.sources import collect_all_deals
from dealbot.profit_ranker import compute_expected_profit
from dealbot.alerts import AlertStore, match_and_dispatch_alerts
from dealbot.pacing import can_post_deal_now, get_recommended_sleep_seconds, is_within_active_hours
from dealbot.expiry_checker import enrich_amazon_deal, inspect_stock
from dealbot.main import deduplicate_deals_intra_cycle, sweep_expired_deals

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dealbot.daemon")

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_LOCK_PATH = ROOT_DIR / "data" / "dealbot_daemon.lock"
DEFAULT_DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT_DIR / "data" / "state.sqlite3")))


# ==============================================================================
# 1. Cross-Platform Single-Instance Process Lock
# ==============================================================================

def is_process_running(pid: int) -> bool:
    """Check if process with given PID is alive across Windows and Unix."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if process != 0:
                kernel32.CloseHandle(process)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class ProcessLock:
    """Manages cross-platform PID lockfile to ensure strict single-instance execution."""

    def __init__(self, lock_path: Optional[Path] = None):
        self.lock_path = lock_path or DEFAULT_LOCK_PATH
        self.acquired = False

    def acquire(self) -> bool:
        """Attempt to acquire exclusive runner lock. Reclaims stale locks automatically."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        current_pid = os.getpid()

        if self.lock_path.exists():
            try:
                content = self.lock_path.read_text(encoding="utf-8").strip()
                if content:
                    lines = content.splitlines()
                    existing_pid = int(lines[0].strip())
                    if is_process_running(existing_pid):
                        logger.warning(
                            f"[ProcessLock] Daemon process PID {existing_pid} is already running. Exiting to avoid overlap."
                        )
                        return False
                    logger.info(
                        f"[ProcessLock] Reclaiming stale lockfile from dead process PID {existing_pid}."
                    )
            except Exception as e:
                logger.debug(f"[ProcessLock] Reading existing lockfile failed: {e}. Recreating.")

        try:
            iso_now = datetime.now(IST).isoformat()
            self.lock_path.write_text(f"{current_pid}\n{iso_now}\n", encoding="utf-8")
            self.acquired = True
            logger.info(f"[ProcessLock] Acquired daemon lockfile for PID {current_pid} at {self.lock_path}")
            return True
        except Exception as e:
            logger.error(f"[ProcessLock] Failed writing lockfile: {e}")
            return False

    def release(self) -> None:
        """Release the lockfile safely."""
        if self.acquired and self.lock_path.exists():
            try:
                self.lock_path.unlink()
                self.acquired = False
                logger.info("[ProcessLock] Released daemon lockfile.")
            except Exception as e:
                logger.warning(f"[ProcessLock] Error releasing lockfile: {e}")

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


# ==============================================================================
# 2. Pre-Posting Live Check & Upward Price Drift Verification
# ==============================================================================

class PrePostingLiveChecker:
    """Performs live pre-flight checks right before posting a candidate deal:
    1. Stock availability: Positive check ensuring deal is not out of stock or removed.
    2. Price drift: Ensures current price has not drifted upward (> 15%) or discount collapsed.
    """

    def __init__(self, min_discount: int = 25, max_allowed_price_increase_ratio: float = 1.15):
        self.min_discount = min_discount
        self.max_drift_ratio = max_allowed_price_increase_ratio

    def check_deal(self, deal: Deal) -> Tuple[bool, str, Optional[int]]:
        """Verify deal live availability and price drift before publishing.
        Returns: (is_valid, reason, live_price)
        """
        if not deal or not deal.url:
            return False, "missing_url", None

        deal_p = parse_rupees(deal.current_price)
        mrp_p = parse_rupees(deal.original_price)

        # 1. Stock check
        stock = inspect_stock(deal.url, timeout=6)
        if stock == "out_of_stock":
            return False, "out_of_stock", None

        # 2. Live Price Check for Amazon
        if "amazon." in deal.url.lower():
            enrichment = enrich_amazon_deal(deal.url, timeout=6)
            if enrichment.get("stock_status") == "out_of_stock":
                return False, "amazon_oos", None

            live_p = enrichment.get("live_price")
            if live_p and live_p > 0 and deal_p and deal_p > 0:
                # Check upward price drift
                if live_p > int(deal_p * self.max_drift_ratio):
                    return False, f"price_drift_upward: ₹{deal_p:,} -> ₹{live_p:,}", live_p

                # Check if discount collapsed
                live_mrp = enrichment.get("mrp") or mrp_p
                if live_mrp and live_mrp > live_p:
                    calc_disc = int(round((live_mrp - live_p) / live_mrp * 100))
                    if calc_disc < self.min_discount:
                        return False, f"discount_too_low: {calc_disc}% < {self.min_discount}%", live_p

                # Update live verified price on deal
                deal.current_price = f"₹{live_p:,}"
                return True, "verified_live_price", live_p

        # For non-Amazon (Flipkart, etc.) fast GET check
        return True, "in_stock", deal_p


# ==============================================================================
# 3. Built-in Lightweight Healthcheck HTTP Server
# ==============================================================================

class HealthMetrics:
    """Holds live thread-safe daemon telemetry for healthchecks and monitoring."""
    def __init__(self):
        self.start_time = time.time()
        self.cycles_completed = 0
        self.deals_ingested = 0
        self.deals_posted = 0
        self.deals_synced = 0
        self.alerts_sent = 0
        self.last_cycle_time: Optional[str] = None
        self.last_error: Optional[str] = None
        self.is_running = True

    def to_dict(self) -> Dict[str, Any]:
        uptime_sec = int(time.time() - self.start_time)
        return {
            "status": "healthy" if self.is_running else "stopping",
            "uptime_seconds": uptime_sec,
            "uptime_human": str(timedelta(seconds=uptime_sec)),
            "cycles_completed": self.cycles_completed,
            "deals_ingested": self.deals_ingested,
            "deals_posted": self.deals_posted,
            "deals_synced": self.deals_synced,
            "alerts_sent": self.alerts_sent,
            "last_cycle_time": self.last_cycle_time,
            "last_error": self.last_error,
        }


_GLOBAL_METRICS = HealthMetrics()


class DaemonHealthHandler(BaseHTTPRequestHandler):
    """Responds to cloud and container healthchecks (/healthz and /)."""

    def do_GET(self):
        if self.path in ("/healthz", "/health"):
            data = _GLOBAL_METRICS.to_dict()
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/":
            data = _GLOBAL_METRICS.to_dict()
            html_content = f"""<!DOCTYPE html>
<html>
<head><title>DealDash AI Daemon Status</title><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc; padding: 24px;">
  <h2>⚡ DealDash AI Autonomous Daemon</h2>
  <p><b>Status:</b> <span style="color:#34d399;">● {data['status'].upper()}</span></p>
  <ul>
    <li><b>Uptime:</b> {data['uptime_human']} ({data['uptime_seconds']}s)</li>
    <li><b>Cycles Completed:</b> {data['cycles_completed']}</li>
    <li><b>Deals Ingested:</b> {data['deals_ingested']}</li>
    <li><b>Deals Posted:</b> {data['deals_posted']}</li>
    <li><b>Alerts Sent:</b> {data['alerts_sent']}</li>
    <li><b>Last Cycle:</b> {data['last_cycle_time'] or 'Initializing...'}</li>
  </ul>
</body>
</html>"""
            body = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy healthcheck access logs
        return


def start_healthcheck_server(port: int = 8080) -> Optional[HTTPServer]:
    """Start the lightweight healthcheck server in a background daemon thread."""
    try:
        server = HTTPServer(("0.0.0.0", port), DaemonHealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"[Healthcheck] Server listening on port {port} (/healthz)")
        return server
    except Exception as e:
        logger.warning(f"[Healthcheck] Failed starting server on port {port}: {e}")
        return None


# ==============================================================================
# 4. Remote BharatBazaro Sync Helper
# ==============================================================================

def sync_deal_to_remote_bharatbazaro(deal: Deal, score: float = 75.0) -> bool:
    """Publish high-Expected-Profit deal to BharatBazaro API endpoint."""
    sync_url = os.getenv("BHARATBAZARO_SYNC_URL", "").strip()
    secret = os.getenv("INTERNAL_SYNC_SECRET", "").strip()
    if not sync_url or not secret or secret == "replace_with_strong_internal_sync_secret":
        return False

    price_val = parse_rupees(deal.current_price) or 0
    mrp_val = parse_rupees(deal.original_price) or price_val
    disc_pct = deal.discount_percent or (int(round((mrp_val - price_val) / mrp_val * 100)) if mrp_val > price_val else 0)

    payload = {
        "canonical_id": getattr(deal, "canonical_product_id", None) or deal.id,
        "title": deal.title,
        "merchant": (deal.retailer or "Amazon").lower(),
        "category": deal.category or "electronics",
        "price": price_val,
        "mrp": mrp_val,
        "discount_pct": disc_pct,
        "bank_offer": deal.bank_offer,
        "coupon_code": deal.coupon_code,
        "affiliate_url": deal.url,
        "image_url": deal.image_url,
        "expected_payout": round(price_val * 0.05, 2),
        "score": score,
        "subid": f"bot_tg_{(deal.category or 'gen')[:8]}_{deal.id[:12]}",
    }

    try:
        resp = requests.post(
            sync_url,
            json=payload,
            headers={"X-Internal-Sync-Secret": secret, "Content-Type": "application/json"},
            timeout=8,
        )
        return resp.status_code in (200, 201)
    except Exception as e:
        logger.debug(f"BharatBazaro remote sync failed: {e}")
        return False


# ==============================================================================
# 5. Autonomous Daemon Engine
# ==============================================================================

class DealDaemon:
    """24/7 background worker managing multi-channel deal ingestion, ranking, pre-posting checks, and posting."""

    def __init__(
        self,
        poll_interval_sec: int = 300,
        dry_run: bool = False,
        store: Optional[StateStore] = None,
        alert_store: Optional[AlertStore] = None,
        lock_path: Optional[Path] = None,
        db_path: Optional[Path] = None,
    ):
        self.poll_interval = max(poll_interval_sec, 30)
        self.dry_run = dry_run
        db_file = db_path or DEFAULT_DB_PATH
        self.store = store or StateStore(str(db_file))
        self.alert_store = alert_store or AlertStore(str(db_file))
        self.lock = ProcessLock(lock_path)
        self.drift_checker = PrePostingLiveChecker(min_discount=int(os.getenv("MIN_DISCOUNT_PERCENT", "25")))
        self.running = False
        self._consecutive_errors = 0

    def run_cycle(self) -> Dict[str, Any]:
        """Execute one complete autonomous deal cycle."""
        cycle_result = {
            "ingested": 0,
            "posted": False,
            "deal_id": None,
            "alerts_dispatched": 0,
            "synced_bharatbazaro": False,
        }

        # 1. Multi-Channel Deal Ingestion
        deals = collect_all_deals()
        cycle_result["ingested"] = len(deals)
        _GLOBAL_METRICS.deals_ingested += len(deals)

        if not deals:
            logger.info("[Daemon Cycle] No deals collected this cycle.")
            return cycle_result

        # 2. Intra-cycle deduplication
        deduped = deduplicate_deals_intra_cycle(deals)

        # 3. Expected Profit Ranking
        ranked_deals: List[Tuple[Deal, float]] = []
        for d in deduped:
            p = parse_rupees(d.current_price) or 0
            if p <= 0:
                continue
            is_loot = bool(d.is_loot or (d.discount_percent and d.discount_percent >= 35))
            score = compute_expected_profit(
                deal_price=float(p),
                category=d.category,
                title=d.title,
                is_loot=is_loot,
            )
            if score > 0.0:
                ranked_deals.append((d, score))

        ranked_deals.sort(key=lambda item: item[1], reverse=True)
        logger.info(f"[Daemon Cycle] Ranked {len(ranked_deals)} deals by Expected Profit.")

        # 4. User Alert Matching & Instant DM Dispatch
        for d, score in ranked_deals[:10]:
            try:
                alert_count = match_and_dispatch_alerts(d, store=self.alert_store, dry_run=self.dry_run)
                cycle_result["alerts_dispatched"] += alert_count
                _GLOBAL_METRICS.alerts_sent += alert_count
            except Exception as e:
                logger.debug(f"[Alerts] Error matching alerts for {d.id}: {e}")

        # 5. Select Top Deal & Pre-Posting Drift Check
        posted_deal = None
        for candidate_deal, score in ranked_deals:
            # Check if deal already posted recently
            if self.store.is_posted(
                candidate_deal.id,
                canonical_product_id=getattr(candidate_deal, "canonical_product_id", None),
                current_price=parse_rupees(candidate_deal.current_price),
            ):
                continue

            # Run pre-posting live check
            is_valid, reason, live_price = self.drift_checker.check_deal(candidate_deal)
            if not is_valid:
                logger.info(f"[PrePostingCheck] Rejected candidate {candidate_deal.id}: {reason}")
                self.store.record_event(candidate_deal.id, f"skipped_{reason}")
                continue

            # Pacing and Active Hours Guard
            can_post, pacing_reason, _ = can_post_deal_now(
                candidate_deal,
                profit_score=score,
                db_path=self.store.db_path,
            )
            if not can_post:
                logger.info(f"[Pacing] {pacing_reason}; queuing {candidate_deal.id} (score={score:.1f})")
                continue

            # Publish deal to Telegram
            ok, msg_id, caption = send_telegram_deal(candidate_deal, dry_run=self.dry_run)
            if ok:
                posted_deal = candidate_deal
                cycle_result["posted"] = True
                cycle_result["deal_id"] = candidate_deal.id
                _GLOBAL_METRICS.deals_posted += 1
                logger.info(f"[Daemon Cycle] Successfully published top deal '{candidate_deal.id}' (msg_id={msg_id})")

                # Record in state store
                chat_id = os.getenv("TELEGRAM_CHAT_ID")
                price_num = getattr(candidate_deal, "effective_price", None) or parse_rupees(candidate_deal.current_price)
                try:
                    self.store.mark_posted(
                        candidate_deal.id,
                        candidate_deal.retailer,
                        chat_id=chat_id,
                        message_id=msg_id,
                        caption=caption,
                        canonical_product_id=getattr(candidate_deal, "canonical_product_id", None),
                        price=price_num,
                        category=candidate_deal.category,
                        agent_score=score,
                    )
                except Exception as e:
                    logger.debug(f"[Daemon] mark_posted error: {e}")

                # Sync to BharatBazaro Storefront
                synced = sync_deal_to_remote_bharatbazaro(candidate_deal, score=score)
                cycle_result["synced_bharatbazaro"] = synced
                if synced:
                    _GLOBAL_METRICS.deals_synced += 1

                # Multi-Channel Broadcast for Viral Loot Deals (WhatsApp & Twitter/X)
                try:
                    from dealbot.sales_engine.whatsapp_broadcaster import broadcast_loot_to_whatsapp
                    wa_ok, wa_reason = broadcast_loot_to_whatsapp(candidate_deal, dry_run=self.dry_run)
                    if wa_ok:
                        logger.info(f"[SalesEngine] Broadcasted deal {candidate_deal.id} to WhatsApp")
                except Exception as e:
                    logger.debug(f"[SalesEngine] WhatsApp broadcast skipped: {e}")

                try:
                    from dealbot.sales_engine.twitter_bot import post_to_twitter
                    tw_ok, tw_reason = post_to_twitter(candidate_deal, dry_run=self.dry_run)
                    if tw_ok:
                        logger.info(f"[SalesEngine] Broadcasted deal {candidate_deal.id} to Twitter/X")
                except Exception as e:
                    logger.debug(f"[SalesEngine] Twitter broadcast skipped: {e}")
                break

        # Check Daily Budget Loot Bundle window (09:15 IST breakfast shopping window)
        now_ist = datetime.now(IST)
        today_date = now_ist.strftime("%Y-%m-%d")
        try:
            last_bundle_date = self.store.get_meta("last_daily_bundle_date")
            if now_ist.hour == 9 and 10 <= now_ist.minute <= 45 and last_bundle_date != today_date:
                from dealbot.sales_engine.bundles import generate_budget_bundle, format_bundle_telegram_message
                bundle = generate_budget_bundle(deals, max_price=199, count=5)
                if bundle:
                    b_msg = format_bundle_telegram_message(bundle)
                    chat_id = os.getenv("TELEGRAM_CHAT_ID")
                    if chat_id and not self.dry_run:
                        from dealbot.publisher import safe_telegram_post
                        safe_telegram_post("sendMessage", {
                            "chat_id": chat_id,
                            "text": b_msg,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": False,
                        })
                    logger.info(f"[SalesEngine] Published Daily Budget Bundle ({len(bundle.deals)} deals under ₹199)")
                    self.store.set_meta("last_daily_bundle_date", today_date)
        except Exception as e:
            logger.debug(f"[SalesEngine] Daily bundle error: {e}")

        # 6. Periodic sweep for expired deals / price spikes
        try:
            sweep_expired_deals(self.store, dry_run=self.dry_run, max_checks=4)
        except Exception as e:
            logger.debug(f"[Sweep] Expiry sweep error: {e}")

        return cycle_result

    def start(self) -> None:
        """Start the 24/7 background polling loop with signal handling and failure alerts."""
        if not self.lock.acquire():
            logger.error("Another daemon instance is active. Exiting.")
            sys.exit(1)

        self.running = True

        def _signal_handler(signum, frame):
            logger.info(f"Received signal {signum}. Shutting down daemon gracefully...")
            self.running = False
            _GLOBAL_METRICS.is_running = False

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        logger.info(
            f"⚡ DealDash AI Daemon started (polling every {self.poll_interval}s, dry_run={self.dry_run})"
        )

        while self.running:
            try:
                cycle_res = self.run_cycle()
                _GLOBAL_METRICS.cycles_completed += 1
                _GLOBAL_METRICS.last_cycle_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
                self._consecutive_errors = 0
            except Exception as exc:
                self._consecutive_errors += 1
                _GLOBAL_METRICS.last_error = str(exc)
                logger.error(f"[Daemon Error] Cycle failed: {exc}", exc_info=True)
                self._notify_admin_crash(exc)

            if not self.running:
                break

            # Sleep until next cycle
            sleep_time = min(self.poll_interval, 900)
            if self._consecutive_errors > 0:
                sleep_time = min(15 * (2 ** min(self._consecutive_errors, 5)), 300)

            logger.info(f"[Daemon] Sleeping for {sleep_time}s before next cycle...")
            for _ in range(int(sleep_time)):
                if not self.running:
                    break
                time.sleep(1)

        self.lock.release()
        logger.info("DealDash AI Daemon shut down cleanly.")

    def _notify_admin_crash(self, exc: Exception) -> None:
        """Send emergency Telegram alert to ADMIN_CHAT_ID on unexpected critical crash."""
        admin_chat_id = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not admin_chat_id or not token or token == "replace_me":
            return

        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-800:]
        msg = (
            f"🚨 <b>DealDash AI Daemon Alert!</b>\n\n"
            f"<b>Error:</b> <code>{html.escape(str(exc))}</code>\n"
            f"<b>Consecutive Failures:</b> {self._consecutive_errors}\n"
            f"<b>Timestamp:</b> {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n\n"
            f"<b>Traceback:</b>\n<pre>{html.escape(tb_text)}</pre>"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": admin_chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception:
            pass


# ==============================================================================
# 6. CLI Entrypoint
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="DealDash AI 24/7 Autonomous Deal Daemon")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without sending live Telegram messages")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", "300")), help="Poll interval in seconds")
    parser.add_argument("--healthcheck-server", action="store_true", help="Start background HTTP healthcheck server on PORT")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")), help="Healthcheck server port")

    args = parser.parse_args()

    if args.healthcheck_server:
        start_healthcheck_server(port=args.port)

    daemon = DealDaemon(
        poll_interval_sec=args.interval,
        dry_run=args.dry_run or (os.getenv("DRY_RUN", "false").lower() == "true"),
    )

    if args.once:
        logger.info("Running single cycle (--once)...")
        res = daemon.run_cycle()
        print(json.dumps(res, indent=2))
    else:
        daemon.start()


if __name__ == "__main__":
    main()
