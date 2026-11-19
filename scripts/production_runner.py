#!/usr/bin/env python3
"""24/7 Production Runner & Resilient Process Supervisor for DealDash_ai_Bot.

Features:
1. Cross-Platform Lockfile:
   - data/dealbot_runner.lock stores PID and ISO timestamp.
   - Prevents overlapping execution from cron jobs, containers, or manual commands.
   - Automatically reclaims stale locks from terminated processes.
2. Auto-Restart & Supervision:
   - Continuous loop with adaptive quiet-hours sleep (IST 23:30 - 08:30).
   - Exponential backoff on consecutive exceptions with automatic recovery.
3. Failure & Recovery Alerting:
   - Sends emergency alerts to ADMIN_CHAT_ID via Telegram Bot API on critical failures.
   - Dispatches recovery notification when the runner recovers.
4. Clean Signal Handling:
   - Catches SIGINT / SIGTERM to checkpoint SQLite state and cleanly release the lockfile.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dealbot.production_runner")

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_LOCK_PATH = ROOT_DIR / "data" / "dealbot_runner.lock"


def is_process_running(pid: int) -> bool:
    """Check if process with given PID is alive."""
    if pid <= 0:
        return False
    # Windows
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
            # Fallback
            try:
                import subprocess
                out = subprocess.check_output(f'tasklist /FI "PID eq {pid}"', shell=True, text=True)
                return str(pid) in out
            except Exception:
                return False
    # Unix / Linux / macOS
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class ProcessLock:
    """Manages cross-platform PID lockfile."""

    def __init__(self, lock_path: Optional[Path] = None):
        self.lock_path = lock_path or DEFAULT_LOCK_PATH
        self.acquired = False

    def acquire(self) -> bool:
        """Attempt to acquire exclusive runner lock."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        current_pid = os.getpid()

        if self.lock_path.exists():
            try:
                content = self.lock_path.read_text(encoding="utf-8").strip()
                lines = content.splitlines()
                existing_pid = int(lines[0].strip()) if lines else 0
                lock_time = lines[1].strip() if len(lines) > 1 else "unknown"

                if existing_pid == current_pid:
                    self.acquired = True
                    return True

                if is_process_running(existing_pid):
                    logger.warning(
                        "[Lock] Another DealBot runner (PID %s, started %s) is currently active. "
                        "Exiting to prevent overlapping execution.",
                        existing_pid,
                        lock_time,
                    )
                    return False
                else:
                    logger.info("[Lock] Found stale lock from dead PID %s. Reclaiming lock.", existing_pid)
            except Exception as err:
                logger.warning("[Lock] Could not read existing lockfile: %s. Overwriting.", err)

        # Write new lockfile
        try:
            now_iso = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
            self.lock_path.write_text(f"{current_pid}\n{now_iso}\n", encoding="utf-8")
            self.acquired = True
            logger.info("[Lock] Acquired runner lock for PID %s (%s)", current_pid, self.lock_path)
            return True
        except Exception as exc:
            logger.error("[Lock] Failed to create lockfile: %s", exc)
            return False

    def release(self) -> None:
        """Release the runner lockfile if owned by this process."""
        if not self.acquired:
            return
        try:
            if self.lock_path.exists():
                content = self.lock_path.read_text(encoding="utf-8").strip()
                lines = content.splitlines()
                if lines and int(lines[0].strip()) == os.getpid():
                    self.lock_path.unlink(missing_ok=True)
                    logger.info("[Lock] Released runner lock for PID %s", os.getpid())
        except Exception as err:
            logger.warning("[Lock] Error releasing lockfile: %s", err)
        finally:
            self.acquired = False


