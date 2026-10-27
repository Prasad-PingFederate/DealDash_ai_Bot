import pytest
from dealbot.models import Deal
from dealbot.expiry_checker import extract_amazon_coupon, extract_amazon_extra_offers
from dealbot.sources import extract_feed_coupon_and_offers
from dealbot.web_exporter import deal_to_web_json
from dealbot.publisher import format_caption


def test_extract_amazon_coupon_flat():
    html_sample = '''
    <div id="couponBadgeRegularVpc">
      <label for="checkbox">
        <span class="a-color-base">Apply ₹50 coupon</span>
      </label>
    </div>
    '''
    disc, text = extract_amazon_coupon(html_sample, live_price=999)
    assert disc == 50
    assert "₹50" in text


def test_extract_amazon_coupon_small_flat_no_min_threshold():
    # Verify small ₹20 flat coupon is captured without any minimum threshold restriction
    html_sample = '<span id="couponText123">Apply ₹20 coupon</span>'
    disc, text = extract_amazon_coupon(html_sample, live_price=299)
    assert disc == 20
    assert "₹20" in text


def test_extract_amazon_coupon_percentage():
    html_sample = '''
    <div class="newCouponBadge">
      <span>Apply 5% coupon</span>
    </div>
    '''
    disc, text = extract_amazon_coupon(html_sample, live_price=1000)
    assert disc == 50  # 5% of 1000
    assert "5%" in text


def test_extract_amazon_extra_offers():
    html_sample = '''
    <div id="itembox-Cashback">
      <span>Get ₹50 cashback with Amazon Pay UPI</span>
    </div>
    <div id="snsAccordionRowMiddle">
      <span>Save 5% now and up to 10% on repeat deliveries</span>
    </div>
    <div id="specialOffer_feature_div">
      <span>Buy 2 Get 5% Off</span>
    </div>
    '''
    offers = extract_amazon_extra_offers(html_sample)
    assert len(offers) == 3
    assert any("₹50 Cashback on Amazon Pay UPI" in o for o in offers)
    assert any("Subscribe & Save" in o for o in offers)
    assert any("Buy 2 Get 5% Off" in o for o in offers)


def test_extract_feed_coupon_and_offers():
    title = "[Apply ₹40 Coupon] Lifelong Egg Boiler Rs.799 with 5% coupon + S&S"
    res = extract_feed_coupon_and_offers(title)
    assert res["coupon_discount"] == 40
    assert "₹40" in res["coupon_text"]
    assert any("Subscribe & Save" in o for o in res["extra_offers"])


def test_deal_to_web_json_claim_steps_and_offers():
    deal = Deal(
        id="amz-B0TEST1234",
        retailer="Amazon",
        title="Sample Wireless Earbuds",
        url="https://www.amazon.in/dp/B0TEST1234",
        current_price="₹1,499",
        original_price="₹2,999",
        discount_percent=50,
        coupon_discount=200,
        coupon_text="Apply ₹200 coupon",
        promo_code="SOUND200",
        bank_offer="10% Instant Discount on HDFC Cards",
        extra_offers=["💸 ₹50 Cashback on Amazon Pay UPI", "🔄 Subscribe & Save: Extra 5%–10% Off"]
    )
    deal.effective_price = 1299
    
    web_dict = deal_to_web_json(deal, score=85)
    assert web_dict["coupon_discount"] == 200
    assert web_dict["coupon_text"] == "Apply ₹200 coupon"
    assert len(web_dict["extra_offers"]) == 2
    assert len(web_dict["claim_steps"]) >= 4
    assert any("SOUND200" in s for s in web_dict["claim_steps"])
    assert any("₹200" in s for s in web_dict["claim_steps"])
    assert any("HDFC" in s for s in web_dict["claim_steps"])


def test_publisher_caption_includes_coupon_trick_and_bonus():
    deal = Deal(
        id="amz-B0TEST5678",
        retailer="Amazon",
        title="Philips Air Fryer",
        url="https://www.amazon.in/dp/B0TEST5678",
        current_price="₹4,999",
        original_price="₹9,999",
        discount_percent=50,
        coupon_discount=500,
        coupon_text="Apply ₹500 coupon",
        extra_offers=["💸 ₹50 Cashback on Amazon Pay UPI"]
    )
    deal.effective_price = 4499
    
    caption = format_caption(deal)
    assert "Coupon Trick:" in caption
    assert "Apply ₹500 coupon" in caption
    assert "Bonus Offer:" in caption
    assert "₹50 Cashback on Amazon Pay UPI" in caption
