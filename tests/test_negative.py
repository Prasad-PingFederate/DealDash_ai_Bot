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