def send_admin_alert(
    message: str,
    title: str = "🚨 DealDash AI — Alert",
    token: Optional[str] = None,
    admin_chat_id: Optional[str] = None,
) -> bool:
    """Send immediate notification to ADMIN_CHAT_ID."""
    bot_token = (token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    admin_id = (admin_chat_id or os.getenv("ADMIN_CHAT_ID") or "").strip()

    if not bot_token or bot_token == "replace_me" or not admin_id:
        logger.info("[Admin Alert (Offline)] %s:\n%s", title, message)
        return False

    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    text = (
        f"<b>{html.escape(title)}</b>\n"
        f"<i>Timestamp: {now_ist}</i>\n\n"
        f"{message}"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": admin_id,
                "text": text[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except requests.RequestException as e:
        logger.warning("[Admin Alert] Failed to dispatch alert: %s", e)
        return False


def run_cycle_supervised() -> int:
    """Run a single deal processing cycle with full exception safety."""
    from dealbot.main import run_once
    return run_once()


def production_runner(
    continuous: bool = True,
    poll_minutes: int = 30,
    lock_path: Optional[Path] = None,
) -> int:
    """Supervised production loop with lockfile, auto-restart, and alerting."""
    lock = ProcessLock(lock_path)
    if not lock.acquire():
        return 0

    consecutive_errors = 0
    had_previous_failure = False

    def handle_exit(signum: int, frame: Any) -> None:
        logger.info("[Supervisor] Received signal %s. Shutting down gracefully...", signum)
        # Checkpoint SQLite
        try:
            from dealbot.publisher import StateStore
            from dealbot.session import database_path
            StateStore(str(database_path())).checkpoint()
        except Exception:
            pass
        lock.release()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    logger.info("=========================================================")
    logger.info("DealDash AI Production Runner Started (PID %s)", os.getpid())
    logger.info("Continuous: %s | Poll Interval: %s mins", continuous, poll_minutes)
    logger.info("=========================================================")

    try:
        while True:
            cycle_start = time.time()
            try:
                posted_count = run_cycle_supervised()
                logger.info("[Supervisor] Cycle finished in %.1fs. Posted: %s", time.time() - cycle_start, posted_count)

                if had_previous_failure:
                    # Notify admin of recovery
                    send_admin_alert(
                        message="DealBot successfully recovered and completed a clean posting cycle.",
                        title="✅ DealDash AI — Service Recovered",
                    )
                    had_previous_failure = False

                consecutive_errors = 0

            except Exception as exc:
                consecutive_errors += 1
                had_previous_failure = True
                tb = traceback.format_exc()
                logger.error("[Supervisor] Uncaught cycle error (#%s):\n%s", consecutive_errors, tb)

                # Send emergency alert to admin
                error_snippet = html.escape(str(exc)[:250])
                tb_snippet = html.escape(tb[-800:])
                send_admin_alert(
                    message=(
                        f"⚠️ <b>Cycle Exception (#{consecutive_errors}):</b>\n"
                        f"<code>{error_snippet}</code>\n\n"
                        f"<b>Traceback:</b>\n<pre>{tb_snippet}</pre>\n\n"
                        f"<i>Supervisor auto-restarting in next interval...</i>"
                    ),
                    title="🚨 DealDash AI — Cycle Failure Alert",
                )

            if not continuous:
                break

            # Adaptive sleep
            try:
                from dealbot.pacing import get_recommended_sleep_seconds
                sleep_sec = get_recommended_sleep_seconds(poll_minutes=poll_minutes)
            except Exception:
                sleep_sec = poll_minutes * 60

            # Exponential backoff if failing
            if consecutive_errors > 0:
                sleep_sec = min(sleep_sec, max(30, 30 * (2 ** min(consecutive_errors, 4))))

            logger.info("[Supervisor] Sleeping for %s seconds before next cycle...", int(sleep_sec))

            # Interruptible sleep
            slept = 0
            while slept < sleep_sec:
                chunk = min(5, sleep_sec - slept)
                time.sleep(chunk)
                slept += chunk

    finally:
        lock.release()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DealDash AI 24/7 Production Runner")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--continuous", action="store_true", default=True, help="Run continuously in supervisor loop")
    parser.add_argument("--poll-interval", type=int, default=30, help="Poll interval in minutes (default: 30)")
    parser.add_argument("--status", action="store_true", help="Check if runner is currently active")
    parser.add_argument("--release-lock", action="store_true", help="Force release existing lockfile")

    args = parser.parse_args()

    if args.status:
        if DEFAULT_LOCK_PATH.exists():
            content = DEFAULT_LOCK_PATH.read_text(encoding="utf-8").strip()
            lines = content.splitlines()
            pid = int(lines[0]) if lines else 0
            running = is_process_running(pid)
            print(f"Lockfile exists at {DEFAULT_LOCK_PATH}")
            print(f"PID: {pid} | Active: {running} | Started: {lines[1] if len(lines) > 1 else 'unknown'}")
        else:
            print("No active runner lockfile found. Service is idle.")
        return 0

    if args.release_lock:
        if DEFAULT_LOCK_PATH.exists():
            DEFAULT_LOCK_PATH.unlink()
            print("Runner lockfile successfully removed.")
        else:
            print("No lockfile to remove.")
        return 0

    continuous_mode = not args.once
    return production_runner(continuous=continuous_mode, poll_minutes=args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
