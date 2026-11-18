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
    clock = {"t": 0.0}
    lane.EbayTop5(db_path=tmp_path / "seen.db", run_seconds=1, clock=lambda: clock["t"],
                  sleeper=lambda s: clock.__setitem__("t", clock["t"] + max(s, 1))).run()
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
    assert tokens.call_count == 4  # asked every poll; the file cache decides renewal


def test_never_searches_with_an_expired_token(tmp_path, monkeypatch):
    # Real get_ebay_token + file cache on one fake clock: tokens live 2 h and
    # the lane runs ~5.7 h, so every search must use a token that is still live.
    clock = {"t": 1_000_000.0}
    issued = {}
    monkeypatch.setattr(lane.engine, "TOKEN_CACHE_PATH", tmp_path / "token.json")
    monkeypatch.setattr(lane.engine.time, "time", lambda: clock["t"])
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")

    def post(*_, **__):
        token = f"t{len(issued)}"
        issued[token] = clock["t"]
        response = Mock()
        response.json.return_value = {"access_token": token, "expires_in": 7200}
        return response

    stale = []

    def search(token, *_, **__):
        if clock["t"] - issued[token] >= 7200:
            stale.append(clock["t"])
        return [], 0

    monkeypatch.setattr(lane.engine.requests, "post", post)
    monkeypatch.setattr(lane.engine, "search_ebay", search)
    monkeypatch.setattr(lane.engine, "search_ebay_ending_soon_auctions", search)
    monkeypatch.setattr(lane, "TRIAL_UNTIL", datetime(2099, 1, 1, tzinfo=timezone.utc))
    sleep = lambda seconds: clock.__setitem__("t", clock["t"] + max(seconds, 75))
    lane.EbayTop5(db_path=tmp_path / "seen.db", run_seconds=340 * 60, poll_seconds=75,
                  clock=lambda: clock["t"], sleeper=sleep).run()
    assert len(issued) >= 3
    assert stale == []


def test_401_drops_cached_token_so_next_poll_reauths(tmp_path, monkeypatch):
    cache = tmp_path / "token.json"
    cache.write_text('{"access_token": "dead", "expires_at": 9e12}\n', encoding="utf-8")
    monkeypatch.setattr(lane.engine, "TOKEN_CACHE_PATH", cache)

    def search(token, *_, **__):
        if token == "dead":
            raise lane.requests.HTTPError(response=Mock(status_code=401))
        return [], 0

    used = []
    monkeypatch.setattr(lane.engine, "get_ebay_token", lambda: used.append(
        lane.engine._read_cached_ebay_token() or "fresh") or used[-1])
    monkeypatch.setattr(lane.engine, "search_ebay", search)
    monkeypatch.setattr(lane.engine, "search_ebay_ending_soon_auctions", search)
    monkeypatch.setattr(lane, "TRIAL_UNTIL", datetime(2099, 1, 1, tzinfo=timezone.utc))
    clock = {"t": 0.0}
    lane.EbayTop5(db_path=tmp_path / "seen.db", run_seconds=150, poll_seconds=75,
                  clock=lambda: clock["t"],
                  sleeper=lambda s: clock.__setitem__("t", clock["t"] + max(s, 75))).run()
    assert used == ["dead", "fresh"]


