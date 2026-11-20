# tests/test_daemon_and_scrapers.py
"""Comprehensive unit tests for:
1. Pluggable deal scrapers (Amazon, Flipkart, Aggregators, Community channel scraper).
2. DealScraperRegistry parallel collection, error isolation, and deduplication.
3. PrePostingLiveChecker (stock verification, upward price drift, and discount collapse guards).
4. ProcessLock (single-instance concurrency control and stale lock recovery).
5. Daemon healthcheck HTTP endpoint (/healthz and /).
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import requests

from dealbot.daemon import (
    DaemonHealthHandler,
    DealDaemon,
    PrePostingLiveChecker,
    ProcessLock,
    start_healthcheck_server,
)
from dealbot.models import Deal
from dealbot.scrapers import (
    AggregatorDealScraper,
    AmazonDealScraper,
    CommunityChannelScraper,
    DealScraperRegistry,
    FlipkartDealScraper,
    get_default_registry,
)


# ==============================================================================
# 1. Scraper Tests (Amazon, Flipkart, Aggregators, Community)
# ==============================================================================

def test_amazon_scraper_parses_deals_and_preserves_variants():
    scraper = AmazonDealScraper()

    mock_html = """
    <html><body>
      <a href="/Samsung-Galaxy-Ultra/dp/B0BS1QCFHX?th=1&psc=1&smid=A1XYZ">Samsung Deal</a>
      <a href="/Apple-MacBook-Air/dp/B0CX234567">MacBook Deal</a>
    </body></html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch.object(scraper, "fetch_url", return_value=mock_resp):
        deals = scraper.scrape(min_discount=20)

    assert len(deals) >= 2
    samsung_deal = next((d for d in deals if "B0BS1QCFHX" in d.id), None)
    assert samsung_deal is not None
    assert samsung_deal.retailer == "Amazon"
    assert "th=1" in samsung_deal.url
    assert "psc=1" in samsung_deal.url
    assert "ascsubtag=bot_tg_" in samsung_deal.url


def test_flipkart_scraper_parses_deals_and_preserves_pid_lid():
    scraper = FlipkartDealScraper()

    mock_html = """
    <html><body>
      <a href="/sony-wh-1000xm5/p/itm123456?pid=ACCELEC123456789&lid=LSTACCELEC123456789">Sony Headphones</a>
      <a href="/puma-running-shoes/p/itm789012?pid=SHOE123456789012">Puma Shoes</a>
    </body></html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch.object(scraper, "fetch_url", return_value=mock_resp):
        deals = scraper.scrape(min_discount=20)

    assert len(deals) >= 2
    sony_deal = next((d for d in deals if "ACCELEC123456789" in d.id), None)
    assert sony_deal is not None
    assert sony_deal.retailer == "Flipkart"
    assert "pid=ACCELEC123456789" in sony_deal.url
    assert "lid=LSTACCELEC123456789" in sony_deal.url


def test_aggregator_scraper_cuelinks_and_earnkaro(monkeypatch):
    monkeypatch.setenv("CUELINKS_API_TOKEN", "valid_test_token")
    monkeypatch.setenv("CUELINKS_PUB_ID", "12345")
    scraper = AggregatorDealScraper()

    mock_cuelinks_json = {
        "deals": [
            {
                "id": 101,
                "title": "Myntra Roadster T-Shirt (60% OFF)",
                "url": "https://www.myntra.com/tshirt/roadster",
                "merchant_name": "Myntra",
                "category_name": "fashion",
                "mrp": 999,
                "deal_price": 399,
                "discount_percent": 60,
                "image_url": "https://assets.myntra.com/img.jpg",
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_cuelinks_json

    with patch("requests.get", return_value=mock_resp):
        deals = scraper.scrape(min_discount=25)

    assert len(deals) == 1
    deal = deals[0]
    assert deal.retailer == "Myntra"
    assert "roadster" in deal.title.lower()
    assert deal.discount_percent == 60


def test_aggregator_scraper_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("CUELINKS_API_TOKEN", raising=False)
    monkeypatch.delenv("CUELINKS_PUB_ID", raising=False)
    monkeypatch.delenv("EARNKARO_API_TOKEN", raising=False)
    monkeypatch.delenv("EARNKARO_ID", raising=False)

    scraper = AggregatorDealScraper()
    deals = scraper.scrape(min_discount=20)
    assert deals == []


def test_community_channel_scraper_filtering(monkeypatch):
    scraper = CommunityChannelScraper()

    # Public telegram web preview format
    mock_html = """
    <html><body>
      <div class="tgme_widget_message_text">🔥 Sony WH-1000XM5 Noise Cancelling Headphones at ₹19,999 (MRP ₹29,999)!\nGrab deal now: https://amzn.to/3xyz123</div>
      <div class="tgme_widget_message_text">❌ Amazon Pay ₹1000 Gift Card Offer!\nBuy here: https://www.amazon.in/dp/B000GIFTCARD?tag=competitor-21</div>
    </body></html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    with patch.object(scraper, "fetch_url", return_value=mock_resp), \
         patch("dealbot.scrapers.community.unshorten_url", side_effect=lambda u, **kwargs: "https://www.amazon.in/dp/B0BS1QCFHX?tag=competitor-21" if "amzn.to" in u else u):
        deals = scraper.scrape(min_discount=20)

    # Gift card must be rejected by compute_expected_profit (0% commission)
    assert any("B0BS1QCFHX" in d.id for d in deals)
    assert not any("GIFTCARD" in d.id for d in deals)
    # Competitor tag must be stripped and replaced with our SubID
    sony = next(d for d in deals if "B0BS1QCFHX" in d.id)
    assert "competitor-21" not in sony.url
    assert "ascsubtag=bot_tg_" in sony.url


