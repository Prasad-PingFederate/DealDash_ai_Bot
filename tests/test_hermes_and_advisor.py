import os
import sqlite3
from unittest.mock import MagicMock, patch
from dealbot.models import Deal
from dealbot.ai_copywriter import AICopywriter
from dealbot.growth_advisor import get_deal_metrics, run_growth_advisor, build_advisor_prompt

def test_hermes_provider_configuration(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "hermes")
    monkeypatch.setenv("HERMES_API_KEY", "test-hermes-token")
    monkeypatch.setenv("HERMES_MODEL", "nousresearch/hermes-3-llama-3.1-8b")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    copywriter = AICopywriter()
    assert copywriter.provider == "hermes"
    assert copywriter.is_configured() is True
    # The old Llama default is rewritten. That route is not the backup.
    assert copywriter.hermes_model == "google/gemma-4-26b-a4b-it:free"

def test_hermes_craft_deal_copy(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "hermes")
    monkeypatch.setenv("HERMES_API_KEY", "test-hermes-token")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    
    deal = Deal("d1", "Amazon", "OnePlus Nord Buds", "https://amazon.in/dp/B01", current_price="₹1,499", original_price="₹2,999", discount_percent=50)
    copywriter = AICopywriter()
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": "🔥 <b>STEAL DEAL: OnePlus Nord Buds!</b>\n• 30h Battery Life\n• Dolby Atmos sound\n💰 <b>Deal Price:</b> ₹1,499 | ❌ <b>MRP:</b> <s>₹2,999</s> | ⚡ <b>Discount:</b> 50% OFF"
            }
        }]
    }
    
    with patch("requests.post", return_value=mock_resp) as mock_post:
        result = copywriter.craft_deal_copy(deal)
        assert result is not None
        assert "OnePlus Nord Buds" in result
        assert "₹1,499" in result
        
        # Verify call went to Hermes endpoint with OpenRouter headers
        called_url = mock_post.call_args[0][0]
        called_headers = mock_post.call_args[1]["headers"]
        called_payload = mock_post.call_args[1]["json"]
        assert "openrouter.ai" in called_url
        assert called_headers["Authorization"] == "Bearer test-hermes-token"
        assert called_payload["model"] == "google/gemma-4-26b-a4b-it:free"
        assert called_payload["reasoning"]["effort"] == "none"


def test_deals_agent_openrouter_secret_name(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DEALS_AGENT_OPENROUTER", "deals-agent-key")

    copywriter = AICopywriter()
    assert copywriter.is_configured() is True
    assert copywriter.hermes_api_key == "deals-agent-key"


def test_unsafe_backup_models_are_rewritten(monkeypatch):
    monkeypatch.setenv("HERMES_API_KEY", "test-hermes-token")
    for unsafe in ("google/gemma-4-31b-it:free", "openrouter/free", "nousresearch/hermes-3-llama-3.1-8b"):
        monkeypatch.setenv("HERMES_MODEL", unsafe)
        copywriter = AICopywriter()
        assert copywriter.hermes_model == "google/gemma-4-26b-a4b-it:free"

    monkeypatch.setenv("HERMES_MODEL", "meta-llama/llama-3.3-8b-instruct:free")
    custom = AICopywriter()
    assert custom.hermes_model == "meta-llama/llama-3.3-8b-instruct:free"


def test_gemini_quota_falls_back_to_gemma(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("HERMES_API_KEY", "openrouter-key")
    monkeypatch.delenv("HERMES_MODEL", raising=False)

    deal = Deal("d1", "Amazon", "OnePlus Nord Buds", "https://amazon.in/dp/B01", current_price="₹1,499")
    copywriter = AICopywriter()

    gemini_resp = MagicMock()
    gemini_resp.status_code = 429
    gemini_resp.text = '{"error":{"status":"RESOURCE_EXHAUSTED","message":"quota exceeded"}}'
    gemma_resp = MagicMock()
    gemma_resp.status_code = 200
    gemma_resp.json.return_value = {
        "choices": [{"message": {"content": "OnePlus Nord Buds drop to ₹1,499 today."}}]
    }

    with patch("requests.post", side_effect=[gemini_resp, gemma_resp]) as mock_post:
        result = copywriter.craft_deal_copy(deal)

    assert result is not None
    assert "₹1,499" in result
    assert copywriter.last_engine == "gemma"
    assert "generativelanguage.googleapis.com" in mock_post.call_args_list[0][0][0]
    backup_call = mock_post.call_args_list[1]
    assert "openrouter.ai" in backup_call[0][0]
    assert backup_call[1]["json"]["model"] == "google/gemma-4-26b-a4b-it:free"
    assert backup_call[1]["headers"]["Authorization"] == "Bearer openrouter-key"


def test_gemini_success_does_not_call_gemma(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("HERMES_API_KEY", "openrouter-key")
    copywriter = AICopywriter()
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01", current_price="₹899")

    gemini_resp = MagicMock()
    gemini_resp.status_code = 200
    gemini_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "Noise Buds are ₹899 right now."}]}}]
    }
    with patch("requests.post", return_value=gemini_resp) as mock_post:
        result = copywriter.craft_deal_copy(deal)

    assert "₹899" in result
    assert copywriter.last_engine == "gemini"
    assert mock_post.call_count == 1


