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

def test_retailer_filtering_only_amazon(monkeypatch, tmp_path):
    from dealbot.sources import collect_all_deals
    monkeypatch.setenv("FEEDS_ENABLED", "false")
    monkeypatch.setenv("AMAZON_API_ENABLED", "false")
    deals_file = tmp_path / "deals.json"
    deals_file.write_text(
        '[{"id":"1","title":"Amz Deal","retailer":"Amazon","url":"https://www.amazon.in/dp/X?tag=t-21"},'
        '{"id":"2","title":"FK Deal","retailer":"Flipkart","url":"https://www.flipkart.com/p?pid=P"}]',
        encoding="utf-8"
    )
    monkeypatch.setenv("LOCAL_DEALS_FILE", str(deals_file))
    
    # 1. With ALLOWED_RETAILERS=Amazon
    monkeypatch.setenv("ALLOWED_RETAILERS", "Amazon")
    deals = collect_all_deals()
    assert len(deals) == 1
    assert deals[0].retailer == "Amazon"
    
    # 2. When FLIPKART_AFFILIATE_ID is placeholder, Flipkart is skipped
    monkeypatch.delenv("ALLOWED_RETAILERS", raising=False)
    monkeypatch.setenv("FLIPKART_AFFILIATE_ID", "yourflipkartid")
    deals = collect_all_deals()
    assert len(deals) == 1
    assert deals[0].retailer == "Amazon"

    # 3. When valid FLIPKART_AFFILIATE_ID is provided, both are included
    monkeypatch.setenv("FLIPKART_AFFILIATE_ID", "real_affiliate_id")
    deals = collect_all_deals()
    assert len(deals) == 2
