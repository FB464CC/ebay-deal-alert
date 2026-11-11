from datetime import datetime, timezone
import sqlite3
from unittest.mock import Mock

import pytest

import ebay_top5 as lane


def listing(title="Brunello Cucinelli Men Large Cashmere Sweater", *, price=80,
            quantity=1, score=120, pct=99.5):
    return {"itemId": "v1|123|0", "title": title,
            "price": {"value": str(price)}, "size": "L",
            "buyingOptions": ["FIXED_PRICE"],
            "estimatedAvailabilities": [{"estimatedAvailableQuantity": quantity}],
            "seller_feedback_score": score, "seller_feedback_percentage": pct,
            "itemWebUrl": "https://www.ebay.com/itm/123"}


@pytest.mark.parametrize("item,part", [
    (listing("Brunello Cucinelli Men's Trousers 34"), "trousers"),
    (listing("Brunello Cucinelli Dress Shirt 16.5 French Cuff"), "French cuffs"),
    (listing("Brunello Cucinelli Dress Shirt Large"), "collar 16.5"),
    (listing("Brunello Cucinelli Suit Pants 42R"), "suit separates"),
    (listing(quantity=3), "multiple available"),
    (listing(score=19), "seller feedback"),
    (listing(pct=97.9), "seller feedback"),
])
def test_wardrobe_and_fake_shop_gates(item, part):
    reason = lane.EbayTop5()._cheap_reason(item, False)[2]
    assert part.lower() in reason.lower()


def test_trial_holds_its_own_small_ai_cap(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(lane.engine, "get_ebay_token", lambda: seen.setdefault(
        "cap", lane.engine.AI_PAID_MONTHLY_BUDGET_USD) and None)
    monkeypatch.setattr(lane.engine, "search_ebay", lambda *_: ([], 0))
    monkeypatch.setattr(lane.engine, "search_ebay_ending_soon_auctions", lambda *_, **__: ([], 0))
    before = lane.engine.AI_PAID_MONTHLY_BUDGET_USD
    lane.EbayTop5(db_path=tmp_path / "seen.db", run_seconds=0, sleeper=lambda _: None).run()
    assert seen["cap"] == min(before, 2.0)
    assert lane.engine.AI_PAID_MONTHLY_BUDGET_USD == before


def test_failed_ai_check_is_not_retried_every_poll(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    monkeypatch.setattr(lane, "_fetch_details", lambda *_: {})
    calls = Mock(return_value=None)
    monkeypatch.setattr(bot.vision, "_first_photo_check", calls)
    bot._judge(conn, "token", listing(), False, bot.clock() + 300)
    bot._judge(conn, "token", listing(), False, bot.clock() + 300)
    assert calls.call_count == 1
    conn.close()


def test_max_bid_stays_inside_taxed_cap():
    ceiling = lane.max_bid(250, 8)
    assert ceiling == 86
    assert (ceiling + 8) * (1 + lane.engine.SALES_TAX_RATE) <= 100
    assert (ceiling + 1 + 8) * (1 + lane.engine.SALES_TAX_RATE) > 100


def test_trial_end_exits_without_browse_or_duplicate_summary(tmp_path, monkeypatch):
    sent = Mock(return_value=True)
    monkeypatch.setattr(lane.engine, "_send_ntfy_notice", sent)
    token = Mock(side_effect=AssertionError("must not call Browse"))
    monkeypatch.setattr(lane.engine, "get_ebay_token", token)
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db", now=lambda: lane.TRIAL_UNTIL)
    bot.run()
    bot.run()
    assert sent.call_count == 1
    token.assert_not_called()


def test_single_or_query_us_and_category(monkeypatch):
    buy, auction = lane.searches()
    assert buy["query"] == auction["query"] == "(loro piana,brunello cucinelli,kiton,brioni,zegna)"
    assert buy["category_id"] == auction["category_id"] == "1059"
    assert auction["search_window_minutes"] == 30
    response = Mock(status_code=200)
    response.json.return_value = {"itemSummaries": [], "total": 0}
    response.raise_for_status.return_value = None
    get = Mock(return_value=response)
    monkeypatch.setattr(lane.engine.requests, "get", get)
    lane.engine.search_ebay("token", buy)
    params = get.call_args.kwargs["params"]
    assert params["q"] == buy["query"]
    assert params["category_ids"] == "1059"
    assert "itemLocationCountry:US" in params["filter"]
    assert params["sort"] == "newlyListed"


def test_one_alert_per_item_id(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    item = listing()
    monkeypatch.setattr(lane, "_fetch_details", lambda *_: {"shippingOptions": [{"shippingCost": {"value": "0"}}]})
    monkeypatch.setattr(bot.vision, "_first_photo_check", lambda *_: {"summary": "clean sweater", "counterfeit_suspected": False,
        "damage_found": False, "weird_logo_found": False, "estimated_resale_value": 250,
        "price_confidence": "high", "looks_good": True})
    monkeypatch.setattr(bot.vision, "_run_vinted_strong_vision", lambda result, _: result.update({
        "vinted_strong_checked": True, "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_brand_label_visible_and_consistent": True,
        "vinted_strong_condition_grade": "excellent", "vinted_strong_estimated_resale_value": 250}) or {})
    monkeypatch.setattr(bot.vision, "_strong_look_veto_reason", lambda result: result.update({
        "vinted_conservative_resale_value": 250}) or None)
    monkeypatch.setattr(lane.engine, "is_blocked_by_steal_quality_gate", lambda *_, **__: None)
    monkeypatch.setattr(lane.engine, "_deepseek_alert_sanity_check", lambda *_, **__: {
        "is_part_or_accessory": False, "is_complete_item": True})
    send = Mock(return_value=True)
    monkeypatch.setattr(lane.engine, "_send_ntfy_notice", send)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    assert send.call_count == 1
    assert conn.execute("SELECT alerted FROM judged WHERE item_id=?", (item["itemId"],)).fetchone() == (1,)
    conn.close()


def test_token_refreshes_and_network_errors_do_not_crash(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    tokens = Mock(return_value="tok")
    monkeypatch.setattr(lane.engine, "get_ebay_token", tokens)
    errors = iter([lane.requests.ConnectionError("blip")] + [None] * 10)

    def search(*_):
        err = next(errors)
        if err:
            raise err
        return [], 0
    monkeypatch.setattr(lane.engine, "search_ebay", search)
    monkeypatch.setattr(lane.engine, "search_ebay_ending_soon_auctions", lambda *_, **__: ([], 0))
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db", run_seconds=4 * 3600,
                        clock=lambda: clock["t"],
                        sleeper=lambda s: clock.__setitem__("t", clock["t"] + max(s, 3600)))
    bot.run()
    assert tokens.call_count == 2  # start + one refresh after 90 min
