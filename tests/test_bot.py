from datetime import datetime, timedelta, timezone
from dealbot.models import Deal
from dealbot.publisher import render_message
from dealbot.sources import load_local_deals, clean_title, expand_and_convert_url

def test_render_includes_disclosure_and_link():
    deal = Deal("abc", "Amazon", "Headphones & more", "https://www.amazon.in/dp/TEST?tag=example-21", current_price="₹1,999")
    message = render_message(deal)
    assert "Headphones &amp; more" in message
    assert "Affiliate link" in message
    assert "As an Amazon Associate I earn from qualifying purchases." in message
    assert "https://www.amazon.in/dp/TEST?tag=example-21" in message

def test_deal_requires_https():
    assert not Deal("abc", "Myntra", "Shoes", "http://myntra.com/shoes").is_valid()
    assert Deal("abc", "Myntra", "Shoes", "https://affiliate-network.example/link/123").is_valid()

def test_expired_deal_is_invalid():
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert not Deal("abc", "Flipkart", "Offer", "https://flipkart.com/offer", expires_at=expired).is_valid()

def test_local_feed_skips_missing_fields(tmp_path):
    file = tmp_path / "deals.json"
    file.write_text('[{"title":"missing url","retailer":"Amazon"},{"id":"1","title":"Valid","retailer":"Amazon","url":"https://www.amazon.in/dp/X?tag=test-21"}]', encoding="utf-8")
    deals = load_local_deals(file)
    assert len(deals) == 1
    assert deals[0].title == "Valid"

def test_clean_title_removes_noise():
    assert clean_title("[Apply 5% Coupon] Sony Headphones Rs. 2999 - Amazon") == "Sony Headphones"
    assert clean_title("Orient Rechargeable Fan - Amazon") == "Orient Rechargeable Fan"
    assert clean_title("Puma Shoes Rs 1,499 - Flipkart") == "Puma Shoes"

def test_expand_and_convert_url_amazon(monkeypatch):
    monkeypatch.setenv("AMAZON_PARTNER_TAG", "testtag-21")
    url, deal_id, retailer = expand_and_convert_url("https://www.amazon.in/dp/B0BS1QCFHX")
    assert url == "https://www.amazon.in/dp/B0BS1QCFHX?tag=testtag-21"
    assert deal_id == "amz-B0BS1QCFHX"
    assert retailer == "Amazon"
