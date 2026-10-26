import requests
from unittest.mock import MagicMock, patch
from dealbot.models import Deal
from dealbot.publisher import format_caption, send_telegram_deal
from dealbot.ai_copywriter import AICopywriter, sanitize_telegram_html

# ── NEGATIVE TEST 1: Unconfigured / Missing API Key ──────────────────────────
def test_ai_copywriter_unconfigured():
    copywriter = AICopywriter(api_key="")
    assert not copywriter.is_configured()
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01", current_price="₹899")
    # Must immediately return None and not throw
    res = copywriter.craft_deal_copy(deal)
    assert res is None
    # format_caption must fall back smoothly
    caption = format_caption(deal, copywriter=copywriter)
    assert "Noise Buds" in caption
    assert "₹899" in caption

# ── NEGATIVE TEST 2: API 400 Bad Request / Invalid Key ──────────────────────
def test_ai_copywriter_invalid_key_400():
    copywriter = AICopywriter(api_key="invalid-api-key")
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01")
    
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"error": {"message": "API key not valid"}}'
    
    with patch("requests.post", return_value=mock_resp):
        res = copywriter.craft_deal_copy(deal)
        assert res is None
        # Format caption still works via fallback
        caption = format_caption(deal, copywriter=copywriter)
        assert "Noise Buds" in caption

# ── NEGATIVE TEST 3: API 429 Rate Limit ─────────────────────────────────────
def test_ai_copywriter_rate_limit_429():
    copywriter = AICopywriter(api_key="valid-key")
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01")
    
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = '{"error": {"message": "Resource has been exhausted (e.g. check quota)."}}'
    
    with patch("requests.post", return_value=mock_resp):
        res = copywriter.craft_deal_copy(deal)
        assert res is None

# ── NEGATIVE TEST 4: API 500 / 503 Internal Server Error ────────────────────
def test_ai_copywriter_server_error_500():
    copywriter = AICopywriter(api_key="valid-key")
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01")
    
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    
    with patch("requests.post", return_value=mock_resp):
        res = copywriter.craft_deal_copy(deal)
        assert res is None

# ── NEGATIVE TEST 5: Network Timeout & Connection Exception ─────────────────
def test_ai_copywriter_timeout_and_network_failure():
    copywriter = AICopywriter(api_key="valid-key")
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01")
    
    with patch("requests.post", side_effect=requests.Timeout("Connection timed out")):
        res = copywriter.craft_deal_copy(deal)
        assert res is None

    with patch("requests.post", side_effect=requests.ConnectionError("DNS failure")):
        res = copywriter.craft_deal_copy(deal)
        assert res is None

# ── NEGATIVE TEST 6: Malformed / Empty LLM JSON Payload ─────────────────────
def test_ai_copywriter_malformed_llm_response():
    copywriter = AICopywriter(api_key="valid-key")
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01")
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Empty candidates or missing parts
    mock_resp.json.return_value = {"candidates": []}
    with patch("requests.post", return_value=mock_resp):
        assert copywriter.craft_deal_copy(deal) is None

    mock_resp.json.return_value = {"candidates": [{"content": {"parts": []}}]}
    with patch("requests.post", return_value=mock_resp):
        assert copywriter.craft_deal_copy(deal) is None

# ── NEGATIVE TEST 7: Telegram HTML Sanitizer & Injection Defense ────────────
def test_sanitize_telegram_html_injection_and_unclosed():
    # 1. Malicious script tag injection
    malicious = "<script>alert('xss')</script><b>Bold Text</b>"
    cleaned = sanitize_telegram_html(malicious)
    assert "<script>" not in cleaned
    assert "&lt;script&gt;" in cleaned
    assert "<b>Bold Text</b>" in cleaned

    # 2. Unclosed tags: <b><i>text without closing
    unclosed = "<b><i>Headline text"
    cleaned = sanitize_telegram_html(unclosed)
    assert cleaned.endswith("</i></b>")

    # 3. Stray angle brackets (< and >)
    strays = "Price < ₹1000 & discount > 50%"
    cleaned = sanitize_telegram_html(strays)
    assert "&lt;" in cleaned
    assert "&gt;" in cleaned
    assert "&amp;" in cleaned

    # 4. Markdown conversion fallback
    markdown_text = "**Super Deal!** *Best price*"
    cleaned = sanitize_telegram_html(markdown_text)
    assert "<b>Super Deal!</b>" in cleaned
    assert "<i>Best price</i>" in cleaned

# ── NEGATIVE TEST 8: Deal with Missing / None Fields ─────────────────────────
def test_deal_with_all_none_prices():
    deal = Deal("none-deal", "Amazon", "No Price Deal", "https://amazon.in/dp/B099")
    caption = format_caption(deal)
    assert "No Price Deal" in caption
    assert "Deal Price:" not in caption
    assert "MRP:" not in caption
    assert "Discount:" not in caption
    assert "Buy Now on Amazon" in caption

# ── NEGATIVE TEST 9: Deal with Massive 10,000 Character Title ───────────────
def test_deal_with_huge_title():
    huge_title = "A" * 10000
    deal = Deal("huge", "Flipkart", huge_title, "https://flipkart.com/p/123")
    caption = format_caption(deal)
    assert len(caption) >= 10000
    assert "Buy Now on Flipkart" in caption

