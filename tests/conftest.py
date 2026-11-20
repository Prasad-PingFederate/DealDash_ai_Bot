# tests/conftest.py
"""Global pytest fixtures for DealDash_ai_Bot test suite."""
import os
import pytest


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    """Ensure daytime posting hours simulation and disable production API calls in tests."""
    monkeypatch.setenv("TELEGRAM_24X7", "true")