def test_gemini_server_error_does_not_spend_gemma(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("HERMES_API_KEY", "openrouter-key")
    copywriter = AICopywriter()
    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01", current_price="₹899")

    gemini_resp = MagicMock(status_code=500, text="Internal Server Error")
    with patch("requests.post", return_value=gemini_resp) as mock_post:
        result = copywriter.craft_deal_copy(deal)

    assert result is None
    assert mock_post.call_count == 1


def test_empty_gemini_key_uses_gemma_directly(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("HERMES_API_KEY", "openrouter-key")
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    copywriter = AICopywriter()
    assert copywriter.is_configured() is True

    deal = Deal("d1", "Amazon", "Noise Buds", "https://amazon.in/dp/B01", current_price="₹899")
    gemma_resp = MagicMock()
    gemma_resp.status_code = 200
    gemma_resp.json.return_value = {
        "choices": [{"message": {"content": "Noise Buds are down to ₹899."}}]
    }
    with patch("requests.post", return_value=gemma_resp) as mock_post:
        result = copywriter.craft_deal_copy(deal)

    assert "₹899" in result
    assert mock_post.call_count == 1
    assert "openrouter.ai" in mock_post.call_args[0][0]
    assert copywriter.last_engine == "gemma"

def test_sale_mode_directives(monkeypatch):
    # 1. Diwali mode
    monkeypatch.setenv("SALE_MODE", "diwali")
    cw_diwali = AICopywriter()
    assert "DIWALI MEGA LOOT" in cw_diwali._get_sale_mode_directive()

    # 2. Great Indian Festival mode
    monkeypatch.setenv("SALE_MODE", "great_indian_festival")
    cw_gif = AICopywriter()
    assert "GREAT INDIAN FESTIVAL" in cw_gif._get_sale_mode_directive()

    # 3. Prime Day mode
    monkeypatch.setenv("SALE_MODE", "prime_day")
    cw_prime = AICopywriter()
    assert "PRIME DAY EXCLUSIVE" in cw_prime._get_sale_mode_directive()

def test_growth_advisor_metrics_and_execution(tmp_path):
    db_file = tmp_path / "test_state.sqlite3"
    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE posted_deals (
                deal_id TEXT PRIMARY KEY,
                retailer TEXT,
                caption TEXT,
                status TEXT DEFAULT 'active',
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO posted_deals (deal_id, retailer, caption, status)
            VALUES ('amz-1', 'Amazon', '🏷️ <b>Smart Watch</b>\n💰 Deal Price: ₹999 | ⚡ Discount: 65% OFF', 'active')
        """)
        conn.execute("""
            INSERT INTO posted_deals (deal_id, retailer, caption, status)
            VALUES ('amz-2', 'Amazon', '🏷️ <b>Bluetooth Speaker</b>\n💰 Deal Price: ₹499 | ⚡ Discount: 40% OFF', 'expired')
        """)

    metrics = get_deal_metrics(str(db_file))
    assert metrics["total_deals"] == 2
    assert metrics["active_deals"] == 1
    assert metrics["expired_deals"] == 1
    assert 65 in metrics["discounts"]
    assert 40 in metrics["discounts"]

    prompt = build_advisor_prompt(metrics, sale_mode="diwali")
    assert "Nous Hermes" in prompt
    assert "OOS Rate:" in prompt

    out_file = tmp_path / "GROWTH_DIGEST.md"
    report = run_growth_advisor(db_path=str(db_file), output_path=str(out_file))
    assert out_file.exists()
    assert "Autonomous DealDash AI Growth & Strategy Digest" in report
    assert "Executive Performance Audit" in report