# ── NEGATIVE TEST 10: Telegram sendPhoto 400 Fallback to sendMessage ────────
def test_publisher_send_photo_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    
    deal = Deal("d1", "Amazon", "Test Deal", "https://amazon.in/dp/123", image_url="https://bad-image.com/fake.jpg")
    
    photo_resp = MagicMock(status_code=400, text='{"description": "wrong file identifier/HTTP URL specified"}')
    msg_resp = MagicMock(status_code=200, text='{"ok": true}')
    
    def mock_post(url, *args, **kwargs):
        if "sendPhoto" in url:
            return photo_resp
        elif "sendMessage" in url:
            return msg_resp
        return MagicMock(status_code=500)
        
    with patch("requests.post", side_effect=mock_post):
        success, msg_id, caption = send_telegram_deal(deal, dry_run=False)
        assert success is True

# ── NEGATIVE TEST 11: Telegram Complete Network Failure ─────────────────────
def test_publisher_handles_network_exception(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    
    deal = Deal("d1", "Amazon", "Test Deal", "https://amazon.in/dp/123")
    with patch("requests.post", side_effect=requests.ConnectionError("Internet down")):
        success, msg_id, caption = send_telegram_deal(deal, dry_run=False)
        assert success is False


# ── NEGATIVE TEST 12: Stress Test expand_and_convert_url with Null & Malformed Input ───
def test_expand_and_convert_url_break_attempts(monkeypatch):
    from dealbot.sources import expand_and_convert_url
    monkeypatch.setenv("AMAZON_PARTNER_TAG", "mytesttag-21")

    # None input
    url, did, ret = expand_and_convert_url(None)
    assert url == ""
    assert did == "deal-invalid"

    # Empty string input
    url, did, ret = expand_and_convert_url("")
    assert url == ""
    assert did == "deal-invalid"

    # Whitespace only
    url, did, ret = expand_and_convert_url("   ")
    assert url == ""

    # Missing http/https prefix
    url, did, ret = expand_and_convert_url("amazon.in/dp/B0CYLJR6MF")
    assert "https://amazon.in/dp/B0CYLJR6MF?tag=mytesttag-21" == url
    assert did == "amz-B0CYLJR6MF"
    assert ret == "Amazon"

# ── NEGATIVE TEST 13: Stress Test clean_title with Null & Weird Strings ─────────────────
def test_clean_title_break_attempts():
    from dealbot.sources import clean_title
    # None
    assert clean_title(None) == "Special Deal"
    # Empty
    assert clean_title("") == "Special Deal"
    # Whitespace only
    assert clean_title("     ") == "Special Deal"
    # Only coupon tag
    assert clean_title("[Apply 50% Coupon]") == "Special Deal"

# ── NEGATIVE TEST 14: Guard Against Telegram > 4000 Character Captions ──────────────────
def test_publisher_caption_over_4000_chars_guard(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")

    # Create a deal with 5,000 characters
    massive_title = "Massive Deal Title " * 300
    deal = Deal("big-1", "Amazon", massive_title, "https://amazon.in/dp/123")

    posted_payload = {}
    def mock_post(url, *args, **kwargs):
        nonlocal posted_payload
        posted_payload = kwargs.get("json", {})
        return MagicMock(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 12345}})

    with patch("requests.post", side_effect=mock_post):
        success, msg_id, caption = send_telegram_deal(deal, dry_run=False)
        assert success is True
        # Must be trimmed under 4000 chars
        assert len(posted_payload.get("text", "")) <= 4000

# ── NEGATIVE TEST 15: Growth Advisor Corrupted Database Recovery ────────────────────────
def test_growth_advisor_corrupted_database(tmp_path):
    from dealbot.growth_advisor import get_deal_metrics
    corrupted_db = tmp_path / "corrupted.sqlite3"
    # Write invalid binary junk
    corrupted_db.write_bytes(b"CORRUPTED_NOT_A_SQLITE_DATABASE_JUNK_HEADER_123456789")

    metrics = get_deal_metrics(str(corrupted_db))
    # Should safely return fallback metrics with 0 deals, not crash
    assert metrics["total_deals"] == 0
    assert metrics["active_deals"] == 0

# ── NEGATIVE TEST 16: Hermes API 401 Unauthorized & Malformed JSON ──────────────────────
def test_hermes_unauthorized_and_malformed_json(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "hermes")
    monkeypatch.setenv("HERMES_API_KEY", "invalid_hermes_key")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    cw = AICopywriter()
    deal = Deal("d1", "Amazon", "Echo Dot", "https://amazon.in/dp/B01")

    # 401 Unauthorized
    mock_resp = MagicMock(status_code=401, text="Unauthorized: invalid api key")
    with patch("requests.post", return_value=mock_resp):
        res = cw.craft_deal_copy(deal)
        assert res is None  # Gracefully falls back to None

    # 200 OK but malformed empty choices
    mock_resp = MagicMock(status_code=200, json=lambda: {"choices": []})
    with patch("requests.post", return_value=mock_resp):
        res = cw.craft_deal_copy(deal)
        assert res is None
