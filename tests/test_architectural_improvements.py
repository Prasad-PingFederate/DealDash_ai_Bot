import time
from unittest.mock import MagicMock, patch
import pytest
import requests

from dealbot.models import Deal
from dealbot.sources import expand_and_convert_url, unroll_shortlink, SHORTLINK_CACHE
from dealbot.expiry_checker import inspect_stock
from dealbot.publisher import StateStore, send_telegram_deal, safe_telegram_post
from dealbot.ai_copywriter import AICopywriter, validate_critic


# ── 1. SHORTLINK UNROLLING & AMAZON CANONICAL REBUILDING ──────────────────────

def test_shortlink_unrolling_and_amazon_canonical(monkeypatch):
    monkeypatch.setenv("AMAZON_AFFILIATE_TAG", "dammaiprasad-21")
    SHORTLINK_CACHE.clear()

    # Mock shortlink HTTP redirect
    mock_resp = MagicMock()
    mock_resp.status_code = 301
    mock_resp.url = "https://www.amazon.in/dp/B0BS1QCFHX?ref_=cm_sw_r_cp_ud_dp_123&psc=1"

    with patch("requests.head", return_value=mock_resp) as mock_head:
        canonical_url, deal_id, retailer = expand_and_convert_url("https://amzn.to/3Yxyz")
        assert mock_head.call_count == 1
        assert canonical_url == "https://www.amazon.in/dp/B0BS1QCFHX?tag=dammaiprasad-21&linkCode=ll1"
        assert deal_id == "amz-B0BS1QCFHX"
        assert retailer == "Amazon"

    # Second call should hit in-memory cache without extra network request
    with patch("requests.head") as mock_head2:
        canonical_url2, _, _ = expand_and_convert_url("https://amzn.to/3Yxyz")
        assert mock_head2.call_count == 0
        assert canonical_url2 == canonical_url


# ── 2. FLIPKART URL PARAMETER CLEANING & AFFID INJECTION ────────────────────

def test_flipkart_canonicalization_and_cleaning(monkeypatch):
    monkeypatch.setenv("FLIPKART_AFFILIATE_ID", "myflipkartid")

    dirty_fk_url = (
        "https://www.flipkart.com/item/p/itm123"
        "?pid=MOBGH76XYZ&otracker=hp_deals&cmpid=sem_123"
        "&affid=bad_upstream_affid&affExtParam1=xyz&ssid=sess123"
    )

    clean_url, deal_id, retailer = expand_and_convert_url(dirty_fk_url)
    assert retailer == "Flipkart"
    assert deal_id == "fk-MOBGH76XYZ"
    assert "pid=MOBGH76XYZ" in clean_url
    assert "affid=myflipkartid" in clean_url
    assert "bad_upstream_affid" not in clean_url
    assert "otracker=" not in clean_url
    assert "cmpid=" not in clean_url
    assert "ssid=" not in clean_url


# ── 3. OOS WATCHER: CAPTCHA VS TRUE OOS DOM ─────────────────────────────────

def test_oos_watcher_captcha_returns_unknown():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>Enter the characters you see below: <form id='validateCaptcha'></form></body></html>"

    with patch("requests.get", return_value=mock_resp):
        status = inspect_stock("https://www.amazon.in/dp/B0BS1QCFHX")
        assert status == "unknown"

    # Also test soft-block 403 / 503
    mock_403 = MagicMock()
    mock_403.status_code = 403
    mock_403.text = "Forbidden"
    with patch("requests.get", return_value=mock_403):
        assert inspect_stock("https://www.amazon.in/dp/B0BS1QCFHX") == "unknown"


def test_oos_watcher_true_unavailable_returns_out_of_stock():
    # Amazon positive OOS DOM signal
    mock_amz_oos = MagicMock()
    mock_amz_oos.status_code = 200
    mock_amz_oos.text = "<div id='availability'><span class='a-color-price'>Currently unavailable.</span></div>"

    with patch("requests.get", return_value=mock_amz_oos):
        status = inspect_stock("https://www.amazon.in/dp/B0BS1QCFHX")
        assert status == "out_of_stock"

    # Flipkart positive OOS DOM signal
    mock_fk_oos = MagicMock()
    mock_fk_oos.status_code = 200
    mock_fk_oos.text = "<div class='_16PBlm'><span class='_1V3wum'>Sold Out</span></div>"

    with patch("requests.get", return_value=mock_fk_oos):
        status = inspect_stock("https://www.flipkart.com/item/p/itm123?pid=XYZ")
        assert status == "out_of_stock"


# ── 4. CANONICAL ASIN DEDUPLICATION + 15% PRICE-DROP RE-TRIGGER ─────────────

