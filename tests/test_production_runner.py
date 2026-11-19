# tests/test_production_runner.py
"""Tests for production runner: lockfile, process concurrency guard, and admin alerting."""
import os
import sys
from unittest.mock import MagicMock, patch
import pytest

from scripts.production_runner import ProcessLock, is_process_running, send_admin_alert


def test_is_process_running():
    # Current process should always be running
    assert is_process_running(os.getpid()) is True
    # PID 0 or negative
    assert is_process_running(0) is False
    assert is_process_running(-1) is False
    # Non-existent large PID
    assert is_process_running(9999999) is False


def test_process_lock_acquire_and_release(tmp_path):
    lock_file = tmp_path / "test.lock"
    lock = ProcessLock(lock_file)

    # 1. Acquire
    assert lock.acquire() is True
    assert lock_file.exists()
    content = lock_file.read_text(encoding="utf-8")
    assert str(os.getpid()) in content

    # 2. Re-acquire by same process succeeds
    assert lock.acquire() is True

    # 3. Release
    lock.release()
    assert not lock_file.exists()


def test_process_lock_blocks_when_active(tmp_path, monkeypatch):
    lock_file = tmp_path / "active.lock"
    # Simulate existing active process with PID 12345
    lock_file.write_text("12345\n2026-11-19 23:00:00 IST\n", encoding="utf-8")

    # Mock is_process_running(12345) -> True
    monkeypatch.setattr("scripts.production_runner.is_process_running", lambda pid: pid == 12345)

    lock = ProcessLock(lock_file)
    assert lock.acquire() is False


def test_process_lock_reclaims_stale_lock(tmp_path, monkeypatch):
    lock_file = tmp_path / "stale.lock"
    # Simulate stale lock from dead PID 88888
    lock_file.write_text("88888\n2026-11-19 20:00:00 IST\n", encoding="utf-8")

    # Mock is_process_running(88888) -> False
    monkeypatch.setattr("scripts.production_runner.is_process_running", lambda pid: False)

    lock = ProcessLock(lock_file)
    assert lock.acquire() is True
    assert str(os.getpid()) in lock_file.read_text(encoding="utf-8")
    lock.release()


def test_send_admin_alert(monkeypatch):
    # Missing credentials -> safe fallback
    assert send_admin_alert("Test message", token=None, admin_chat_id=None) is False

    mock_resp = MagicMock(status_code=200)
    with patch("requests.post", return_value=mock_resp) as mock_post:
        ok = send_admin_alert(
            message="Database connection error",
            title="🚨 DealDash AI — Alert",
            token="fake_bot_token",
            admin_chat_id="12345678",
        )
        assert ok is True
        assert mock_post.called
        kwargs = mock_post.call_args[1]
        assert kwargs["json"]["chat_id"] == "12345678"
        assert "Database connection error" in kwargs["json"]["text"]