# ==============================================================================
# 2. Registry Parallel Execution & Error Isolation Tests
# ==============================================================================

def test_registry_parallel_execution_and_error_isolation():
    reg = DealScraperRegistry()

    # Scraper 1: Returns a valid deal
    s1 = MagicMock()
    s1.name = "scraper1"
    s1.enabled = True
    d1 = Deal(id="amz-1", retailer="Amazon", title="Deal 1", url="https://www.amazon.in/dp/B001", current_price="₹999")
    s1.safe_scrape.return_value = [d1]

    # Scraper 2: Raises an exception
    s2 = MagicMock()
    s2.name = "scraper2"
    s2.enabled = True
    s2.safe_scrape.side_effect = RuntimeError("Scraper 2 network failure")

    # Scraper 3: Returns duplicate deal with extra metadata
    s3 = MagicMock()
    s3.name = "scraper3"
    s3.enabled = True
    d3 = Deal(id="amz-1", retailer="Amazon", title="Deal 1 Enhanced", url="https://www.amazon.in/dp/B001", image_url="https://img.com/1.jpg")
    s3.safe_scrape.return_value = [d3]

    reg.register(s1)
    reg.register(s2)
    reg.register(s3)

    collected = reg.collect_all(min_discount=20, max_workers=2)

    # Error in s2 did not crash registry; duplicate deals deduplicated into 1
    assert len(collected) == 1
    assert collected[0].id == "amz-1"
    assert collected[0].image_url == "https://img.com/1.jpg"
    assert collected[0].source_count >= 1


# ==============================================================================
# 3. Pre-Posting Live Check & Price Drift Tests
# ==============================================================================

def test_pre_posting_check_in_stock_pass():
    checker = PrePostingLiveChecker(min_discount=25)
    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        current_price="₹19,999",
        original_price="₹29,999",
        discount_percent=33,
    )

    with patch("dealbot.daemon.inspect_stock", return_value="in_stock"), \
         patch("dealbot.daemon.enrich_amazon_deal", return_value={"stock_status": "in_stock", "live_price": 19999, "mrp": 29999}):
        valid, reason, live_p = checker.check_deal(deal)

    assert valid is True
    assert reason == "verified_live_price"
    assert live_p == 19999


def test_pre_posting_check_rejects_out_of_stock():
    checker = PrePostingLiveChecker(min_discount=25)
    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        current_price="₹19,999",
    )

    with patch("dealbot.daemon.inspect_stock", return_value="out_of_stock"):
        valid, reason, _ = checker.check_deal(deal)

    assert valid is False
    assert reason == "out_of_stock"


def test_pre_posting_check_rejects_upward_price_drift():
    checker = PrePostingLiveChecker(min_discount=25, max_allowed_price_increase_ratio=1.15)
    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        current_price="₹10,000",
        original_price="₹20,000",
        discount_percent=50,
    )

    # Price jumped to ₹15,000 (50% increase, exceeds 15% drift allowance)
    with patch("dealbot.daemon.inspect_stock", return_value="in_stock"), \
         patch("dealbot.daemon.enrich_amazon_deal", return_value={"stock_status": "in_stock", "live_price": 15000, "mrp": 20000}):
        valid, reason, live_p = checker.check_deal(deal)

    assert valid is False
    assert "price_drift_upward" in reason
    assert live_p == 15000


def test_pre_posting_check_rejects_collapsed_discount():
    checker = PrePostingLiveChecker(min_discount=25)
    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        current_price="₹10,000",
        original_price="₹20,000",
        discount_percent=50,
    )

    # Current price jumped to ₹18,000 (10% discount on ₹20,000 MRP, lower than 25% minimum)
    with patch("dealbot.daemon.inspect_stock", return_value="in_stock"), \
         patch("dealbot.daemon.enrich_amazon_deal", return_value={"stock_status": "in_stock", "live_price": 18000, "mrp": 20000}):
        valid, reason, live_p = checker.check_deal(deal)

    assert valid is False
    assert "price_drift_upward" in reason or "discount_too_low" in reason


# ==============================================================================
# 4. ProcessLock Concurrency Tests
# ==============================================================================

def test_process_lock_acquisition_and_stale_recovery(tmp_path):
    lock_file = tmp_path / "daemon_test.lock"

    # 1. Acquire first lock
    lock1 = ProcessLock(lock_file)
    assert lock1.acquire() is True
    assert lock_file.exists()

    # 2. Second acquire with same PID (already running) returns False
    lock2 = ProcessLock(lock_file)
    with patch("dealbot.daemon.is_process_running", return_value=True):
        assert lock2.acquire() is False

    # 3. If existing PID is dead, reclaim stale lock returns True
    with patch("dealbot.daemon.is_process_running", return_value=False):
        assert lock2.acquire() is True

    # 4. Release removes the file
    lock2.release()
    assert not lock_file.exists()


# ==============================================================================
# 5. Healthcheck HTTP Server Tests
# ==============================================================================

def test_healthcheck_endpoint():
    server = start_healthcheck_server(port=8899)
    try:
        resp = requests.get("http://127.0.0.1:8899/healthz", timeout=3)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "uptime_seconds" in data
        assert "cycles_completed" in data

        resp_root = requests.get("http://127.0.0.1:8899/", timeout=3)
        assert resp_root.status_code == 200
        assert "DealDash AI" in resp_root.text
    finally:
        if server:
            server.shutdown()
