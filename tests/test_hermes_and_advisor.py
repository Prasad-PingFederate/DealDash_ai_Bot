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
    
    copywriter = AICopywriter()
    assert copywriter.provider == "hermes"
    assert copywriter.is_configured() is True
    assert copywriter.hermes_model == "nousresearch/hermes-3-llama-3.1-8b"

def test_hermes_craft_deal_copy(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "hermes")
    monkeypatch.setenv("HERMES_API_KEY", "test-hermes-token")
    
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
        assert "openrouter.ai" in called_url
        assert called_headers["Authorization"] == "Bearer test-hermes-token"

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
