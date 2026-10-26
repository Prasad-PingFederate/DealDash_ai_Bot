from unittest.mock import MagicMock, patch

from dealbot.agent import DealAgent, classify_category, score_deal, titles_too_similar
from dealbot.models import Deal
from dealbot.publisher import StateStore, format_caption
from dealbot.ai_copywriter import AICopywriter


def _deal(deal_id, title, discount, category=None, price="₹999", mrp="₹1,999", image="https://img.example/a.jpg"):
    return Deal(
        id=deal_id,
        retailer="Amazon",
        title=title,
        url=f"https://www.amazon.in/dp/{deal_id}",
        current_price=price,
        original_price=mrp,
        discount_percent=discount,
        category=category,
        image_url=image,
    )


def test_classifier_and_similarity():
    assert classify_category(_deal("a", "boAt Airdopes Bluetooth Earbuds", 40)) == "electronics"
    assert classify_category(_deal("b", "Prestige Mixer Grinder", 40)) == "home"
    assert titles_too_similar(
        "Sony WH-CH520 Wireless Bluetooth Headphones",
        "Sony WH-CH520 Wireless Bluetooth Headphones Black",
    )


def test_agent_prefers_fresh_category(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    repeated = _deal("buds", "Noise Bluetooth Earbuds", 70, category="electronics")
    fresh = _deal("mix", "Prestige Mixer Grinder 750W", 60, category="home")
    agent = DealAgent(copywriter=None, min_score=20)
    plan = agent.plan([repeated, fresh], recent_categories=["electronics"], max_posts=1)
    assert plan.posts[0].deal.id == "mix"
    assert plan.source == "heuristic"
    assert any(item.deal.id == "buds" for item in plan.skips)


def test_agent_skips_near_duplicate(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    first = _deal("h1", "Sony WH-CH520 Wireless Bluetooth Headphones", 62)
    twin = _deal("h2", "Sony WH-CH520 Wireless Bluetooth Headphones Black", 64)
    fan = _deal("fan", "Orient Rechargeable Pedestal Fan", 48, category="home")
    agent = DealAgent(copywriter=None, min_score=20)
    plan = agent.plan([first, twin, fan], recent_categories=[], max_posts=2)
    posted = {item.deal.id for item in plan.posts}
    assert "fan" in posted
    assert len(posted & {"h1", "h2"}) == 1


def test_agent_llm_picks_are_constrained_to_real_ids(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    monkeypatch.setenv("AGENT_LLM", "true")
    deals = [
        _deal("keep", "boAt Stone Bluetooth Speaker", 55),
        _deal("also", "Philips Beard Trimmer", 40),
    ]
    copywriter = MagicMock()
    copywriter.is_configured.return_value = True
    copywriter.generate_text.return_value = (
        '{"posts":[{"id":"does-not-exist","why":"hallucinated"},'
        '{"id":"keep","why":"clear speaker discount"}]}'
    )
    plan = DealAgent(copywriter=copywriter, min_score=20).plan(deals, [], max_posts=1)
    assert plan.posts[0].deal.id == "keep"
    assert "clear speaker discount" in plan.posts[0].reason
    assert plan.source == "llm+heuristic"


def test_agent_llm_garbage_falls_back(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    copywriter = MagicMock()
    copywriter.is_configured.return_value = True
    copywriter.generate_text.return_value = "I think you should post everything."
    deal = _deal("only", "Pigeon Electric Kettle 1.5L", 50, category="home")
    plan = DealAgent(copywriter=copywriter, min_score=20).plan([deal], [], max_posts=1)
    assert plan.posts[0].deal.id == "only"
    assert plan.source == "heuristic"


def test_big_saving_on_a_known_brand_beats_a_padded_cheap_deal(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    junk = _deal(
        "cable",
        "Generic USB Cable 3 Pack",
        90,
        price="₹99",
        mrp="₹999",
    )
    flagship = _deal(
        "tv",
        "Samsung 43 inch Smart TV",
        35,
        category="electronics",
        price="₹22,999",
        mrp="₹35,999",
    )
    plan = DealAgent(copywriter=None).plan([junk, flagship], [], max_posts=1)
    assert [item.deal.id for item in plan.posts] == ["tv"]
    assert junk.id not in {item.deal.id for item in plan.posts}
    assert any(item.deal.id == "cable" and item.score < plan.posts[0].score for item in plan.skips)


def test_cheap_product_is_posted(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    cheap = _deal("clip", "Generic Cable Clip", 60, price="₹49", mrp="₹149")
    plan = DealAgent(copywriter=None).plan([cheap], [], max_posts=1)
    assert [item.deal.id for item in plan.posts] == ["clip"]


def test_nothing_posts_when_there_is_no_real_deal(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    nameless = _deal("x", "Deal", 0, price=None, mrp=None, image=None)
    plan = DealAgent(copywriter=None).plan([nameless], [], max_posts=2)
    assert plan.posts == []


def test_feed_keeps_a_small_price():
    from dealbot.sources import worth_posting_discount

    assert worth_posting_discount(25, "149", "49", min_discount=30)
    assert worth_posting_discount(25, "1000", "750", min_discount=30)
    assert not worth_posting_discount(10, "1000", "900", min_discount=30)
    assert worth_posting_discount(40, "1000", "600", min_discount=30)


def test_low_score_deal_is_skipped_when_a_better_one_exists(monkeypatch):
    monkeypatch.setattr("dealbot.agent.current_slot", lambda now=None: "night")
    weak = _deal("weak", "Deal", 0, price=None, mrp=None, image=None)
    strong = _deal("strong", "Fire-Boltt Bluetooth Smart Watch", 65)
    plan = DealAgent(copywriter=None, min_score=32).plan([weak, strong], [], max_posts=1)
    assert [item.deal.id for item in plan.posts] == ["strong"]
    assert any(item.deal.id == "weak" and item.score < 32 for item in plan.skips)


def test_price_critic_drops_copy_that_hides_the_price():
    deal = _deal("p", "OnePlus Nord Buds", 50, price="₹1,499", mrp="₹2,999")
    copywriter = AICopywriter(api_key="test-key")
    with patch.object(copywriter, "craft_deal_copy", return_value="<b>Nice buds</b> grab them fast"):
        caption = format_caption(deal, copywriter=copywriter)
    assert "1,499" in caption
    assert "Nice buds" not in caption


def test_state_store_remembers_agent_decision(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    store.mark_posted(
        "amz-1",
        "Amazon",
        chat_id="@deals",
        message_id=7,
        caption="caption",
        category="electronics",
        discount=61,
        agent_score=78,
        agent_reason="heuristic pick",
    )
    ctx = store.get_recent_context()
    assert ctx[0]["category"] == "electronics"
    assert ctx[0]["discount"] == 61
    store.record_decisions(
        [{"deal_id": "amz-1", "action": "post", "score": 78, "category": "electronics", "reason": "ok"}]
    )
    # Still migrates cleanly on an already-initialized database.
    StateStore(str(tmp_path / "state.sqlite3"))
