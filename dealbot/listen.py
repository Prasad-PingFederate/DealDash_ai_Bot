"""Collect the reactions Telegram will actually tell a bot about.

Buy-button clicks and forwards are not included. Telegram does not send those.
"""

import logging
import os
import time
from pathlib import Path

import requests

from dealbot.publisher import StateStore

logger = logging.getLogger(__name__)


def _store_from_env() -> StateStore:
    project_root = Path(__file__).resolve().parent.parent
    db_env = os.getenv("DATABASE_PATH", "data/state.sqlite3")
    db_path = Path(db_env)
    if not db_path.is_absolute():
        db_path = project_root / db_path
    return StateStore(str(db_path))


def drain_updates(store: StateStore, token: str) -> int:
    """Read queued Telegram updates once. Returns how many we understood."""
    if not token or token == "replace_me":
        return 0
    params = {
        "timeout": 0,
        "allowed_updates": '["message","callback_query","message_reaction_count"]',
    }
    offset = store.get_meta("update_offset")
    if offset:
        params["offset"] = offset
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params=params,
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning("Could not read Telegram updates: %s", exc)
        return 0
    if response.status_code != 200:
        logger.warning("getUpdates returned %s", response.status_code)
        return 0

    updates = response.json().get("result") or []
    handled = 0
    last_id = None
    for update in updates:
        last_id = update.get("update_id", last_id)

        # 1. Message Command Handling (/alert, /myalerts, /delalert, /admin_stats)
        msg = update.get("message") or {}
        text = str(msg.get("text") or "").strip()
        from_user = msg.get("from") or {}
        user_id = str(from_user.get("id") or "")
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or user_id)

        if text.startswith("/"):
            admin_chat_id = os.getenv("ADMIN_CHAT_ID", "").strip()
            if text.lower().startswith("/admin_stats"):
                if (admin_chat_id and (str(chat_id) == admin_chat_id or str(user_id) == admin_chat_id)) or not admin_chat_id:
                    from scripts.revenue_digest import get_revenue_digest, format_revenue_digest_markdown
                    digest = get_revenue_digest(store.db_path)
                    reply_text = format_revenue_digest_markdown(digest)
                else:
                    reply_text = "⛔ <i>Unauthorized: This command is restricted to the channel administrator.</i>"
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": reply_text, "parse_mode": "HTML"},
                        timeout=10,
                    )
                    handled += 1
                except requests.RequestException:
                    pass

            elif any(text.lower().startswith(c) for c in ("/start", "/alert", "/myalerts", "/delalert")):
                from dealbot.alerts import AlertStore, handle_user_command
                alert_store = AlertStore(store.db_path)
                reply = handle_user_command(text, user_id=user_id, chat_id=chat_id, store=alert_store)
                if reply:
                    msg_text = reply[0] if isinstance(reply, tuple) else reply
                    reply_markup = reply[1] if isinstance(reply, tuple) else None
                    payload = {"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"}
                    if reply_markup:
                        payload["reply_markup"] = reply_markup
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json=payload,
                            timeout=10,
                        )
                        handled += 1
                    except requests.RequestException:
                        pass

        # 2. Callback Queries
        callback = update.get("callback_query") or {}
        data = str(callback.get("data") or "")
        query_id = callback.get("id")
        if data.startswith("worth:"):
            deal_id = data.split(":", 1)[1].strip()[:40]
            if deal_id:
                store.add_worth_tap(deal_id)
                handled += 1
        if query_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                    json={"callback_query_id": query_id, "text": "Noted" if data.startswith("worth:") else ""},
                    timeout=10,
                )
            except requests.RequestException:
                logger.warning("Could not answer a button tap.")
        reaction = update.get("message_reaction_count") or {}
        if reaction:
            message_id = reaction.get("message_id")
            total = sum(int(item.get("total_count") or 0) for item in reaction.get("reactions") or [])
            deal_id = store.deal_id_for_message(message_id) if message_id is not None else None
            if deal_id:
                store.set_reaction_count(deal_id, total)
                handled += 1
    if last_id is not None:
        store.set_meta("update_offset", str(int(last_id) + 1))
    return handled


def listen(seconds: int, store: StateStore = None) -> int:
    """Poll until the queue is quiet, for up to `seconds`. Used by the always-on container."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if store is None:
        store = _store_from_env()
    end = time.time() + max(0, int(seconds))
    seen = 0
    while True:
        if token and token != "replace_me":
            seen += drain_updates(store, token)
        remaining = end - time.time()
        if remaining <= 0:
            return seen
        time.sleep(min(20, remaining))
