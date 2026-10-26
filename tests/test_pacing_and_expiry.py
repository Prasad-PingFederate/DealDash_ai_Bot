from unittest.mock import MagicMock, patch
from dealbot.pacing import is_within_active_hours, get_recommended_sleep_seconds
from dealbot.expiry_checker import check_deal_is_active, mark_telegram_deal_expired
from dealbot.publisher import StateStore

def test_pacing_active_hours():
    # Test hour limits logic
    assert is_within_active_hours(start_hour=0, end_hour=23) is True
    assert is_within_active_hours(start_hour=25, end_hour=26) is False

def test_pacing_sleep_duration():
    with patch("dealbot.pacing.is_within_active_hours", return_value=True):
        assert get_recommended_sleep_seconds(poll_minutes=15) == 15 * 60
    with patch("dealbot.pacing.is_within_active_hours", return_value=False):
        assert get_recommended_sleep_seconds(poll_minutes=15) == 3600

def test_expiry_checker_detects_oos():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>Currently unavailable. We don't know when this item will be back in stock.</body></html>"
    with patch("requests.get", return_value=mock_resp):
        assert check_deal_is_active("https://amazon.in/dp/B01") is False

def test_expiry_checker_detects_in_stock():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>In stock. Sold by Appario. Add to Cart</body></html>"
    with patch("requests.get", return_value=mock_resp):
        assert check_deal_is_active("https://amazon.in/dp/B01") is True


def test_expiry_checker_ignores_related_out_of_stock_text():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>Add to Cart. Similar item currently unavailable.</body></html>"
    with patch("requests.get", return_value=mock_resp):
        assert check_deal_is_active("https://amazon.in/dp/B01") is True

def test_mark_telegram_deal_expired():
    mock_resp = MagicMock(status_code=200)
    with patch("requests.post", return_value=mock_resp):
        success = mark_telegram_deal_expired("fake_token", "-1001", 12345, "Original deal text", is_photo=True)
        assert success is True

def test_state_store_message_id_and_expiry(tmp_path):
    db_file = tmp_path / "state.sqlite3"
    store = StateStore(str(db_file))
    
    store.mark_posted("deal-101", "Amazon", chat_id="-1001", message_id=555, caption="Test Caption")
    assert store.is_posted("deal-101") is True
    
    active = store.get_recent_active_deals()
    assert len(active) == 1
    assert active[0]["deal_id"] == "deal-101"
    assert active[0]["message_id"] == 555
    
    store.mark_expired("deal-101")
    active_after = store.get_recent_active_deals()
    assert len(active_after) == 0