def test_canonical_asin_dedup_and_price_drop(tmp_path):
    db_file = tmp_path / "test_dedup.db"
    store = StateStore(str(db_file))

    canon_id = "amazon:B0EXAMPLE1"
    deal_id = "amz-B0EXAMPLE1"

    # 1. First post at ₹10,000
    assert store.is_posted(deal_id, canonical_product_id=canon_id, current_price=10000) is False
    store.mark_posted(
        deal_id=deal_id,
        retailer="Amazon",
        canonical_product_id=canon_id,
        price=10000,
        chat_id="-100",
        message_id=1,
    )

    # 2. Check duplicate at same price -> should be True (blocked)
    assert store.is_posted("different-feed-id", canonical_product_id=canon_id, current_price=10000) is True

    # 3. Check duplicate with only 5% drop (₹9,500) -> should be True (blocked)
    assert store.is_posted("different-feed-id", canonical_product_id=canon_id, current_price=9500) is True

    # 4. Check with 16% drop (₹8,400 <= 10000 * 0.85) -> should be False (re-triggered!)
    assert store.is_posted("different-feed-id", canonical_product_id=canon_id, current_price=8400) is False


# ── 5. TELEGRAM SENDPHOTO BROKEN FALLBACK & 429 RETRY ─────────────────────────

def test_telegram_broken_image_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456789")

    deal = Deal(
        id="amz-B0BS1QCFHX",
        retailer="Amazon",
        title="Sony Wireless Noise Cancelling Headphones",
        url="https://www.amazon.in/dp/B0BS1QCFHX",
        current_price="₹1,499",
        image_url="https://broken-image.com/notfound.jpg",
    )

    photo_fail = MagicMock()
    photo_fail.status_code = 400
    photo_fail.text = '{"ok":false,"error_code":400,"description":"Bad Request: failed to get HTTP URL content"}'

    msg_ok = MagicMock()
    msg_ok.status_code = 200
    msg_ok.json.return_value = {"ok": True, "result": {"message_id": 999}}

    with patch("dealbot.publisher.safe_telegram_post", side_effect=[photo_fail, msg_ok]) as mock_post:
        ok, msg_id, caption = send_telegram_deal(deal, dry_run=False)
        assert ok is True
        assert msg_id == 999
        assert mock_post.call_count == 2
        # Verify inline keyboard has Price History for Amazon ASIN
        sent_payload = mock_post.call_args_list[1][1]["payload"]
        keyboard = sent_payload["reply_markup"]["inline_keyboard"]
        assert any("Price History" in btn["text"] for row in keyboard for btn in row)
        assert any("Grab Deal @ ₹1,499" in btn["text"] for row in keyboard for btn in row)


def test_telegram_429_retry_handling():
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.json.return_value = {"ok": False, "parameters": {"retry_after": 1}}

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"ok": True}

    with patch("requests.post", side_effect=[resp_429, resp_200]) as mock_req, patch("time.sleep") as mock_sleep:
        res = safe_telegram_post("https://api.telegram.org/bot123/sendMessage", payload={"text": "hi"})
        assert res.status_code == 200
        assert mock_req.call_count == 2
        mock_sleep.assert_called_once_with(2)  # retry_after (1) + 1


# ── 6. GROQ FALLBACK & CRITIC HALLUCINATION CHECK ────────────────────────────

def test_groq_secondary_fallback(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    copywriter = AICopywriter()
    assert copywriter.is_configured() is True

    groq_resp = MagicMock()
    groq_resp.status_code = 200
    groq_resp.json.return_value = {
        "choices": [{"message": {"content": "Grab OnePlus Nord Buds at ₹1,499 today!"}}]
    }

    with patch("requests.post", return_value=groq_resp) as mock_post:
        res = copywriter.generate_text("Prompt")
        assert "₹1,499" in res
        assert copywriter.last_engine == "groq"
        assert "groq.com" in mock_post.call_args[0][0]


def test_critic_strict_hallucination_validation():
    deal = Deal(
        id="amz-B01",
        retailer="Amazon",
        title="Noise Buds",
        url="https://amazon.in/dp/B01",
        current_price="₹899",
        original_price="₹1,999",
        discount_percent=55,
        coupon_code="SAVE50",
    )

    # 1. Valid copy (correct price, discount, coupon)
    valid_copy = "Get Noise Buds for ₹899 with 55% off using coupon SAVE50!"
    assert validate_critic(valid_copy, deal) is True

    # 2. Hallucinated price (₹299 instead of ₹899)
    bad_price_copy = "Steal deal on Noise Buds for ₹299!"
    assert validate_critic(bad_price_copy, deal) is False

    # 3. Missing price digits
    no_price_copy = "Great deal on Noise Buds!"
    assert validate_critic(no_price_copy, deal) is False

    # 4. Hallucinated discount (90% instead of 55%)
    bad_disc_copy = "Noise Buds at ₹899 with 90% discount!"
    assert validate_critic(bad_disc_copy, deal) is False

    # 5. Hallucinated coupon code (FAKECODE)
    bad_coupon_copy = "Noise Buds at ₹899 using coupon FAKECODE!"
    assert validate_critic(bad_coupon_copy, deal) is False
