import pytest
from dealbot.models import Deal
from dealbot.agent import validate_typesafe_ai_batch
from dealbot.ai_copywriter import ensure_valid_telegram_html, validate_critic, AICopywriter


def test_validate_typesafe_ai_batch_from_fenced_markdown():
    raw_markdown = """```json
    {
      "posts": [
        {"id": "amz-B012345678", "why": "Strong price drop", "copy": "Steal deal on headphones at ₹1,499!"},
        {"id": "amz-B087654321", "why": "Loot deal", "copy": "Lowest price ever at ₹299!"}
      ]
    }
    ```"""
    results = validate_typesafe_ai_batch(raw_markdown)
    assert len(results) == 2
    assert results[0]["id"] == "amz-B012345678"
    assert "₹1,499" in results[0]["copy"]
    assert results[1]["id"] == "amz-B087654321"


def test_validate_typesafe_ai_batch_from_top_level_list():
    raw_list = [
        {"id": "amz-B0LIST0001", "why": "Top brand", "copy": "Puma sneakers for ₹999"},
        {"id": "amz-B0LIST0002", "why": "50% off", "copy": "Boat speaker for ₹1,299"}
    ]
    results = validate_typesafe_ai_batch(raw_list)
    assert len(results) == 2
    assert results[0]["id"] == "amz-B0LIST0001"
    assert results[1]["id"] == "amz-B0LIST0002"


def test_validate_typesafe_ai_batch_alternative_dict_keys():
    # Supports "deals", "items", "results"
    for key in ("deals", "items", "results", "data"):
        raw_dict = {
            key: [{"id": f"amz-{key}", "why": "Good deal", "copy": "Test copy ₹499"}]
        }
        res = validate_typesafe_ai_batch(raw_dict)
        assert len(res) == 1
        assert res[0]["id"] == f"amz-{key}"


def test_validate_typesafe_ai_batch_handles_malformed_input():
    assert validate_typesafe_ai_batch(None) == []
    assert validate_typesafe_ai_batch("") == []
    assert validate_typesafe_ai_batch("{not valid json") == []
    assert validate_typesafe_ai_batch(12345) == []
    # Missing id or invalid item types
    assert validate_typesafe_ai_batch([{"why": "no id"}, {"id": None}]) == []


def test_ensure_valid_telegram_html_auto_repair():
    unclosed = "<b>Flash Deal: <br> <span>Sony Headphones</span> for ₹4,990"
    cleaned = ensure_valid_telegram_html(unclosed)
    assert cleaned.endswith("</b>")
    assert "<br>" not in cleaned
    assert "<span>" not in cleaned
    assert "</span>" not in cleaned
    assert "Sony Headphones" in cleaned


def test_ensure_valid_telegram_html_synonym_normalization():
    synonyms = "<strong>Bold</strong> and <em>Italic</em> with <del>Old Price</del>"
    cleaned = ensure_valid_telegram_html(synonyms)
    assert "<b>Bold</b>" in cleaned
    assert "<i>Italic</i>" in cleaned
    assert "<s>Old Price</s>" in cleaned


def test_critic_integration_with_html_validator():
    deal = Deal(
        id="amz-B0HEALTH01",
        retailer="Amazon",
        title="Sony Headphones",
        url="https://www.amazon.in/dp/B0HEALTH01",
        current_price="₹4,999",
        original_price="₹9,999",
        discount_percent=50,
    )
    # Valid copy with unclosed tag gets auto-repaired and accepted
    copy_with_tag = "<b>Epic drop on Sony Headphones at ₹4,999 (50% OFF)"
    assert validate_critic(copy_with_tag, deal) is True

    # Hallucinated price fails
    hallucinated_copy = "<b>Epic drop at just ₹1,999!"
    assert validate_critic(hallucinated_copy, deal) is False