def test_size_only_in_item_specifics_reaches_details_not_rejected(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    item = {**listing("Brunello Cucinelli Cashmere Crewneck Sweater"), "size": None}
    response = Mock()
    response.json.return_value = {"localizedAspects": [{"name": "Size", "value": "L"},
                                                       {"name": "Color", "value": "Gray"}]}
    monkeypatch.setattr(lane.requests, "get", Mock(return_value=response))
    photo = Mock(return_value=None)
    monkeypatch.setattr(bot.vision, "_first_photo_check", photo)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    assert item["size"] == "L"
    assert photo.call_count == 1  # cleared the size gate on getItem data
    conn.close()


def test_size_still_missing_after_details_is_recorded_once(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    item = {**listing("Brunello Cucinelli Cashmere Crewneck Sweater"), "size": None}
    get = Mock(return_value=Mock(**{"json.return_value": {}}))
    monkeypatch.setattr(lane.requests, "get", get)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    assert get.call_count == 1
    assert conn.execute("SELECT reason FROM judged").fetchone() == ("size missing or does not fit",)
    conn.close()


def test_hourly_details_cap_defers_without_recording(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    bot.calls["details"] = lane.DETAILS_PER_HOUR
    get = Mock()
    monkeypatch.setattr(lane.requests, "get", get)
    bot._judge(conn, "token", listing(), False, bot.clock() + 300)
    get.assert_not_called()
    assert conn.execute("SELECT count(*) FROM judged").fetchone() == (0,)
    conn.close()


def test_malformed_token_reply_costs_one_poll_not_the_run(tmp_path, monkeypatch):
    replies = iter([KeyError("access_token"), "tok", "tok"])

    def token():
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    searched = []
    monkeypatch.setattr(lane.engine, "get_ebay_token", token)
    monkeypatch.setattr(lane.engine, "search_ebay", lambda t, *_: searched.append(t) or ([], 0))
    monkeypatch.setattr(lane.engine, "search_ebay_ending_soon_auctions", lambda *_, **__: ([], 0))
    monkeypatch.setattr(lane, "TRIAL_UNTIL", datetime(2099, 1, 1, tzinfo=timezone.utc))
    clock = {"t": 0.0}
    lane.EbayTop5(db_path=tmp_path / "seen.db", run_seconds=225, poll_seconds=75,
                  clock=lambda: clock["t"],
                  sleeper=lambda s: clock.__setitem__("t", clock["t"] + max(s, 75))).run()
    assert searched == ["tok", "tok"]


def test_get_item_bare_neck_size_reads_as_collar_and_bad_shapes_are_ignored(monkeypatch):
    def body(payload):
        response = Mock()
        response.json.return_value = payload
        monkeypatch.setattr(lane.requests, "get", Mock(return_value=response))
        return lane._fetch_details("token", "v1|1|0")

    details = body({"localizedAspects": [{"name": "Size", "value": "L"}, None,
                                         {"name": "Neck Size", "value": "16.5"}]})
    assert details["size"] == "16.5 in L"
    assert lane.fast._collar_size({"size": details["size"], "title": "Kiton dress shirt"}) == 16.5
    assert body([]) == {}


def test_suit_with_size_only_in_item_specifics_reaches_details(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    item = {**listing("Kiton Full Suit Navy Wool", price=200), "size": None}
    response = Mock()
    response.json.return_value = {"localizedAspects": [{"name": "Size", "value": "42R"}]}
    get = Mock(return_value=response)
    monkeypatch.setattr(lane.requests, "get", get)
    photo = Mock(return_value=None)
    monkeypatch.setattr(bot.vision, "_first_photo_check", photo)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    assert get.call_count == 1
    reason = conn.execute("SELECT reason FROM judged").fetchone()
    assert reason is None or "40-44" not in reason[0], reason
    conn.close()


@pytest.mark.parametrize("title", [
    "Kiton Cashmere Quarter Zip Sweater Men Large",
    "Zegna Cashmere Crewneck Sweater Men Large",
    "Brioni Cashmere Sweater Men Large",
])
def test_non_lp_houses_reach_the_alert_with_their_own_bar(tmp_path, monkeypatch, title):
    # 11-18 audit: result["search_query"] was the 5-brand QUERY (so the LP/Cucinelli
    # bar rejected every other house) and brand_tier was never set (so the knitwear
    # bar rejected them as not grab_on_sight).
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    item = {**listing(title, price=30), "itemId": "v1|9|0"}  # a Steal on every house's table row
    monkeypatch.setattr(lane, "_fetch_details", lambda *_: {"shippingOptions": [{"shippingCost": {"value": "0"}}]})
    monkeypatch.setattr(bot.vision, "_first_photo_check", lambda *_: {"summary": "clean sweater", "counterfeit_suspected": False,
        "damage_found": False, "weird_logo_found": False, "estimated_resale_value": 400,
        "price_confidence": "high", "looks_good": True})
    monkeypatch.setattr(bot.vision, "_run_vinted_strong_vision", lambda result, _: result.update({
        "vinted_strong_checked": True, "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_brand_label_visible_and_consistent": True,
        "vinted_strong_condition_grade": "excellent", "vinted_strong_estimated_resale_value": 400}) or {})
    monkeypatch.setattr(bot.vision, "_strong_look_veto_reason", lambda result: result.update({
        "vinted_conservative_resale_value": 400}) or None)
    monkeypatch.setattr(lane.engine, "_deepseek_alert_sanity_check", lambda *_, **__: {
        "is_part_or_accessory": False, "is_complete_item": True})
    send = Mock(return_value=True)
    monkeypatch.setattr(lane.engine, "_send_ntfy_notice", send)
    bot._judge(conn, "token", item, False, bot.clock() + 300)
    reason = conn.execute("SELECT reason FROM judged").fetchone()
    assert send.call_count == 1, reason
    conn.close()
