"""Failure paths in the current poster: bad feeds, dead pages, and bad Telegram replies."""

import html
from unittest.mock import MagicMock, patch

import requests

from dealbot.expiry_checker import check_deal_is_active, mark_telegram_deal_expired
from dealbot.models import Deal, parse_rupees
from dealbot.publisher import deal_badge, format_caption, offer_percent, send_admin_note, send_telegram_deal
from dealbot.sources import fetch_extra_feeds, parse_offer_feed, worth_posting_discount


def _page(status=200, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


def _deal(deal_id="d1", **kwargs):
    data = dict(
        retailer="Amazon",
        title="boAt Speaker",
        url="https://www.amazon.in/dp/B0TESTDEAL1",
        current_price="₹999",
        original_price="₹1,999",
        discount_percent=50,
    )
    data.update(kwargs)
    return Deal(deal_id, **data)


def test_parse_rupees_rejects_empty_zero_and_absurd_amounts():
    assert parse_rupees(None) is None
    assert parse_rupees("") is None
    assert parse_rupees("free") is None
    assert parse_rupees("₹0") is None
    assert parse_rupees("Rs. 500001") is None
    assert parse_rupees("₹1,499") == 1499
    assert parse_rupees("Rs. 799") == 799
    assert _deal(current_price="₹2,000", original_price="₹1,000").savings_rupees() is None


def test_percent_and_badge_failure_edges():
    close = _deal(discount_percent=40, current_price="₹600", original_price="₹1,000")
    assert offer_percent(close) == 40
    assert deal_badge(_deal(discount_percent=49, current_price="₹500", original_price="₹900")) == "DEAL"
    assert deal_badge(_deal(discount_percent=20, current_price="₹49", original_price="₹70")) == "LOOT"
    assert deal_badge(_deal(discount_percent=10, current_price="₹49", original_price="₹55")) == "DEAL"
    implied = _deal(discount_percent=0, current_price="₹50", original_price="₹100")
    implied.discount_percent = None
    assert offer_percent(implied) == 50


def test_caption_escapes_bank_html_and_drops_a_bad_coupon():
    deal = _deal(coupon_code="<script>", bank_offer="<b>SBI</b> & more")
    caption = format_caption(deal)
    assert "Coupon" not in caption
    assert "Bank offer: " + html.escape("<b>SBI</b> & more") in caption
    assert "<script>" not in caption


def test_ai_tagline_is_dropped_without_the_price_and_escaped_with_it():
    from dealbot.ai_copywriter import AICopywriter

    deal = _deal(current_price="₹1,499", original_price="₹2,999", discount_percent=50)
    writer = AICopywriter(api_key="test-key")
    with patch.object(writer, "craft_deal_copy", return_value="<b>Nice</b> <script>x</script>"):
        caption = format_caption(deal, copywriter=writer)
        assert "<script>" not in caption
        assert "Nice" not in caption
    with patch.object(writer, "craft_deal_copy", return_value="<b>Grab 1499 now</b> <img>"):
        caption = format_caption(deal, copywriter=writer)
        assert "₹1,499" in caption
        assert "<i>Grab 1499 now</i>" in caption
        assert "<img>" not in caption


def test_feed_parser_failure_cases():
    assert parse_offer_feed(b"<not xml") == []
    expired = """<deals><all_deals>
      <title>Gone</title><product_url>https://www.amazon.in/dp/B0EXPIRED01</product_url>
      <org_price>100</org_price><discount_price>40</discount_price>
      <discount_per>60</discount_per><expire>1</expire>
    </all_deals></deals>"""
    assert parse_offer_feed(expired.encode(), min_discount=30) == []
    weak = """<deals><all_deals>
      <title>Tiny</title><product_url>https://www.amazon.in/dp/B0WEAKDEAL1</product_url>
      <org_price>100</org_price><discount_price>95</discount_price>
      <discount_per>5</discount_per><expire>0</expire>
    </all_deals></deals>"""
    assert parse_offer_feed(weak.encode(), min_discount=30) == []
    reddit = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Look at this 80% off</title>
      <content>https://www.reddit.com/r/IndiaDeals/comments/abc only</content>
    </entry></feed>"""
    assert parse_offer_feed(reddit.encode(), min_discount=30) == []
    assert worth_posting_discount(0, "", "", 30) is False
    assert worth_posting_discount(14, "100", "80", 30) is False
    assert worth_posting_discount(15, "100", "80", 30) is True


def test_extra_feed_is_skipped_when_disabled_or_the_request_fails(monkeypatch):
    monkeypatch.setenv("EXTRA_FEED_URLS", "off")
    assert fetch_extra_feeds() == []

    monkeypatch.setenv("EXTRA_FEED_URLS", "https://feeds.example/deals")
    with patch("dealbot.sources.requests.get", return_value=_page(403, "no")):
        assert fetch_extra_feeds() == []
    with patch("dealbot.sources.requests.get", side_effect=requests.Timeout("slow")):
        assert fetch_extra_feeds() == []

    monkeypatch.setenv("DEALS_FEED_URL", "https://feeds.example/deals")
    monkeypatch.setenv("EXTRA_FEED_URLS", "https://feeds.example/deals")
    with patch("dealbot.sources.requests.get") as get:
        assert fetch_extra_feeds() == []
        get.assert_not_called()


def test_stock_check_failure_matrix():
    assert check_deal_is_active("") is False
    with patch("requests.get", return_value=_page(404)):
        assert check_deal_is_active("https://amazon.in/dp/X") is False
    with patch("requests.get", return_value=_page(410)):
        assert check_deal_is_active("https://amazon.in/dp/X") is False
    with patch("requests.get", return_value=_page(503, "out of stock")):
        assert check_deal_is_active("https://amazon.in/dp/X") is True
    with patch("requests.get", side_effect=requests.Timeout("slow")):
        assert check_deal_is_active("https://amazon.in/dp/X") is True
    with patch("requests.get", return_value=_page(200, "This item cannot be shipped to your location")):
        assert check_deal_is_active("https://amazon.in/dp/X") is False


def test_expired_edit_fails_closed():
    assert mark_telegram_deal_expired("", "1", 2, "cap") is False
    with patch("requests.post", return_value=_page(400, "bad html")):
        assert mark_telegram_deal_expired("token", "1", 2, "cap", is_photo=False) is False
    with patch("requests.post", side_effect=requests.ConnectionError("down")):
        assert mark_telegram_deal_expired("token", "1", 2, "cap") is False


def test_telegram_send_and_admin_note_fail_closed(monkeypatch):
    deal = _deal()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    ok, msg_id, caption = send_telegram_deal(deal, dry_run=False)
    assert ok is False and msg_id is None and "₹999" in caption

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    with patch("requests.post", return_value=_page(400, "bad")):
        assert send_telegram_deal(deal, dry_run=False)[0] is False
    with patch("requests.post", side_effect=requests.Timeout("slow")):
        assert send_telegram_deal(deal, dry_run=False)[0] is False

    assert send_admin_note("hello", dry_run=True) is False
    monkeypatch.setenv("ADMIN_CHAT_ID", "99")
    with patch("requests.post", return_value=_page(500, "no")):
        assert send_admin_note("hello", dry_run=False) is False


def _quiet_cycle(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("MAX_POSTS_PER_CYCLE", "1")
    monkeypatch.setenv("AGENT_MODE", "false")
    monkeypatch.setenv("FEEDS_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.setattr("dealbot.main.sweep_expired_deals", lambda *args, **kwargs: None)
    monkeypatch.setattr("dealbot.main.send_admin_note", lambda *args, **kwargs: False)


def test_live_cycle_skips_a_dead_page_and_posts_the_spare(tmp_path, monkeypatch):
    _quiet_cycle(monkeypatch, tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")
    dead = _deal("dead", url="https://www.amazon.in/dp/DEADPAGE01", title="Dead Speaker")
    live = _deal("live", url="https://www.amazon.in/dp/LIVEPAGE01", title="Live Speaker")
    monkeypatch.setattr("dealbot.main.collect_all_deals", lambda: [dead, live])
    monkeypatch.setattr("dealbot.main.check_deal_is_active", lambda url, timeout=8: "DEAD" not in url)
    posted = []
    monkeypatch.setattr(
        "dealbot.main.send_telegram_deal",
        lambda deal, dry_run=True, copywriter=None: posted.append(deal.id) or (True, 7, "cap"),
    )
    from dealbot.main import run_once

    assert run_once() == 1
    assert posted == ["live"]


def test_dry_run_does_not_open_product_pages(tmp_path, monkeypatch):
    _quiet_cycle(monkeypatch, tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr("dealbot.main.collect_all_deals", lambda: [_deal("only")])

    def explode(url, timeout=8):
        raise AssertionError("stock check should not run in dry run")

    monkeypatch.setattr("dealbot.main.check_deal_is_active", explode)
    from dealbot.main import run_once

    assert run_once() == 1


def test_stock_check_can_be_turned_off(tmp_path, monkeypatch):
    _quiet_cycle(monkeypatch, tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("CHECK_STOCK_BEFORE_POST", "false")
    monkeypatch.setattr("dealbot.main.collect_all_deals", lambda: [_deal("only")])
    monkeypatch.setattr(
        "dealbot.main.check_deal_is_active",
        lambda url, timeout=8: (_ for _ in ()).throw(AssertionError("check disabled")),
    )
    monkeypatch.setattr(
        "dealbot.main.send_telegram_deal",
        lambda deal, dry_run=True, copywriter=None: (True, 3, "cap"),
    )
    from dealbot.main import run_once

    assert run_once() == 1


def test_failed_send_and_already_posted_do_not_count(tmp_path, monkeypatch):
    _quiet_cycle(monkeypatch, tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("CHECK_STOCK_BEFORE_POST", "false")
    deal = _deal("once")
    monkeypatch.setattr("dealbot.main.collect_all_deals", lambda: [deal])
    monkeypatch.setattr(
        "dealbot.main.send_telegram_deal",
        lambda deal, dry_run=True, copywriter=None: (False, None, "cap"),
    )
    from dealbot.main import run_once
    from dealbot.publisher import StateStore

    assert run_once() == 0
    store = StateStore(str(tmp_path / "state.sqlite3"))
    assert store.is_posted("once") is False

    store.mark_posted("once", "Amazon")
    monkeypatch.setattr(
        "dealbot.main.send_telegram_deal",
        lambda deal, dry_run=True, copywriter=None: (_ for _ in ()).throw(AssertionError("already posted")),
    )
    assert run_once() == 0
