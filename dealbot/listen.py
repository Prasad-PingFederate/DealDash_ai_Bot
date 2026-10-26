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
        "allowed_updates": '["callback_query","message_reaction_count"]',
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
