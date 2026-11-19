from unittest.mock import MagicMock
import pytest
import requests

from dealbot.monetizer import (
    MonetizationResult,
    can_publish_monetization,
    detect_store,
    extract_asin,
    monetize_url,
    slugify,
    strip_tracking_params,
    unshorten_url,
)


def test_slugify():
    assert slugify("Telegram Channel") == "telegram_channel"
    assert slugify("All-Time Low!!") == "all_time_low"
    assert slugify("") == "na"
    assert slugify(None) == "na"


def test_unshorten_url_fast_path():
    # Non-shortener URL should return directly with zero network calls
    url = "https://www.amazon.in/dp/B0BS1QCFHX"
    assert unshorten_url(url) == url


def test_unshorten_url_redirect_hops():
    mock_session = MagicMock(spec=requests.Session)
    resp1 = MagicMock(status_code=301, headers={"location": "https://www.amazon.in/dp/B0BS1QCFHX"})
    mock_session.head.return_value = resp1

    expanded = unshorten_url("https://amzn.to/3xyz", session=mock_session)
    assert expanded == "https://www.amazon.in/dp/B0BS1QCFHX"
    mock_session.head.assert_called_once()


def test_unshorten_url_loop_and_max_hops():
    mock_session = MagicMock(spec=requests.Session)
    # Ping-pong loop
    r1 = MagicMock(status_code=302, headers={"location": "https://amzn.to/hop2"})
    r2 = MagicMock(status_code=302, headers={"location": "https://amzn.to/hop1"})
    mock_session.head.side_effect = [r1, r2, r1]

    # Should safely terminate without infinite loop
    res = unshorten_url("https://amzn.to/hop1", max_hops=3, session=mock_session)
    assert res in ("https://amzn.to/hop1", "https://amzn.to/hop2")


def test_strip_tracking_params():
    url = (
        "https://www.amazon.in/dp/B0BS1QCFHX?"
        "tag=competitor-21&affid=comp_aff&ascsubtag=old_sub&"
        "utm_source=telegram&utm_medium=social&utm_campaign=sale&"
        "ref=sr_1_1&pf_rd_r=XYZ123&pd_rd_w=ABC456&fbclid=fb_click&th=1&psc=1"
    )
    cleaned = strip_tracking_params(url)
    assert "tag=" not in cleaned
    assert "affid=" not in cleaned
    assert "ascsubtag=" not in cleaned
    assert "utm_" not in cleaned
    assert "pf_rd_" not in cleaned
    assert "pd_rd_" not in cleaned
    assert "fbclid=" not in cleaned
    assert "ref=" not in cleaned
    # Preserves essential product variant parameters
    assert "th=1" in cleaned
    assert "psc=1" in cleaned


def test_extract_asin_edge_cases():
    assert extract_asin("https://www.amazon.in/dp/B0BS1QCFHX") == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/Sony-Headphones/dp/B0BS1QCFHX/ref=xyz") == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/gp/product/B0BS1QCFHX") == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/gp/aw/d/B0BS1QCFHX") == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/exec/obidos/ASIN/B0BS1QCFHX") == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/d/B0BS1QCFHX/") == "B0BS1QCFHX"
    assert extract_asin("https://www.amazon.in/product?asin=B0BS1QCFHX&foo=bar") == "B0BS1QCFHX"

    # Edge cases: invalid length or non-alphanumeric
    assert extract_asin("https://www.amazon.in/dp/SHORT") is None
    assert extract_asin("https://www.amazon.in/dp/TOOLONGASIN123") is None
    assert extract_asin("https://www.flipkart.com/item/p/itm123") is None
    assert extract_asin("") is None


def test_detect_store():
    assert detect_store("https://www.amazon.in/dp/B0BS1QCFHX") == "amazon"
    assert detect_store("https://amzn.to/3xyz") == "amazon"
    assert detect_store("https://www.flipkart.com/p/itm123") == "flipkart"
    assert detect_store("https://fkrt.it/xyz") == "flipkart"
    assert detect_store("https://www.myntra.com/tshirt/123") == "myntra"
    assert detect_store("https://www.ajio.com/s/123") == "ajio"
    assert detect_store("https://www.tatacliq.com/p/123") == "tatacliq"
    assert detect_store("https://www.nykaa.com/p/123") == "nykaa"
    assert detect_store("https://example.com/deal") == "example.com"
    assert detect_store("") == "unknown"


def test_monetize_amazon_canonical(monkeypatch):
    monkeypatch.setenv("AMAZON_TAG", "mypartner-21")
    raw_url = "https://www.amazon.in/Sony-Headphone/dp/B0BS1QCFHX?tag=competitor-21&utm_source=old"

    res = monetize_url(raw_url, source="telegram", verdict="all_time_low", allow_unshorten=False)
    assert isinstance(res, MonetizationResult)
    assert res.store == "amazon"
    assert res.is_monetized is True
    assert res.error_reason is None
    assert res.monetized_url == (
        "https://www.amazon.in/dp/B0BS1QCFHX?tag=mypartner-21&ascsubtag=telegram_all_time_low"
    )

    # Amazon URL without ASIN should fail monetization
    bad_res = monetize_url("https://www.amazon.in/gp/bestsellers", allow_unshorten=False)
    assert bad_res.is_monetized is False
    assert "No valid 10-character ASIN" in (bad_res.error_reason or "")


def test_monetize_non_amazon_cuelinks(monkeypatch):
    monkeypatch.setenv("CUELINKS_PUB_ID", "12345")
    fk_url = "https://www.flipkart.com/item/p/itm123?affid=competitor"

    res = monetize_url(fk_url, source="telegram", verdict="good_buy", allow_unshorten=False)
    assert res.store == "flipkart"
    assert res.is_monetized is True
    assert "linksredirect.com/?cid=12345" in res.monetized_url
    assert "subid=telegram_good_buy" in res.monetized_url
    assert "affid=competitor" not in res.monetized_url


def test_monetize_non_amazon_unconfigured(monkeypatch):
    monkeypatch.delenv("CUELINKS_PUB_ID", raising=False)
    monkeypatch.delenv("AFFILIATE_REDIRECT_TEMPLATE", raising=False)
    monkeypatch.delenv("FLIPKART_AFFID", raising=False)

    myntra_url = "https://www.myntra.com/tshirt/123"
    res = monetize_url(myntra_url, allow_unshorten=False)
    assert res.store == "myntra"
    assert res.is_monetized is False
    assert "No affiliate network or publisher ID configured" in (res.error_reason or "")


def test_can_publish_monetization_gate():
    monetized = MonetizationResult("https://amazon.in/dp/B0BS1QCFHX", "amazon", True, None)
    can_pub, reason = can_publish_monetization(monetized)
    assert can_pub is True

    unmonetized = MonetizationResult("https://example.com/p", "unknown", False, "No affiliate program")
    blocked, reason_blocked = can_publish_monetization(unmonetized, allow_unmonetized=False)
    assert blocked is False
    assert "Blocked unmonetized link" in reason_blocked

    allowed_override, reason_override = can_publish_monetization(unmonetized, allow_unmonetized=True)
    assert allowed_override is True
    assert "override" in reason_override
