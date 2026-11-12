import base64
import io
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image
import pytest
import requests

import ebay_deal_alert as engine
import platforms as marketplaces
import vinted_fast as fast


def _raw_item(item_id, timestamp=1_700_000_000):
    return {
        "id": str(item_id),
        "title": f"Peter Millar Gamecocks Polo {item_id}",
        "price": {"amount": "20"},
        "total_item_price": {"amount": "21"},
        "url": f"https://www.vinted.com/items/{item_id}",
        "size_title": "M",
        "user": {"login": "seller"},
        "photo": {
            "url": "https://images.example/main.jpg",
            "is_main": True,
            "high_resolution": {"timestamp": timestamp},
        },
        "photos": [],
    }


def _listing_and_result():
    listing = marketplaces.make_listing(
        "vinted",
        "new-1",
        "Peter Millar South Carolina Gamecocks Polo Size M",
        20,
        "https://www.vinted.com/items/new-1",
        image_url="https://images.example/main.jpg",
        shipping=1.99,
        size="M",
    )
    result = engine.score_listing(
        listing, None, shipping_cost=1.99, category="school-gear"
    )
    result.update(
        {
            "item_price": 20.0,
            "shipping_cost": 1.99,
            "search_query": '"peter millar" "gamecocks" polo -scarf',
            "search_id": "school-peter-millar-polo",
            "category": "school-gear",
            "profile": "fast",
        }
    )
    return listing, result


def _lane(tmp_path):
    return fast.VintedFastLane(
        searches=[],
        state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        main_seen_db_path=tmp_path / "missing-main.db",
        run_seconds=1,
    )


def test_ntfy_feedback_actions_include_listing_id(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    with mock.patch.object(fast.requests, "post") as post:
        lane._ntfy("Deal", "Buy now", "5", "zap", "https://vinted.test/items/123",
                   item_id="vinted:123")
    actions = post.call_args.kwargs["headers"]["Actions"].split("; ")
    assert len(actions) == 3
    assert actions[0] == "view, Vinted app, https://vinted.test/items/123"
    assert actions[1] == ("http, Bought, https://ntfy.sh/test-topic-fb, method=POST, "
                          "body=bought vinted:123, clear=true")
    assert actions[2] == ("http, Gone, https://ntfy.sh/test-topic-fb, method=POST, "
                          "body=gone vinted:123, clear=true")


def test_verified_vinted_alert_uses_feedback_actions(monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    _listing, result = _listing_and_result()
    with mock.patch.object(engine.requests, "post") as post:
        engine.send_alert(result, feedback_item_id="vinted:123")
    actions = post.call_args.kwargs["headers"]["Actions"].split("; ")
    assert len(actions) == 3
    assert actions[0].startswith("view, Vinted app, ")
    assert "body=bought vinted:123" in actions[1]
    assert "body=gone vinted:123" in actions[2]


def test_feedback_poll_records_valid_tap_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    lane._alert_watch["vinted:123"] = {"title": "A deal", "price": 20,
                                        "kind": "instant", "alert_time": 1000}
    response = mock.Mock()
    response.text = '\n'.join(json.dumps(row) for row in (
        {"id": "one", "time": 1012, "event": "message", "message": "bought vinted:123"},
        {"id": "two", "time": 1013, "event": "message", "message": "bad input"},
    ))
    state = {"version": 1, "searches": {}}
    with mock.patch.object(fast.requests, "get", return_value=response) as get:
        lane._poll_feedback(state)
    assert get.call_args.args[0] == "https://ntfy.sh/test-topic-fb/json?poll=1&since=12h"
    assert get.call_args.kwargs["timeout"] == 5
    assert state["feedback_cursor"] == "two"
    rows = [json.loads(line) for line in lane.log_path.read_text().splitlines()]
    assert rows == [{"type": "feedback", "outcome": "bought", "item_id": "vinted:123",
                     "tap_time": 1012, "title": "A deal", "price": 20, "kind": "instant"}]


def test_feedback_poll_network_error_does_not_change_vinted_pacing(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    lane._last_request_started = 40
    state = {"version": 1, "searches": {}}
    with mock.patch.object(fast.requests, "get", side_effect=requests.Timeout):
        lane._poll_feedback(state)
    assert lane._last_request_started == 40
    assert "feedback_cursor" not in state


def test_feedback_poll_runs_at_most_once_per_five_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    now = [1000]
    lane = _lane(tmp_path)
    lane.clock = lambda: now[0]
    with mock.patch.object(lane, "_poll_feedback") as poll:
        lane._maybe_poll_feedback({})
        lane._feedback_thread.join()
        now[0] = 1299
        lane._maybe_poll_feedback({})
        assert poll.call_count == 1
        now[0] = 1300
        lane._maybe_poll_feedback({})
        lane._feedback_thread.join()
        assert poll.call_count == 2


def _prescreen_response(answer_text='{"answer": true}', *, cost=0.00002):
    response = mock.Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": answer_text}}],
        "usage": {"cost": cost},
    }
    return response


def _image_bytes(size=(1024, 768), color="navy"):
    image = io.BytesIO()
    Image.new("RGB", size, color).save(image, "PNG")
    return image.getvalue()


def _prescreen_result(item_id="prescreen-1", category="other"):
    listing = {
        "itemId": f"vinted:{item_id}",
        "title": "Brooks Brothers Golden Fleece Mens Shirt",
        "price": {"value": 12.0, "currency": "USD"},
        "itemWebUrl": f"https://www.vinted.com/items/{item_id}",
        "platform": "vinted",
        "image": {"imageUrl": "https://images.example/one.jpg"},
        "additionalImages": [
            {"imageUrl": "https://images.example/two.jpg"},
            {"imageUrl": "https://images.example/three.jpg"},
        ],
    }
    return {
        "listing": listing,
        "category": category,
        "verdict": "REVIEW",
        "item_price": 12.0,
        "price": 12.72,
        "vinted_fast_lane": True,
    }


def _passing_strong_result(resale=100, **overrides):
    result = {
        "brand_label_visible_and_consistent": True,
        "brand_label_reason": "Permanent maker label is clear and consistent",
        "counterfeit_indicators": False,
        "counterfeit_reason": "",
        "condition_grade": "excellent",
        "flaws_found": [],
        "premium_desirable_piece": True,
        "premium_reason": "Premium line and fabric are visible on the tags",
        "estimated_resale_value": resale,
        "size_on_tag": "M",
    }
    result.update(overrides)
    return result


def _record_mock_strong(lane, result, strong_result):
    result["vinted_strong_checked"] = True
    result["vinted_strong_brand_label_visible_and_consistent"] = strong_result.get(
        "brand_label_visible_and_consistent"
    )
    result["vinted_strong_brand_label_reason"] = strong_result.get(
        "brand_label_reason"
    )
    result["vinted_strong_label_other_brand"] = strong_result.get(
        "label_shows_different_brand"
    )
    result["vinted_strong_counterfeit_indicators"] = strong_result.get(
        "counterfeit_indicators"
    )
    result["vinted_strong_counterfeit_reason"] = strong_result.get(
        "counterfeit_reason"
    )
    result["vinted_strong_condition_grade"] = strong_result.get("condition_grade")
    result["vinted_strong_flaws_found"] = strong_result.get("flaws_found")
    result["vinted_strong_premium"] = strong_result.get("premium_desirable_piece")
    result["vinted_strong_premium_reason"] = strong_result.get("premium_reason")
    result["vinted_strong_estimated_resale_value"] = strong_result.get(
        "estimated_resale_value"
    )
    result["vinted_strong_size_on_tag"] = strong_result.get("size_on_tag")
    lane.strong_look_calls += 1
    lane.ai_calls += 1
    return strong_result


def _run_one_pipeline(
    tmp_path, *, jev_score=0.9, ai_result=None, strong_result=None, cap=40
):
    lane = _lane(tmp_path)
    listing, result = _listing_and_result()
    lane._cheap_result = mock.Mock(return_value=(listing, result, None))
    conn = engine.init_db(lane.seen_db_path)
    if ai_result is None:
        ai_result = {
            "damage_found": False,
            "weird_logo_found": False,
            "looks_good": True,
            "counterfeit_suspected": False,
            "summary": "Peter Millar Gamecocks polo in clean condition",
            "visible_brand_evidence": "Peter Millar neck label",
            "estimated_retail_price": 110,
            "estimated_resale_value": 100,
            "price_confidence": "high",
            "liquidity": "fast",
        }
    with (
        mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}),
        mock.patch.object(engine, "jev_vinted_score", return_value=jev_score),
        mock.patch.object(engine, "check_photos_with_gemini", return_value=ai_result) as photo,
        mock.patch.object(
            engine,
            "_deepseek_alert_sanity_check",
            return_value={
                "is_complete_item": True,
                "is_part_or_accessory": False,
                "reason": "",
            },
        ),
        mock.patch.object(engine, "send_alert") as send,
        mock.patch.object(fast, "VINTED_FAST_MAX_AI_CALLS_PER_RUN", cap),
        mock.patch.object(
            lane,
            "_run_vinted_strong_vision",
            side_effect=lambda merged, _hard_stop: _record_mock_strong(
                lane,
                merged,
                strong_result or _passing_strong_result(),
            ),
        ) as strong,
    ):
        lane._process_item(conn, None, {}, {"query": result["search_query"]}, None)
    seen = not engine.is_new(conn, listing["itemId"])
    conn.close()
    return lane, photo, strong, send, seen


def _apparel_listing_result(
    title,
    item_price,
    category,
    search_query,
    *,
    search_id="vinted-test-apparel",
):
    listing = marketplaces.make_listing(
        "vinted",
        search_id,
        title,
        item_price,
        f"https://www.vinted.com/items/{search_id}",
        image_url="https://images.example/main.jpg",
        shipping=0,
        size="M",
    )
    result = engine.score_listing(
        listing, None, shipping_cost=0, category=category
    )
    result.update(
        {
            "item_price": float(item_price),
            "shipping_cost": 0.0,
            "search_query": search_query,
            "search_id": search_id,
            "category": category,
            "profile": "fast",
            "vinted_fast_lane": True,
        }
    )
    return listing, result


def _run_custom_pipeline(
    tmp_path,
    listing,
    result,
    ai_result,
    *,
    strong_result=None,
    strong_unavailable=None,
    cap=40,
):
    lane = _lane(tmp_path)
    lane._cheap_result = mock.Mock(return_value=(listing, result, None))
    conn = engine.init_db(lane.seen_db_path)

    def strong_side_effect(merged, _hard_stop):
        if strong_unavailable:
            merged["vinted_strong_checked"] = False
            merged["vinted_strong_unavailable_reason"] = strong_unavailable
            return None
        return _record_mock_strong(
            lane,
            merged,
            strong_result or _passing_strong_result(),
        )

    with (
        mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}),
        mock.patch.object(engine, "jev_vinted_score", return_value=0.9),
        mock.patch.object(engine, "check_photos_with_gemini", return_value=ai_result),
        mock.patch.object(
            engine,
            "_deepseek_alert_sanity_check",
            return_value={
                "is_complete_item": True,
                "is_part_or_accessory": False,
                "reason": "",
            },
        ),
        mock.patch.object(engine, "send_alert") as send,
        mock.patch.object(fast, "VINTED_FAST_MAX_AI_CALLS_PER_RUN", cap),
        mock.patch.object(
            lane, "_run_vinted_strong_vision", side_effect=strong_side_effect
        ) as strong,
    ):
        lane._process_item(
            conn,
            None,
            {},
            {"query": result["search_query"]},
            None,
        )
    row = json.loads(lane.log_path.read_text(encoding="utf-8"))
    conn.close()
    return lane, strong, send, row


def _apparel_ai(resale, **overrides):
    result = {
        "damage_found": False,
        "weird_logo_found": False,
        "looks_good": True,
        "counterfeit_suspected": False,
        "counterfeit_reason": "",
        "summary": "Clean premium menswear item",
        "visible_brand_evidence": "Permanent maker label is visible",
        "peter_millar_back_crown_visible": True,
        "estimated_retail_price": resale * 3,
        "estimated_resale_value": resale,
        "price_confidence": "high",
        "liquidity": "fast",
    }
    result.update(overrides)
    return result


_REAL_SELLER_STATS = fast.fetch_vinted_seller_stats
TRUSTED_SELLER = {
    "_http_status": 200,
    "feedback_count": 12.0,
    "feedback_reputation": 1.0,
    "item_count": 20.0,
    "given_item_count": 15.0,
}


@pytest.fixture(autouse=True)
def _no_live_seller_lookup(monkeypatch):
    monkeypatch.setattr(fast, "fetch_vinted_seller_stats", lambda _user_id: dict(TRUSTED_SELLER))


def _fragrance_search(search_id="vinted-pdm-delina-exclusif"):
    return next(search for search in engine.SAVED_SEARCHES if search["id"] == search_id)


def _fragrance_item(item_id, title, price, brand="Parfums de Marly"):
    item = _raw_item(item_id)
    item["title"] = title
    item["price"] = {"amount": str(price)}
    item["total_item_price"] = {"amount": str(price)}
    item["item_box"] = {"first_line": brand}
    return item


def _fragrance_ai(resale=400, **overrides):
    result = {
        "damage_found": False,
        "weird_logo_found": False,
        "looks_good": True,
        "counterfeit_suspected": False,
        "counterfeit_reason": "",
        "summary": "Sealed full-size women's perfume bottle in clean condition",
        "visible_brand_evidence": "Parfums de Marly bottle and box markings",
        "estimated_retail_price": 430,
        "estimated_resale_value": resale,
        "price_confidence": "high",
        "liquidity": "fast",
    }
    result.update(overrides)
    return result


def test_only_items_newer_than_high_water_enter_pipeline():
    baseline, water = fast.items_newer_than_high_water(
        [_raw_item(3, 300), _raw_item(2, 200)], None
    )
    assert baseline == []
    assert water["ids"] == ["3", "2"]
    newer, next_water = fast.items_newer_than_high_water(
        [_raw_item(4, 400), _raw_item(3, 300), _raw_item(2, 200)], water
    )
    assert [item["id"] for item in newer] == ["4"]
    assert next_water["ids"][0] == "4"


def test_draft_published_late_with_a_low_id_still_enters_pipeline():
    # Live 11-04: ids come from draft creation, so newest_first shows a
    # fresh listing below ids already seen; the old high-water mark hid it.
    _, water = fast.items_newer_than_high_water(
        [_raw_item(900), _raw_item(800)], None
    )
    newer, water = fast.items_newer_than_high_water(
        [_raw_item(500), _raw_item(900), _raw_item(800)], water
    )
    assert [item["id"] for item in newer] == ["500"]
    newer, _ = fast.items_newer_than_high_water(
        [_raw_item(500), _raw_item(900)], water
    )
    assert newer == []


def test_legacy_single_marker_state_upgrades_to_id_memory():
    legacy = fast._item_marker(_raw_item(3, 300))
    newer, water = fast.items_newer_than_high_water(
        [_raw_item(4, 400), _raw_item(3, 300)], legacy
    )
    assert [item["id"] for item in newer] == ["4"]
    assert water == {"ids": ["4", "3"]}


def test_vinted_deal_is_judged_at_full_landed_list_price():
    # Owner: offers aren't reliably accepted and steals are bought at list.
    result = {"item_price": 100.0, "price": 120.0}
    assert fast._set_vinted_negotiation_prices(result) == 120.0
    assert result["vinted_expected_paid"] == 120.0
    assert result["vinted_expected_paid_fraction"] == 1.0
    assert "vinted_opening_offer" not in result


def test_vinted_brand_resale_floors_match_owner_profile():
    expected = {
        "peter millar": 30,
        "canali": 150,
        "brooks brothers": 150,
        "e. marinella": 60,
        "marinella": 60,
    }
    assert {
        brand: fast.VINTED_MIN_RESALE_BY_BRAND[brand]
        for brand in expected
    } == expected


def test_new_vinted_searches_load_with_required_scope_sizes_and_negatives():
    searches = {search["id"]: search for search in engine.SAVED_SEARCHES}
    required_ids = {
        "vinted-canali-suit",
        "vinted-brooks-brothers",
        "vinted-e-marinella-tie",
        "vinted-peter-millar-crown-crafted",
    }
    assert required_ids <= searches.keys()
    for search_id in required_ids:
        search = searches[search_id]
        assert search["enabled"] is True
        assert search["profile"] == "fast"
        assert search["platforms"] == ["vinted"]
        assert search["ebay"] is False
        for negative in ("-women", "-womens", "-ladies", "-girls", "-kids"):
            assert negative in search["query"]

    canali = searches["vinted-canali-suit"]
    assert canali["category"] == "tailoring"
    assert canali["size"] == ["40", "41", "42", "43", "44"]
    assert canali["size_equivalents"] == ["50", "52", "54"]
    assert engine.listing_matches_size_filter(
        canali, {"title": "Canali Navy Wool Suit 42R"}
    )
    assert engine.listing_matches_size_filter(
        canali, {"title": "Canali Navy Wool Suit EU 52"}
    )
    assert engine.listing_matches_size_filter(
        canali, {"title": "Canali Navy Wool Suit", "size": "52"}
    )
    assert not engine.listing_matches_size_filter(
        canali, {"title": "Canali Navy Wool Suit 46R", "size": "EU 56"}
    )
    assert searches["vinted-brooks-brothers"]["size"] == ["40", "41", "42", "43", "44"]
    assert searches["vinted-e-marinella-tie"]["size"] is None
    # Owner (11-03): tops M-XL.
    assert searches["vinted-peter-millar-crown-crafted"]["size"] == ["M", "L", "XL"]
    assert searches["vinted-peter-millar-polo"]["size"] == ["M", "L"]
    assert len(fast.enabled_vinted_searches()) == 62  # owner 11-08 dropped shoe and poker searches


def test_fragrance_searches_and_feeds_load_with_price_floors():
    expected = {
        "vinted-pdm-delina-exclusif": (60, 290),
        "vinted-pdm-oriana": (60, 200),
        "vinted-kilian-angels-share": (60, 230),
        # Owner 11-03: gf's Christmas ask; US retail $181/100ml.
        "vinted-burberry-goddess": (30, 100),
    }
    searches = {search["id"]: search for search in engine.SAVED_SEARCHES}
    for search_id, (minimum, maximum) in expected.items():
        search = searches[search_id]
        assert search["enabled"] is True
        assert search["profile"] == "fast"
        assert search["platforms"] == ["vinted"]
        assert search["ebay"] is False
        assert search["size"] is None
        assert search["category"] == "fragrance"
        assert (search["min_price"], search["max_price"]) == (minimum, maximum)
    assert {("delina exclusif", 290), ("oriana marly", 200), ("angels share kilian", 230),
            ("burberry goddess", 100)} <= {
        (feed["query"], feed["max_price"]) for feed in fast.VINTED_TEXT_FEEDS
    }
    assert fast.VINTED_MIN_RESALE_BY_BRAND["parfums de marly"] == 150
    assert fast.VINTED_MIN_RESALE_BY_BRAND["kilian"] == 150
    for title, display in (
        ("Parfums de Marly Delina Exclusif 75ml", "Parfums de Marly"),
        ("By Kilian Angel's Share 100ml", "Kilian"),
    ):
        result = {"listing": {"title": title}}
        assert fast._vinted_brand_and_floor(result) == (display, 150)


def test_vinted_menswear_caps_are_negotiation_adjusted_list_price_caps():
    expected_caps = {
        "vinted-peter-millar-polo": 30,
        "vinted-peter-millar-quarter-zip": 45,
        "vinted-peter-millar-cashmere-quarter-zip": 90,
        "vinted-peter-millar-jacket": 65,
        "vinted-peter-millar": 30,
        "vinted-peter-millar-crown-crafted": 50,
        "vinted-canali-suit": 130,
        "vinted-canali": 90,
        "vinted-brooks-brothers": 90,
        "vinted-e-marinella-tie": 60,
        "vinted-brunello-cucinelli": 130,
        "vinted-loro-piana": 130,
        "vinted-zegna": 75,
        "vinted-kiton": 115,
        "vinted-brioni": 90,
        "vinted-isaia": 90,
        "vinted-purple-label": 75,
        "vinted-tom-ford": 90,
        "vinted-g-fore": 30,
        "vinted-holderness-bourne": 25,
        "vinted-pdm-delina-exclusif": 290,
        "vinted-pdm-oriana": 200,
        "vinted-kilian-angels-share": 230,
        "vinted-burberry-goddess": 100,
        "vinted-seiko-presage": 250,
        "vinted-seiko-cocktail-time": 220,
        "vinted-king-seiko": 400,
        "vinted-seiko-alpinist": 400,
        "vinted-seiko-5-sports": 110,
        "vinted-tissot-prx": 250,
        "vinted-hamilton-khaki": 250,
        "vinted-hamilton-jazzmaster": 250,
        "vinted-orient-bambino": 80,
        "vinted-orient-star": 200,
        "vinted-longines": 400,
        "vinted-seiko-automatic": 150,
        "vinted-seiko-diver": 250,
        "vinted-seiko-skx": 250,
        "vinted-seiko-turtle": 300,
        "vinted-tissot-visodate": 200,
        "vinted-tissot-le-locle": 250,
        "vinted-tissot-gentleman": 250,
        "vinted-tissot-seastar": 250,
        "vinted-tissot-powermatic": 250,
        "vinted-tissot-automatic": 200,
        "vinted-orient-mako": 110,
        "vinted-orient-kamasu": 150,
        "vinted-hamilton-intra-matic": 250,
        # Owner, 10-28: super-luxury knitwear, polos and US 12-13 shoes.
        "vinted-zegna-cashmere-sweater": 130,
        "vinted-loro-piana-cashmere-sweater": 200,
        "vinted-brunello-cucinelli-cashmere-sweater": 200,
        "vinted-zegna-quarter-zip": 110,
        "vinted-loro-piana-quarter-zip": 200,
        "vinted-brunello-cucinelli-quarter-zip": 200,
        "vinted-kiton-quarter-zip": 150,
        "vinted-zegna-polo": 80,
        "vinted-loro-piana-polo": 150,
        "vinted-brunello-cucinelli-polo": 150,
        "vinted-kiton-polo": 150,
        "vinted-brioni-polo": 120,
        "vinted-isaia-polo": 100,
        "vinted-tom-ford-polo": 120,
        "vinted-loro-piana-shoes": 200,
        "vinted-brunello-cucinelli-shoes": 200,
        "vinted-zegna-shoes": 120,
        "vinted-brioni-shoes": 150,
        "vinted-kiton-shoes": 150,
        "vinted-tom-ford-shoes": 200,
        "vinted-berluti-shoes": 250,
        "vinted-john-lobb-shoes": 250,
        "vinted-edward-green-shoes": 250,
        "vinted-gaziano-girling-shoes": 250,
        "vinted-santoni-shoes": 150,
        # Owner, 10-29: designer sneakers and shell cordovan.
        "vinted-margiela-shoes": 150,
        "vinted-common-projects-shoes": 100,
        "vinted-alden-shoes": 200,
        "vinted-crockett-jones-shoes": 150,
    }
    actual = {
        search["id"]: search["max_price"]
        for search in engine.SAVED_SEARCHES
        if str(search.get("id") or "").startswith("vinted-")
    }
    assert actual == expected_caps


def test_429_backoff_schedule_starts_at_60_and_caps_at_15_minutes(tmp_path):
    sleeps = []
    lane = fast.VintedFastLane(
        searches=[],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen",
        log_path=tmp_path / "log",
        clock=lambda: 0,
        sleeper=sleeps.append,
    )
    delays = [lane._backoff(10_000) for _ in range(6)]
    assert delays == [60, 120, 240, 480, 900, 900]
    assert sleeps == delays


def test_configured_poll_interval_is_respected(tmp_path):
    sleeps = []
    response = mock.Mock(status=200)
    response.json.return_value = {"items": []}
    session = mock.Mock()
    session.get.return_value = response
    lane = fast.VintedFastLane(
        searches=[],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen",
        log_path=tmp_path / "log",
        clock=lambda: 100.0,
        sleeper=sleeps.append,
    )
    lane.session = session
    search = {"id": "watch", "query": "omega watch", "max_price": 250}
    lane._catalog_request(search)
    lane._catalog_request(search)
    assert sleeps == [fast.VINTED_FAST_POLL_SECONDS]
    # Owner wants 1-3s refresh; feeds keep total load near 1 req/s.
    assert 1 <= fast.VINTED_FAST_POLL_SECONDS <= 3


def test_jev_image_request_shape_and_resize_bounds(monkeypatch):
    source = io.BytesIO()
    Image.new("RGB", (200, 800), "red").save(source, "PNG")
    prepared = engine._prepare_jev_image(source.getvalue())
    assert prepared is not None
    _content, width, height = prepared
    assert 280 <= width <= 1568
    assert 280 <= height <= 1568

    response = mock.Mock(status_code=200)
    response.json.return_value = {
        "answers": {"matches_listing": {"noul": 0.75}}
    }
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    with (
        mock.patch.object(
            engine, "_download_jev_listing_images", return_value=[prepared[0]]
        ),
        mock.patch.object(engine.requests, "post", return_value=response) as post,
    ):
        score = engine._jev_image_score(
            {"title": "Omega watch", "description": "clean"},
            "Does this show the watch?",
        )
    assert score == 0.75
    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["TypeSafe-Danger-Zone-Images"] == "true"
    assert post.call_args.args[0] == engine.TYPESAFE_SYSTEMONE_URL
    payload = kwargs["json"]
    assert payload["assets"]
    assert set(payload["state"]["photos"]) == set(payload["assets"])
    assert all(asset["type"] == "image" for asset in payload["assets"].values())
    assert all(asset["format"] == "jpeg" for asset in payload["assets"].values())

    too_narrow = io.BytesIO()
    Image.new("RGB", (100, 2000), "red").save(too_narrow, "PNG")
    assert engine._prepare_jev_image(too_narrow.getvalue()) is None


def test_jev_image_error_and_missing_keys_fail_open(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with mock.patch.object(engine.requests, "post") as post:
        assert engine.jev_vinted_score({"title": "watch"}, "watches") is None
    post.assert_not_called()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    with (
        mock.patch.object(
            engine, "_download_jev_listing_images", return_value=[b"jpeg"]
        ),
        mock.patch.object(
            engine.requests,
            "post",
            side_effect=requests.exceptions.Timeout("slow"),
        ),
    ):
        assert engine.jev_vinted_score({"title": "watch"}, "watches") is None


def test_missing_typesafe_key_uses_openrouter_text_jev(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    response = mock.Mock()
    response.json.return_value = {
        "answers": {"matches_listing": {"noul": 0.6}}
    }
    with mock.patch.object(engine.requests, "post", return_value=response) as post:
        assert engine.jev_vinted_score({"title": "Omega watch"}, "watches") == 0.6
    assert post.call_args.args[0] == engine.JEV_DECISIONS_URL
    assert "assets" not in post.call_args.kwargs["json"]


def test_openrouter_photo_prescreen_request_shape_resize_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(fast, "VINTED_PRESCREEN_MAX_PER_RUN", 2000)  # off in config since 11-11
    lane = _lane(tmp_path)
    result = _prescreen_result()
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    downloads = [
        (_image_bytes((1024, 768), "navy"), "image/png"),
        (_image_bytes((300, 900), "gold"), "image/png"),
    ]
    with (
        mock.patch.object(
            engine, "_download_listing_image", side_effect=downloads
        ),
        mock.patch.object(
            fast.requests,
            "post",
            return_value=_prescreen_response(cost=0.000021),
        ) as post,
        mock.patch.object(fast.time, "monotonic", side_effect=[10.0, 11.25]),
    ):
        assert lane._run_vinted_prescreen(result, None) is True

    assert lane.prescreen_calls == 1
    assert post.call_args.args[0] == fast.VINTED_PRESCREEN_URL
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "google/gemma-3-12b-it"
    assert payload["temperature"] == 0
    content = payload["messages"][0]["content"]
    assert len(content) == 3
    assert content[0]["type"] == "text"
    assert "Answer false ONLY if the photo clearly shows" in content[0]["text"]
    for part in content[1:]:
        assert part["type"] == "image_url"
        data_url = part["image_url"]["url"]
        assert data_url.startswith("data:image/jpeg;base64,")
        encoded = data_url.split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            assert max(image.size) == 512
            assert image.width <= 512
            assert image.height <= 512

    assert result["vinted_prescreen_model"] == fast.VINTED_PRESCREEN_MODEL
    assert result["vinted_prescreen_answer"] is True
    assert result["vinted_prescreen_latency_seconds"] == 1.25
    assert result["vinted_prescreen_cost_usd"] == 0.000021
    lane._log_result(result)
    row = json.loads(lane.log_path.read_text(encoding="utf-8"))
    assert row["vinted_prescreen_model"] == fast.VINTED_PRESCREEN_MODEL
    assert row["vinted_prescreen_answer"] is True
    assert row["vinted_prescreen_latency_seconds"] == 1.25
    assert row["vinted_prescreen_cost_usd"] == 0.000021


def test_prescreen_falls_back_after_primary_error_or_unparseable(tmp_path, monkeypatch):
    monkeypatch.setattr(fast, "VINTED_PRESCREEN_MAX_PER_RUN", 2000)  # off in config since 11-11
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    primary_failures = (
        requests.exceptions.Timeout("primary timed out"),
        _prescreen_response("not json", cost=0.00001),
    )
    for index, primary_failure in enumerate(primary_failures):
        lane = _lane(tmp_path / f"fallback-{index}")
        result = _prescreen_result(f"fallback-{index}")
        with (
            mock.patch.object(
                engine,
                "_download_listing_image",
                return_value=(_image_bytes(), "image/png"),
            ),
            mock.patch.object(
                fast.requests,
                "post",
                side_effect=[
                    primary_failure,
                    _prescreen_response('{"answer": true}', cost=0.0001),
                ],
            ) as post,
        ):
            assert lane._run_vinted_prescreen(result, None) is True
        assert [
            call.kwargs["json"]["model"] for call in post.call_args_list
        ] == [
            fast.VINTED_PRESCREEN_MODEL,
            fast.VINTED_PRESCREEN_FALLBACK_MODEL,
        ]
        assert result["vinted_prescreen_model"] == fast.VINTED_PRESCREEN_FALLBACK_MODEL
        assert result["vinted_prescreen_answer"] is True


def test_prescreen_errors_and_unparseable_responses_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(fast, "VINTED_PRESCREEN_MAX_PER_RUN", 2000)  # off in config since 11-11
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    attempt_sets = (
        [
            requests.exceptions.Timeout("primary timed out"),
            requests.exceptions.ConnectionError("fallback unavailable"),
        ],
        [
            _prescreen_response("not json"),
            _prescreen_response('{"answer": "maybe"}'),
        ],
    )
    for index, attempts in enumerate(attempt_sets):
        lane = _lane(tmp_path / f"fail-open-{index}")
        result = _prescreen_result(f"fail-open-{index}")
        with (
            mock.patch.object(
                engine,
                "_download_listing_image",
                return_value=(_image_bytes(), "image/png"),
            ),
            mock.patch.object(fast.requests, "post", side_effect=attempts) as post,
        ):
            assert lane._run_vinted_prescreen(result, None) is None
        assert post.call_count == 2
        assert result["vinted_prescreen_answer"] is None


def test_prescreen_only_explicit_false_drops_before_real_ai(tmp_path):
    def run_case(case_name, answer):
        case_path = tmp_path / case_name
        case_path.mkdir()
        lane = _lane(case_path)
        listing, result = _listing_and_result()
        listing["itemId"] = f"vinted:{case_name}"
        lane._cheap_result = mock.Mock(return_value=(listing, result, None))
        conn = engine.init_db(lane.seen_db_path)
        with (
            mock.patch.dict(
                os.environ,
                {"TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": "test-key"},
            ),
            mock.patch.object(
                lane, "_run_vinted_prescreen", return_value=answer
            ) as prescreen,
            mock.patch.object(
                engine,
                "check_photos_with_gemini",
                return_value=_apparel_ai(100),
            ) as photo,
            mock.patch.object(
                engine,
                "_deepseek_alert_sanity_check",
                return_value={
                    "is_complete_item": True,
                    "is_part_or_accessory": False,
                    "reason": "",
                },
            ),
            mock.patch.object(engine, "send_alert") as send,
            mock.patch.object(
                lane,
                "_run_vinted_strong_vision",
                side_effect=lambda merged, _hard_stop: _record_mock_strong(
                    lane, merged, _passing_strong_result()
                ),
            ),
        ):
            lane._process_item(
                conn, None, {}, {"query": result["search_query"]}, None
            )
        conn.close()
        prescreen.assert_called_once()
        return photo, send

    rejected_photo, rejected_send = run_case("explicit-false", False)
    rejected_photo.assert_not_called()
    rejected_send.assert_not_called()
    open_photo, open_send = run_case("unknown-fails-open", None)
    open_photo.assert_called_once()
    open_send.assert_called_once()


def test_prescreen_per_run_cap_stops_additional_requests(tmp_path, monkeypatch):
    lane = _lane(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    first = _prescreen_result("cap-first")
    second = _prescreen_result("cap-second")
    with (
        mock.patch.object(fast, "VINTED_PRESCREEN_MAX_PER_RUN", 1),
        mock.patch.object(
            engine,
            "_download_listing_image",
            return_value=(_image_bytes(), "image/png"),
        ) as download,
        mock.patch.object(
            fast.requests, "post", return_value=_prescreen_response()
        ) as post,
    ):
        assert lane._run_vinted_prescreen(first, None) is True
        assert lane._run_vinted_prescreen(second, None) is None
    assert lane.prescreen_calls == 1
    assert post.call_count == 1
    assert download.call_count == 2
    assert "per-run prescreen cap of 1 reached" == second["vinted_prescreen_reason"]


def test_low_jev_score_skips_real_ai(tmp_path):
    _lane_obj, photo, strong, send, seen = _run_one_pipeline(
        tmp_path, jev_score=0.05
    )
    photo.assert_not_called()
    strong.assert_not_called()
    send.assert_not_called()
    assert seen


def test_ai_pass_sends_alert(tmp_path):
    _lane_obj, photo, strong, send, seen = _run_one_pipeline(tmp_path)
    photo.assert_called_once()
    strong.assert_called_once()
    send.assert_called_once()
    assert seen


def test_ai_failure_never_sends_alert(tmp_path):
    lane = _lane(tmp_path)
    listing, result = _listing_and_result()
    lane._cheap_result = mock.Mock(return_value=(listing, result, None))
    conn = engine.init_db(lane.seen_db_path)
    with (
        mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}),
        mock.patch.object(engine, "jev_vinted_score", return_value=0.8),
        mock.patch.object(engine, "check_photos_with_gemini", return_value=None),
        mock.patch.object(engine, "send_alert") as send,
    ):
        lane._process_item(conn, None, {}, {"query": result["search_query"]}, None)
    send.assert_not_called()
    assert not engine.is_new(conn, listing["itemId"])
    conn.close()


def test_lane_ai_cap_logs_and_marks_seen_without_call(tmp_path):
    lane, photo, strong, send, seen = _run_one_pipeline(tmp_path, cap=0)
    photo.assert_not_called()
    strong.assert_not_called()
    send.assert_not_called()
    assert seen
    row = json.loads(lane.log_path.read_text(encoding="utf-8"))
    assert row["disposition_code"] == "NO_AI_BUDGET"


def test_main_marketplace_prefetch_can_exclude_vinted(tmp_path):
    called = []
    conn = engine.init_db(tmp_path / "main.db")
    search = {
        "id": "watch-omega",
        "query": "omega watch",
        "enabled": True,
        "platforms": ["vinted"],
    }
    with (
        mock.patch.object(engine, "SAVED_SEARCHES", [search]),
        mock.patch.object(engine, "FOCUS_SEARCH_IDS", []),
        mock.patch.object(engine, "MARKETPLACES_ENABLED", ["vinted"]),
        mock.patch.object(engine.scout_queue, "load_scout_queue", return_value=[]),
        mock.patch.dict(
            marketplaces.ADAPTERS,
            {"vinted": lambda _search: called.append(True) or ([], 0)},
            clear=False,
        ),
    ):
        result = engine.prefetch_marketplaces(
            datetime.now(timezone.utc), conn, include_vinted=False
        )
    conn.close()
    assert result == {}
    assert called == []


def test_golf_shadow_records_probability_without_skipping(tmp_path):
    listing = {
        "itemId": "golf-shadow-1",
        "title": "TaylorMade P790 5-PW iron set",
        "price": {"value": 200},
        "itemWebUrl": "https://example.test/golf",
    }
    result = {
        "listing": listing,
        "verdict": "REVIEW",
        "category": "golf-equipment",
        "price": 212,
    }
    with (
        mock.patch.object(engine, "JEV_IMAGE_GOLF_MODE", "shadow"),
        mock.patch.object(engine, "_golf_image_jev_score", return_value=0.01),
    ):
        assert not engine._apply_golf_image_jev_screen(result, listing)
    log_path = tmp_path / "golf.jsonl"
    engine.append_alert_log(result, log_path=log_path)
    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["jev_image_iron_set"] == 0.01


def test_generic_peter_millar_polo_below_resale_floor_never_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Basic Golf Polo Size M",
        12,
        "golf",
        "peter millar polo -women",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(25)
    )
    strong.assert_not_called()
    send.assert_not_called()
    assert "below $30 brand floor" in row["reason"]


def test_cheap_luxury_piece_alerts_below_its_brand_floor(tmp_path):
    # Owner, 10-28: $9.50 Zegna sweater worth $75 was rejected by the $100 floor.
    listing, result = _apparel_listing_result(
        "Ermenegildo Zegna Men's Sweater", 9.5, "knitwear", "zegna",
        search_id="vinted-zegna",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(75),
        strong_result=_passing_strong_result(75),
    )
    strong.assert_called_once()
    send.assert_called_once()
    assert row["delivered"] is True


def test_brooks_brothers_keeps_full_resale_floor(tmp_path):
    listing, result = _apparel_listing_result(
        "Brooks Brothers Wool Suit 42R", 15, "tailoring", "brooks brothers",
        search_id="vinted-brooks-brothers",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(80)
    )
    send.assert_not_called()
    assert "below $150 brand floor" in row["reason"]


def test_peter_millar_ten_dollar_list_at_35_resale_alerts_after_second_look(
    tmp_path,
):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Polo Size L",
        10,
        "other",
        "peter millar crown crafted -women",
        search_id="vinted-pm-ten-dollar-steal",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(35),
        strong_result=_passing_strong_result(
            35, premium_reason="Crown Crafted label is clearly visible", size_on_tag="L"
        ),
    )
    strong.assert_called_once()
    send.assert_called_once()
    assert row["delivered"] is True
    assert round(row["vinted_expected_paid"], 2) == 10.6
    assert row["deal_rating"] == "Great Deal"


def test_peter_millar_twenty_dollar_list_at_35_resale_does_not_alert(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Polo Size L",
        20,
        "other",
        "peter millar crown crafted -women",
        search_id="vinted-pm-twenty-not-steal",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(35),
        strong_result=_passing_strong_result(35),
    )
    send.assert_not_called()
    assert round(row["vinted_expected_paid"], 2) == 21.2
    # 11-10 price table: a used PM polo sells for ~$30, so the $35 guess is cut.
    assert row["estimated_resale_value"] == 30
    assert row["deal_rating"] == "Fair"
    assert "below Great Deal" in row["reason"]


def test_peter_millar_crown_crafted_cashmere_quarter_zip_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Cashmere Quarter Zip Size M",
        7,
        "knitwear",
        "peter millar crown crafted quarter zip",
        search_id="vinted-pm-crown-cashmere",
    )
    lane, strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(120),
        strong_result=_passing_strong_result(
            110,
            premium_reason="Crown Crafted cashmere tag is clearly visible",
            size_on_tag="L",
        ),
    )
    strong.assert_called_once()
    send.assert_called_once()
    assert row["delivered"] is True
    assert lane.ai_calls == 2
    # The $120/$110 AI guesses are cut to the $42 table price (11-10).
    assert row["vinted_conservative_resale_value"] == 42


def test_vinted_strong_condition_fair_vetoes(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Cashmere Quarter Zip",
        18,
        "knitwear",
        "peter millar crown crafted quarter zip",
        search_id="vinted-pm-good-condition",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(120),
        strong_result=_passing_strong_result(
            120, condition_grade="fair", flaws_found=["heavy pilling"]
        ),
    )
    send.assert_not_called()
    assert "condition grade fair below good" in row["reason"]


def test_vinted_strong_condition_good_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Zegna Cashmere sweater",
        10,
        "knitwear",
        "zegna",
        search_id="vinted-zegna-good-condition",
    )
    _lane_obj, _strong, send, _row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(150),
        strong_result=_passing_strong_result(
            150, condition_grade="good", flaws_found=["light wear"]
        ),
    )
    send.assert_called_once()


def test_instant_alert_only_for_luxury_apparel_at_giveaway_price(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    cases = [
        ("Zegna Cashmere sweater", 10, "knitwear", True),
        ("Zegna Cashmere sweater", 40, "knitwear", False),  # over 25% of $100 floor
        ("Zegna silk tie", 5, "other", False),
        # PM instant ping is off by default (11-04: 3/3 pings were bad).
        ("Peter Millar Crown Crafted quarter zip", 5, "knitwear", False),
        ("Peter Millar polo shirt", 10, "golf", False),
        ("Loro Piana cashmere crewneck", 30, "knitwear", True),
        ("Canali two thin pullover seaters navy blue and burgundy", 20, "knitwear", True),
        ("Zegna polo shirt", 15, "golf", True),
        ("Canali Camisa de manga larga", 8, "other", False),  # mis-tagged shirt
        ("Parfums de Marly Delina Exclusif perfume 75ml", 10, "fragrance", False),
        ("Tom Ford Ombré Leather Mini", 12, "other", False),
        ("Brooks Brothers Mens suit", 22, "tailoring", False),
        # Audit 11-03: trousers/jeans and top-house shirts never pinged.
        ("Canali navy wool dress pants", 13, "other", True),
        ("Kiton jeans", 20, "other", True),
        ("Brioni dress shirt", 10, "other", True),
        # Owner 11-06: titled only with the brand; the verified alert was too late.
        ("Brunello Cucinelli", 20, "other", True),
        ("Kiton", 20, "other", True),
        # Owner 11-08: elite knitwear pings up to $60; other pieces stay at 25%.
        ("Loro Piana cashmere sweater", 60, "knitwear", True),
        ("Loro Piana cashmere sweater", 61, "knitwear", False),
        ("Loro Piana wool jacket", 45, "outerwear", False),
        ("Canali cashmere sweater", 45, "knitwear", False),
        # 11-07: sellers of many dress shirts title them only "Canali"/"Zegna".
        ("Ermenegildo Zegna", 10, "other", False),
        ("Canali", 20, "other", False),
        ("Brunello Cucinelli bathrobe", 4, "other", False),
    ]
    for title, price, category, expected in cases:
        _listing, result = _apparel_listing_result(
            title, price, category, title.split()[0].lower(), search_id="s"
        )
        lane._instant_pinged.clear()
        with mock.patch.object(fast.requests, "post") as post:
            assert lane._maybe_instant_alert(result, category) is expected, title
        assert post.called is expected, title
        if expected:
            headers = post.call_args.kwargs["headers"]
            assert headers["Priority"] == "5"
            assert headers["Title"].startswith("FAST ")
            assert " M: " in headers["Title"]  # size survives iOS body truncation
            # The owner's all-in includes 6% tax, not just list + fee + shipping.
            body = post.call_args.kwargs["data"]
            body = body.decode("utf-8") if isinstance(body, bytes) else body
            all_in = (price + fast.engine.get_shipping_cost(_listing)) * 1.06
            assert f"(~${all_in:.0f} all-in)" in body, (title, body)


def test_alert_watch_times_a_listing_leaving_the_feed(tmp_path, caplog):
    lane = _lane(tmp_path)
    now = [100.0]
    lane.clock = lambda: now[0]
    lane._first_seen["vinted:7"] = 90.0
    lane._watch_alert({"itemId": "vinted:7", "title": "Brunello Cucinelli"})
    lane._watch_alert({"itemId": "vinted:8", "title": "text feed only"})
    with caplog.at_level("INFO"):
        lane._update_alert_watch([{"id": 7}])
        now[0] = 112.0
        lane._update_alert_watch([{"id": 1}])  # one miss can be a flaky page
        assert "timing" not in caplog.text
        lane._update_alert_watch([{"id": 1}])
    assert "first alert 10s after first seen, gone from feed 22s" in caplog.text
    assert "vinted:7" not in lane._alert_watch
    assert "vinted:8" in lane._alert_watch  # never in this feed, so never "gone"
    now[0] = 800.0
    lane._update_alert_watch([{"id": 1}])
    assert not lane._alert_watch


def test_pm_instant_needs_the_brand_in_the_sellers_own_title(tmp_path, monkeypatch):
    # Owner 11-04: a Greg Norman shirt brand-tagged Peter Millar pinged.
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(fast, "VINTED_PM_INSTANT_MAX_PRICE", 10.0)
    lane = _lane(tmp_path)
    for seller_title, expected in (
        ("Greg Norman golf polo", False),
        ("Mens polo shirt", False),
        ("Mens large peter miller polo shirt", True),
    ):
        _listing, result = _apparel_listing_result(
            f"Peter Millar {seller_title}", 8, "golf", "peter millar polo", search_id="s"
        )
        result["listing"]["sellerTitle"] = seller_title
        lane._instant_pinged.clear()
        with mock.patch.object(fast.requests, "post") as post:
            assert lane._maybe_instant_alert(result, "golf") is expected, seller_title
        assert post.called is expected


def test_canali_over_all_in_cap_does_not_alert_at_list(tmp_path, monkeypatch):
    listing, result = _apparel_listing_result(
        "Canali men's sweater", 26, "knitwear", "canali", search_id="vinted-canali"
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(300),
        strong_result=_passing_strong_result(300),
    )
    send.assert_not_called()
    assert "all-in cap" in row["reason"]

    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    for price, expected in ((26, False), (10, True)):
        _listing, result = _apparel_listing_result(
            "Canali men's sweater", price, "knitwear", "canali", search_id="s"
        )
        lane._instant_pinged.clear()
        with mock.patch.object(fast.requests, "post"):
            assert lane._maybe_instant_alert(result, "knitwear") is expected, price


def test_luxury_instant_needs_exact_house_tag_or_sellers_title(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    for tag, seller_title, expected in (
        ("carlos Brioni", "Cardigan", False),
        ("Brioni", "Cardigan", True),
        ("carlos Brioni", "Brioni cashmere cardigan", True),
        ("Ermenegildo Zegna", "Cashmere sweater", True),
    ):
        _listing, result = _apparel_listing_result(
            f"{tag} {seller_title}", 20, "knitwear", "brioni", search_id="s"
        )
        result["listing"]["sellerTitle"] = seller_title
        lane._instant_pinged.clear()
        with mock.patch.object(fast.requests, "post") as post:
            assert lane._maybe_instant_alert(result, "knitwear") is expected, tag
        assert post.called is expected


def test_web_catalog_row_keeps_the_sellers_own_title():
    row = fast._web_item_to_catalog_item({"productItem": {
        "id": 7, "title": "Polo shirt", "url": "/items/7", "itemBox": {"firstLine": "Peter Millar"},
    }})
    assert row["title"] == "Peter Millar Polo shirt"
    assert row["seller_title"] == "Polo shirt"
    listing = marketplaces._vinted_item_to_listing({**row, "price": {"amount": "8"}})
    assert listing["sellerTitle"] == "Polo shirt"


def test_instant_prepass_pings_from_catalog_row_before_any_item_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    conn = engine.init_db(lane.seen_db_path)
    item = _raw_item("zegna-1")
    item["title"] = "Zegna Cashmere sweater"
    item["price"] = {"amount": "10"}
    search = {"id": "s", "query": "zegna", "category": "knitwear"}
    with (
        mock.patch.object(fast.requests, "post") as post,
        mock.patch.object(engine, "fetch_vinted_item_page_details") as details,
        mock.patch.object(fast, "fetch_vinted_seller_stats") as seller,
    ):
        lane._instant_prepass(conn, None, [item], "search", search)
        lane._instant_prepass(conn, None, [item], "search", search)
    conn.close()
    details.assert_not_called()
    seller.assert_not_called()
    post.assert_called_once()
    # The full pipeline's later call is a no-op but keeps the flag.
    _listing, result = _apparel_listing_result(
        "Zegna Cashmere sweater", 10, "knitwear", "zegna", search_id="s"
    )
    lane._instant_pinged.add(result["listing"]["itemId"])
    with mock.patch.object(fast.requests, "post") as post:
        assert lane._maybe_instant_alert(result, "knitwear") is False
    post.assert_not_called()
    assert result["vinted_instant_alerted"] is True


def test_brand_feed_peek_pings_without_moving_high_water(tmp_path):
    lane = _lane(tmp_path)
    feed = {"id": "feed-brands", "brand_ids": [1]}
    state = {"searches": {}}
    lane._peek_ctx = (None, None, state, feed)
    items = [_raw_item(5)]
    with (
        mock.patch.object(lane, "_brand_catalog_request", return_value=(200, items)),
        mock.patch.object(lane, "_instant_prepass") as prepass,
    ):
        lane._peek_brand_feed()
    prepass.assert_called_once()
    assert state == {"searches": {}}
    lane._last_brand_poll = lane.clock()
    with mock.patch.object(lane, "_brand_catalog_request") as request:
        lane._peek_brand_feed()  # polled moments ago: skip
    request.assert_not_called()
    lane._last_brand_poll = None
    # Audit 11-11: a catalog-page 429 pauses only the brand slots.
    with mock.patch.object(lane, "_brand_catalog_request", return_value=(429, None)):
        lane._peek_brand_feed()
    assert lane._brand_paused()
    with mock.patch.object(lane, "_brand_catalog_request") as request:
        lane._peek_brand_feed()
    request.assert_not_called()
    lane._brand_paused_until = 0
    with (
        mock.patch.object(lane, "_brand_catalog_request", return_value=(403, None)),
        pytest.raises(fast.VintedRequestBlocked),
    ):
        lane._peek_brand_feed()


def test_unsized_brand_search_drops_stated_sizes_owner_cant_wear():
    off = [
        ("S", ""), ("XXL", ""), ("3XL", ""), ("S / 36-38", ""), ("XS", ""),
        ("", "Peter Millar Flannel size XXL"),
        ("", "Peter Millar Polo Size Small"),
        ("", "Men’s Small Zegna Sweater"),
        ("", "Ermenegildo Zegna Trofeo EU 56 US 46L Blazer"),
        ("", "Like New Peter Millar 40/32 pants"),
        ("W30", "Zegna chinos"), ("", "Brioni trousers 38R"),
        ("XXL", "Loro Piana cashmere polo"),  # fitted piece: never XXL
        ("XXL", "Peter Millar crewneck sweater"),  # $5 but floor too low
    ]
    fits = [
        ("S/M", ""), ("L", ""), ("XL", ""), ("M / 38-40", ""), ("One size", ""),
        ("", "Brioni Suit 42R"), ("", "Brioni Men's Sweater"),
        ("", "Canali Richards Wool Blazer Men's 50R"),  # EU 50 = US 40
        ("", "Canali Navy Wool Dress Pants – Men’s Size 34R"),
        ("", "Jeans W34 L32"), ("", "Zegna trousers 33x32"), ("50", "Brioni trousers"),
        ("XXL", "Loro Piana cashmere crewneck sweater"),  # $5: megasteal knit
    ]
    def reason(size, title):
        listing = {"size": size, "title": title, "price": {"value": 5}}
        return fast._off_size_reason(listing, "other")
    for size, title in off:
        assert reason(size, title), (size, title)
    for size, title in fits:
        assert reason(size, title) is None, (size, title)
    assert fast._off_size_reason({"size": "S"}, "footwear") is None
    # Letter-sized searches can't name a waist or the XXL exception.
    lettered = {"query": "peter millar pants", "size": ["M", "L", "XL"]}
    pants = {"title": "Peter Millar pants 34x30", "price": {"value": 20}}
    assert fast._fits_size_list(lettered, pants, "golf")
    pants["title"] = "Peter Millar pants 40x30"
    assert not fast._fits_size_list(lettered, pants, "golf")
    # Vinted sizes pants "W32" and suits "40R" whatever the title says (11-03).
    bare = {"title": "Peter Millar size 32", "size": "W32", "price": {"value": 20}}
    assert fast._fits_size_list(lettered, bare, "other")
    assert fast._off_size_reason(bare, "other") is None
    bare.update(size="W40", title="Peter Millar")
    assert not fast._fits_size_list(lettered, bare, "other")
    assert fast._off_size_reason(bare, "other")
    suit = {"title": "Peter Millar charcoal Gray suit", "size": "42R", "price": {"value": 20}}
    assert fast._fits_size_list(lettered, suit, "other")
    suit["size"] = "48R"
    assert not fast._fits_size_list(lettered, suit, "other")
    knit = {"title": "Loro Piana cashmere sweater", "size": "XXL", "price": {"value": 30}}
    assert fast._fits_size_list(lettered, knit, "knitwear")
    knit["price"] = {"value": 300}
    assert not fast._fits_size_list(lettered, knit, "knitwear")


def test_slow_ai_call_keeps_polling_the_brand_feed(tmp_path):
    import threading

    lane = _lane(tmp_path)
    lane._peek_ctx = object()
    peeked = threading.Event()
    with mock.patch.object(
        lane, "_peek_brand_feed", side_effect=lambda: peeked.set()
    ) as peek:
        assert lane._while_peeking(lambda: 42 if peeked.wait(5) else None) == 42
    peek.assert_called()
    finished = []

    def slow():
        peeked.wait(5)
        finished.append(True)

    peeked.clear()
    def blocked():
        peeked.set()
        raise fast.VintedRequestBlocked(429)

    with mock.patch.object(lane, "_peek_brand_feed", side_effect=blocked):
        lane._while_peeking(slow)
    # The paid AI result is kept; the run loop backs off after the item.
    assert finished == [True]
    assert isinstance(lane._pending_block, fast.VintedRequestBlocked)


def test_brand_feed_asks_for_mens_catalog_with_short_timeout(tmp_path):
    lane = _lane(tmp_path)
    lane.session = mock.Mock()
    lane.session.get.return_value = mock.Mock(status=200, body="")
    lane._brand_catalog_request({"brand_ids": [1, 2], "max_price": 250})
    kwargs = lane.session.get.call_args.kwargs
    assert ("catalog[]", 5) in kwargs["params"]
    assert kwargs["timeout"] == fast.VINTED_BRAND_FEED_TIMEOUT == 4


def test_text_feed_titles_get_the_seller_picked_brand(tmp_path):
    lane = _lane(tmp_path)
    response = mock.Mock(status=200)
    response.json.return_value = {"items": [
        {"title": "Cashmere sweater", "item_box": {"first_line": "Loro Piana"}},
        {"title": "Loro Piana scarf", "item_box": {"first_line": "Loro Piana"}},
    ]}
    lane.session = mock.Mock()
    lane.session.get.return_value = response
    _status, items = lane._catalog_request({"id": "t", "query": "loro piana"})
    assert [item["title"] for item in items] == ["Loro Piana Cashmere sweater", "Loro Piana scarf"]


def test_catalog_row_rules_reject_before_the_item_page_request(tmp_path):
    lane = _lane(tmp_path)
    conn = engine.init_db(lane.seen_db_path)
    polo = {"id": "s", "query": "peter millar polo", "size": ["L"], "category": "golf"}
    cases = [
        ("M", polo, "size mismatch for saved search"),
        ("M / US 8-10", {"id": "s", "query": "peter millar", "category": "golf"}, "women's"),
        ("L", {**polo, "min_price": 50}, "below fake-price floor"),
    ]
    with mock.patch.object(engine, "fetch_vinted_item_page_details") as details:
        for index, (size, search, reason) in enumerate(cases):
            item = _raw_item(index)
            item["size_title"] = size
            _listing, result, why = lane._cheap_result(conn, None, item, search)
            assert result is None and reason in why, (size, why)
    details.assert_not_called()
    conn.close()


def test_negated_flaws_and_description_only_logo_words_do_not_reject():
    fail = engine.condition_hard_fail_keyword
    assert fail("", "great shape, no stains or holes") is None
    assert fail("", "no holes, stains, or tears. stain-free") is None
    assert fail("", "small stain on the cuff") == "stain"
    assert fail("", "seiko stainless steel") is None
    listing = marketplaces.make_listing(
        "vinted", "x", "Peter Millar cashmere quarter zip", 20, "https://v/x", shipping=0
    )
    listing["description"] = "Club collar. Open to offers!"
    result = engine.score_listing(listing, None, shipping_cost=0, category="knitwear")
    assert result.get("reason") != "corporate logo keyword match"
    listing["title"] = "Peter Millar Pebble Beach Club quarter zip"
    result = engine.score_listing(listing, None, shipping_cost=0, category="knitwear")
    assert result.get("reason") == "corporate logo keyword match"


def _openrouter_reply(resale=400):
    reply = mock.Mock()
    reply.json.return_value = {
        "choices": [{"message": {"content": json.dumps(_passing_strong_result(resale))}}]
    }
    return reply



def test_strong_look_timeout_is_not_retried_a_third_time(tmp_path, monkeypatch):
    # Owner 11-10: paid backup first; if it fails, one free Gemini try, no pause.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    lane = _lane(tmp_path)
    lane.sleeper = mock.Mock()
    _listing, result = _apparel_listing_result(
        "Loro Piana Cashmere Sweater", 30, "knitwear", '"loro piana"', search_id="s"
    )
    timeout = requests.exceptions.ReadTimeout("slow")
    with (
        mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
        mock.patch.object(
            engine, "_download_listing_image", return_value=(b"jpeg", "image/jpeg")
        ),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
        mock.patch.object(engine, "_call_gemini_json", side_effect=[timeout] * 3) as gemini,
        mock.patch.object(fast.requests, "post", side_effect=timeout),
    ):
        assert not lane._run_vinted_strong_vision(result, None)
    assert gemini.call_count == 1
    assert gemini.call_args.kwargs["timeout"] <= fast.VINTED_STRONG_PRIMARY_TIMEOUT_SECONDS
    lane.sleeper.assert_not_called()


def test_common_no_flaw_wordings_do_not_hard_fail():
    for text in (
        "No rips, tears, or stains",
        "no pilling, stains, or holes",
        "No fading, stains, holes",
        "no known stains",
        "doesn't have any stains",
        "does not have stains",
        "no signs of wear or stains",
        "stain release finish",
        "no stains on the shirt",
        "no stains or holes in the fabric",
    ):
        assert engine.condition_hard_fail_keyword("x", text) is None, text
    for text in (
        "small stain on cuff",
        "no rips, stain on the cuff",
        "no rips or tears, stain on collar",
        "no holes but a stain on the back",
    ):
        assert engine.condition_hard_fail_keyword("x", text) == "stain", text


def test_belt_length_must_fit_the_owner():
    belt = "Tom Ford black leather belt"
    assert fast._off_size_reason({"size": "44 in", "title": belt + " Size 110 cm"}, "other")
    assert fast._off_size_reason({"title": belt + " 110 cm"}, "leather-goods")
    assert fast._off_size_reason({"size": "36 in", "title": belt}, "other") is None
    assert fast._off_size_reason({"title": belt + " 95cm"}, "other") is None
    assert fast._off_size_reason({"title": belt}, "other") is None


def test_lettered_suit_only_fits_at_large():
    search = {"size": ["40", "42", "44"], "size_equivalents": ["50", "52", "54"]}
    suit = {"title": "Canali Proposta Men's Black Tux Set", "size": "L"}
    assert fast._fits_size_list(search, suit, "tailoring")
    assert not fast._fits_size_list(search, {**suit, "size": "M"}, "tailoring")
    assert not fast._fits_size_list(search, {**suit, "size": "XL"}, "tailoring")


def test_stray_photo_of_a_woman_is_not_womenswear():
    stray = (
        "Peter Millar Raleigh chino, size 36. One photo appears to be an "
        "irrelevant stock photo of a woman in a green dress."
    )
    assert "woman" not in fast._STRAY_PHOTO_SENTENCE.sub(" ", stray).lower()
    real = "Photos show a women's blouse."
    assert "women" in fast._STRAY_PHOTO_SENTENCE.sub(" ", real).lower()


def test_moth_balls_are_not_a_garment_ping():
    assert fast._VINTED_INSTANT_SKIP_WORDS.search(
        "Loro Piana Cedar Wood Moth Protection Balls Cashmere Care"
    )


def test_offer_view_prices_a_25_dollar_sweater_at_15():
    view = fast._offer_view({"item_price": 25.0, "estimated_resale_value": 60.0})
    assert view["item_price"] == 15.0
    # (15 + 0.75 + 0.70 + 4.99) * 1.06
    assert round(view["price"], 2) == 21.67
    assert view["vinted_expected_paid"] == view["price"]
    assert view["deal_rating"] == "Great Deal"
    # Never below Vinted's 40%-off floor.
    assert fast._offer_view({"item_price": 13.33, "estimated_resale_value": 60.0})[
        "item_price"
    ] == 8.0
    assert fast._offer_view({"item_price": 25.0}) is None


def _offer_ai(resale):
    return {
        "damage_found": False,
        "weird_logo_found": False,
        "looks_good": True,
        "counterfeit_suspected": False,
        "summary": "Canali wool jacket in clean condition",
        "visible_brand_evidence": "Canali neck label",
        "estimated_retail_price": 900,
        "estimated_resale_value": resale,
        "price_confidence": "high",
        "liquidity": "fast",
    }


def _run_offer(tmp_path, price, resale, title="Canali wool jacket", category="outerwear",
               offer_brands=frozenset({"canali"})):
    listing, result = _apparel_listing_result(title, price, category, "canali jacket")
    with mock.patch.object(fast.VintedFastLane, "_ntfy") as ntfy, \
            mock.patch.object(fast, "VINTED_OFFER_BRANDS", offer_brands):
        _lane_obj, _strong, send, row = _run_custom_pipeline(
            tmp_path, listing, result, _offer_ai(resale),
            strong_result=_passing_strong_result(resale=resale),
        )
    return row, ntfy, send


def test_near_miss_of_any_brand_gets_an_offer_alert_not_a_buy_alert(tmp_path):
    # Good Deal at list; the ~$83 all-in offer is under 40% of a $210 resale.
    row, ntfy, send = _run_offer(tmp_path, 115, 210)
    send.assert_not_called()
    assert row["verdict"] == "OFFER_ALERTED"
    args = ntfy.call_args.args
    assert args[0].startswith("OFFER Canali $69 (list $115)")
    assert args[2] == "4"


def test_offers_only_for_the_top_five_houses(tmp_path):
    # Owner 11-11: a near miss outside LP/BC/Kiton/Brioni/Zegna gets no offer.
    row, ntfy, send = _run_offer(tmp_path, 115, 210, offer_brands=fast.VINTED_OFFER_BRANDS)
    send.assert_not_called()
    assert row["verdict"] == "REJECTED"
    assert not any(c.args[0].startswith(("OFFER", "MESSAGE")) for c in ntfy.call_args_list)
    assert fast.VINTED_OFFER_BRANDS == {"brunello cucinelli", "kiton", "loro piana", "brioni", "zegna"}


def test_near_miss_that_only_works_at_half_gets_a_message_alert(tmp_path):
    row, ntfy, send = _run_offer(tmp_path, 115, 180)
    send.assert_not_called()
    assert row["verdict"] == "OFFER_ALERTED"
    args = ntfy.call_args.args
    assert args[0].startswith("MESSAGE Canali $58 (list $115)")
    assert '"I can do $58"' in args[1]
    assert args[2] == "3"


def test_offer_checks_leave_half_the_strong_looks_for_buy_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Canali wool jacket", 115, "outerwear", "canali jacket"
    )
    lane = _lane(tmp_path)
    lane.strong_look_calls = fast.VINTED_STRONG_LOOK_MAX_PER_RUN // 2
    result.update(
        {
            "vinted_photo_ai_checked": True,
            "counterfeit_suspected": False,
            "damage_found": False,
            "price_confidence": "high",
            "estimated_resale_value": 210,
        }
    )
    conn = engine.init_db(lane.seen_db_path)
    with mock.patch.object(lane, "_run_vinted_strong_vision") as strong:
        assert not lane._try_offer_alert(conn, result, "outerwear", {}, {"query": "x"}, None)
    conn.close()
    strong.assert_not_called()


def test_near_miss_below_the_list_floor_gets_no_offer_or_message(tmp_path, monkeypatch):
    # Same near-miss as the message test above, with the floor above its $115 list.
    monkeypatch.setattr(fast, "VINTED_OFFER_MIN_LIST", 116)
    monkeypatch.setattr(fast, "VINTED_OFFER_MIN_LIST_TOPS", 116)
    row, ntfy, send = _run_offer(tmp_path, 115, 180)
    send.assert_not_called()
    assert row["verdict"] != "OFFER_ALERTED"
    assert not any(c.args[0].startswith(("OFFER", "MESSAGE")) for c in ntfy.call_args_list)


def test_cheap_top_gets_a_message_alert_but_cheap_pants_do_not(tmp_path):
    # Owner 11-10: polos list at $10-$20, under the $25 offer floor. A $20
    # polo with $45 resale fails the 40% veto at list and at the $12 offer,
    # and passes at a $10 message (~$17 all-in). Pants keep the $25 floor.
    row, ntfy, _send = _run_offer(tmp_path, 20, 45, "Canali polo", "other")
    assert row["verdict"] == "OFFER_ALERTED"
    assert ntfy.call_args.args[0].startswith("MESSAGE Canali $10 (list $20)")

    (tmp_path / "p").mkdir()
    row, ntfy, _send = _run_offer(tmp_path / "p", 20, 45, "Canali pants", "other")
    assert row["verdict"] != "OFFER_ALERTED"
    assert not any(c.args[0].startswith(("OFFER", "MESSAGE")) for c in ntfy.call_args_list)


def test_offer_that_still_fails_at_half_stays_rejected(tmp_path):
    row, ntfy, send = _run_offer(tmp_path, 115, 160)
    send.assert_not_called()
    ntfy.assert_not_called()
    assert row["verdict"] == "REJECTED"


def test_offer_alert_daily_cap_reserves_slots_for_steals():
    conn = sqlite3.connect(":memory:")
    assert fast._offer_slot_open(conn, "Great Deal")
    now = fast.time.time()
    open_slots = fast.VINTED_OFFER_DAILY_CAP - fast.VINTED_OFFER_STEAL_RESERVE
    for n in range(open_slots):
        conn.execute("INSERT INTO offer_alerts VALUES (?, ?, 'offer')", (str(n), now))
    assert not fast._offer_slot_open(conn, "Great Deal")
    # Messages aren't limited by Vinted, so they keep their own count.
    assert fast._offer_slot_open(conn, "Great Deal", "message")
    assert fast._offer_slot_open(conn, "Steal")
    for n in range(open_slots, fast.VINTED_OFFER_DAILY_CAP):
        conn.execute("INSERT INTO offer_alerts VALUES (?, ?, 'offer')", (str(n), now))
    assert not fast._offer_slot_open(conn, "Steal")
    conn.execute("UPDATE offer_alerts SET sent_at = ?", (now - 90000,))
    assert fast._offer_slot_open(conn, "Great Deal")


def test_full_cashmere_zip_needs_peter_millar_and_all_cashmere():
    def result(title, brand="peter millar"):
        return {"vinted_brand_key": brand, "listing": {"title": title}}

    assert fast._is_full_cashmere_zip(result("Peter Millar 100% cashmere quarter zip"), {})
    assert fast._is_full_cashmere_zip(
        result("Peter Millar 1/4 zip"), {"fabric_from_tag": "100% Cashmere"}
    )
    assert not fast._is_full_cashmere_zip(
        result("Peter Millar quarter zip"), {"fabric_from_tag": "95% cotton 5% cashmere"}
    )
    assert not fast._is_full_cashmere_zip(result("Peter Millar 100% cashmere crewneck"), {})
    assert not fast._is_full_cashmere_zip(result("100% cashmere quarter zip", "zegna"), {})


def test_full_cashmere_zip_passes_knitwear_bar_at_great_deal_only():
    base = {
        "vinted_fast_lane": True,
        "vinted_min_resale_floor": 30,
        "item_price": 40.0,
        "discount_pct": 55,
        "price_confidence": "medium",
        "deal_rating": "Great Deal",
        "peter_millar_back_crown_visible": True,
        "listing": {"title": "Peter Millar 100% cashmere quarter zip"},
    }
    knit = "knitwear bar: deal_rating"
    plain = engine.is_blocked_by_steal_quality_gate(base, category="knitwear")
    assert plain and knit in plain
    cashmere = {**base, "vinted_full_cashmere_zip": True}
    assert not engine.is_blocked_by_steal_quality_gate(cashmere, category="knitwear")
    good = {**cashmere, "deal_rating": "Good Deal", "discount_pct": 35}
    assert knit in engine.is_blocked_by_steal_quality_gate(good, category="knitwear")


def test_verified_vinted_alert_breaks_through_ios_focus():
    # Medium confidence, 60% off: was Priority 3, which iOS Focus silences.
    result = {
        "listing": {"platform": "vinted"},
        "discount_pct": 60,
        "price_confidence": "medium",
        "deal_rating": "Great Deal",
    }
    assert engine.alert_urgency(result)[0] == 5
    result["listing"]["platform"] = "ebay"
    assert engine.alert_urgency(result)[0] == 3


def test_strong_look_vetoes_a_third_party_logo(tmp_path):
    lane = _lane(tmp_path)
    result = {
        "listing": {"title": "Peter Millar jacket", "price": {"value": 15}},
        "category": "outerwear",
        "vinted_strong_checked": True,
        "vinted_strong_label_other_brand": False,
        "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_third_party_logo": True,
        "vinted_strong_logo_desc": "Pebble Beach crest on chest",
    }
    assert "Pebble Beach" in lane._strong_look_veto_reason(result)
    result["vinted_strong_third_party_logo"] = False
    assert "logo" not in (lane._strong_look_veto_reason(result) or "")
    assert "third_party_logo_found" in fast._vinted_strong_prompt(result, "Peter Millar")


def test_strong_look_vetoes_a_tag_size_the_owner_cannot_wear(tmp_path):
    lane = _lane(tmp_path)
    result = {
        "listing": {"title": "Peter Millar golf shirt", "price": {"value": 15}},
        "category": "golf",
        "vinted_strong_checked": True,
        "vinted_strong_label_other_brand": False,
        "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_size_on_tag": "XXL",
    }
    assert "tag size XXL" in lane._strong_look_veto_reason(result)
    result["vinted_strong_size_on_tag"] = "52R"  # EU 52 = US 42
    assert "tag size" not in (lane._strong_look_veto_reason(result) or "")


def test_block_seen_during_ai_backs_off_after_the_item_finishes(tmp_path):
    ticks = iter(x / 100 for x in range(10_000))
    search = {"id": "s", "query": "peter millar polo", "platforms": ["vinted"]}
    lane = fast.VintedFastLane(
        searches=[search], state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db", log_path=tmp_path / "lane.jsonl",
        main_seen_db_path=tmp_path / "missing-main.db", run_seconds=0.5,
        clock=lambda: next(ticks), sleeper=lambda _seconds: None,
    )
    old_water = fast._item_marker(_raw_item(0, timestamp=1))
    (tmp_path / "state.json").write_text(json.dumps({"searches": {"s": old_water}}))

    def process(*_args):
        lane._pending_block = fast.VintedRequestBlocked(429)

    with (
        mock.patch.object(lane, "_catalog_request", return_value=(200, [_raw_item(1)])),
        mock.patch.object(lane, "_instant_prepass"),
        mock.patch.object(lane, "_peek_brand_feed"),
        mock.patch.object(lane, "_process_item", side_effect=process) as processed,
        mock.patch.object(lane, "_backoff") as backoff,
    ):
        lane.run()
    processed.assert_called()
    backoff.assert_called()
    assert lane._pending_block is None


def test_quoted_brand_shoe_searches_accept_any_shoe_title(tmp_path):
    lane = fast.VintedFastLane(state_path=tmp_path / "s", seen_db_path=tmp_path / "d",
                               log_path=tmp_path / "l", schedule=[])

    def item(title):
        return {"id": 1, "title": title, "url": "/items/1", "price": {"amount": "150"},
                "photo": {"url": "https://x/a.webp"}, "photos": [], "item_box": {}}

    # "shoes" was a stopword but "shoe" was not, so these needed the literal
    # word "shoe" and the feeds produced nothing for 66 h (audit 11-03).
    # Owner 11-08 dropped the shoe searches: no shoe search takes these now.
    assert lane._route(item("Edward Green Dover shoes"), "edward green") is None
    assert lane._route(item("John Lobb City II"), "john lobb") is None
    # A catch-all-backed text feed still won't drop a blazer into shoes.
    assert lane._route(item("Zegna wool blazer 50L"), "zegna") is None
    # "cucinelli" is a partial house name; over-cap pieces must not fall into
    # the Brunello shoe or polo search (11-06: $165 shirt -> shoe size mismatch).
    assert lane._route(item("Brunello Cucinelli Men's M Gingham Cotton Shirt"), "cucinelli") is None
    assert lane._route(item("Brunello Cucinelli Crest Logo Cotton T-Shirt"), "cucinelli") is None
    assert lane._route(item("Brunello Cucinelli suede loafers"), "cucinelli") is None


def test_relaxed_waist_and_regex_false_positives():
    assert fast._pants_waist_fits({"title": "Peter Millar Golf Shorts Men's 34"}) is True
    assert fast._pants_waist_fits({"title": "Peter Millar Golf Shorts-40"}) is False
    assert fast._off_profile_reason("Canali brown leather double monk strap dress shoe") is None
    assert fast._off_profile_reason("Zegna silk wrap dress") == "women's dress, not menswear"
    assert not fast._VINTED_NOT_MENS.search("Peter Millar baby blue shorts")
    assert not fast._VINTED_NOT_MENS.search("Pinehurst Putter Boy polo")
    assert fast._VINTED_NOT_MENS.search("boys polo size 10")
    assert "quarter" in fast._route_words("Peter Millar Qtr Zip")


def test_brand_peek_block_in_item_loop_backs_off_instead_of_crashing(tmp_path):
    ticks = iter(x / 100 for x in range(10_000))
    search = {"id": "s", "query": "peter millar polo", "platforms": ["vinted"]}
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        main_seen_db_path=tmp_path / "missing-main.db",
        run_seconds=0.5,
        clock=lambda: next(ticks),
        sleeper=lambda _seconds: None,
    )
    old_water = fast._item_marker(_raw_item(0, timestamp=1))
    (tmp_path / "state.json").write_text(json.dumps({"searches": {"s": old_water}}))
    with (
        mock.patch.object(lane, "_catalog_request", return_value=(200, [_raw_item(1)])),
        mock.patch.object(lane, "_instant_prepass"),
        mock.patch.object(
            lane, "_peek_brand_feed", side_effect=fast.VintedRequestBlocked(429)
        ),
        mock.patch.object(lane, "_process_item") as process,
        mock.patch.object(lane, "_backoff") as backoff,
    ):
        lane.run()
    backoff.assert_called()
    process.assert_not_called()
    # Blocked before processing: the item stays unseen for the next poll.
    saved = json.loads((tmp_path / "state.json").read_text())["searches"]["s"]
    assert saved == json.loads(json.dumps(old_water))


def test_strong_look_uses_openrouter_when_gemini_rate_limits_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    lane = _lane(tmp_path)
    _listing, result = _apparel_listing_result(
        "Loro Piana Cashmere Sweater", 40, "other", '"loro piana"', search_id="s"
    )
    limited = requests.exceptions.HTTPError("quota", response=mock.Mock(status_code=429))
    reply = mock.Mock()
    reply.json.return_value = {
        "choices": [{"message": {"content": json.dumps(_passing_strong_result(400))}}]
    }
    with (
        mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
        mock.patch.object(
            engine, "_download_listing_image", return_value=(b"jpeg", "image/jpeg")
        ),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
        mock.patch.object(engine, "_call_gemini_json", side_effect=[limited] * 3),
        mock.patch.object(fast.requests, "post", return_value=reply) as post,
    ):
        assert lane._run_vinted_strong_vision(result, None)
    assert post.call_args.kwargs["json"]["model"] == fast.VINTED_STRONG_OPENROUTER_MODEL
    assert result["vinted_strong_model"].startswith("openrouter:")


@pytest.mark.parametrize("first_alerted", [True, False])
def test_identical_title_and_price_batch_is_rejected_before_ai(tmp_path, first_alerted):
    # Audit 11-11: a same-seller repeat is a relist only if the first alerted;
    # otherwise it is a closet-dump sibling and gets its own look.
    lane = _lane(tmp_path)
    conn = engine.init_db(lane.seen_db_path)
    first = _listing_and_result()
    second = _listing_and_result()
    second[0]["itemId"] = "v|duplicate"
    first[0]["seller"] = second[0]["seller"] = {"username": "vinted:closet"}
    second[1]["listing"] = second[0]
    second[0]["price"] = {"value": float(first[0]["price"]["value"]) + 0.2, "currency": "USD"}
    # Another seller's identical apparel listing is a closet dump, not a scam.
    third = _listing_and_result()
    third[0]["itemId"] = "v|other-seller"
    third[0]["seller"] = {"username": "vinted:someone-else"}
    third[1]["listing"] = third[0]
    lane._cheap_result = mock.Mock(
        side_effect=[(*first, None), (*second, None), (*third, None)]
    )
    if first_alerted:
        lane._alerted_item_ids.add(first[0]["itemId"])
    with (
        mock.patch.object(engine, "check_photos_with_gemini", return_value=None) as photo,
        mock.patch.object(lane, "_run_vinted_prescreen", return_value=None),
    ):
        # Brand-feed rows carry only user.id, never the login.
        for user_id in (7, 7, 8):
            lane._process_item(
                conn, None, {"user": {"id": user_id}}, {"query": first[1]["search_query"]}, None
            )
    assert photo.call_count == (2 if first_alerted else 3)
    assert not engine.is_new(conn, "v|duplicate")
    conn.close()


def test_luxury_necktie_skips_ai_but_marinella_tie_does_not(tmp_path):
    listing, result = _apparel_listing_result(
        "Brioni Silk Necktie", 15, "other", "brioni", search_id="vinted-brioni"
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(150),
        strong_result=_passing_strong_result(150),
    )
    strong.assert_not_called()
    send.assert_not_called()
    assert "ties are off" in row["reason"]  # owner 11-11


def test_eyewear_and_zero_review_luxury_sellers_skip_ai(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    listing, result = _apparel_listing_result(
        "Brunello Cucinelli Sunglasses", 65, "other", "brunello cucinelli",
        search_id="vinted-cucinelli",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path / "a", listing, result, _apparel_ai(300),
        strong_result=_passing_strong_result(300),
    )
    strong.assert_not_called()
    send.assert_not_called()
    assert "eyewear" in row["reason"]

    listing, result = _apparel_listing_result(
        "Loro Piana Cashmere sweater", 40, "knitwear", "loro piana",
        search_id="vinted-loro",
    )
    listing["seller_feedback_count"] = 0
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path / "b", listing, result, _apparel_ai(400),
        strong_result=_passing_strong_result(400),
    )
    assert "0 reviews" not in (row.get("reason") or "")  # menswear: owner said legit


def test_womens_fragrance_routes_ignores_gender_and_reaches_real_ai(tmp_path):
    search = _fragrance_search()
    item = _fragrance_item(
        "delina-womens",
        "Parfums de Marly Delina Exclusif Women's EDP 75ml sealed",
        180,
    )
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    routed = lane._route(item, "delina exclusif")
    assert routed["id"] == "vinted-pdm-delina-exclusif"
    conn = engine.init_db(lane.seen_db_path)
    details = {
        "_http_status": 200,
        "description": "New sealed women's perfume, full retail bottle",
        "seller_feedback_count": 12,
        "image_urls": [],
    }
    with (
        mock.patch.object(engine, "fetch_vinted_item_page_details", return_value=details),
        mock.patch.object(engine, "jev_vinted_score") as jev,
        mock.patch.object(lane, "_run_vinted_prescreen") as prescreen,
        mock.patch.object(
            engine, "check_photos_with_gemini", return_value=_fragrance_ai()
        ) as photo,
        mock.patch.object(
            engine,
            "_deepseek_alert_sanity_check",
            return_value={
                "is_complete_item": True,
                "is_part_or_accessory": False,
                "reason": "",
            },
        ),
        mock.patch.object(engine, "send_alert") as send,
    ):
        lane._process_item(conn, None, item, routed, None)
    conn.close()
    jev.assert_not_called()
    prescreen.assert_not_called()
    photo.assert_called_once()
    assert photo.call_args.kwargs["category"] == "fragrance"
    send.assert_called_once()


@pytest.mark.parametrize(
    ("title", "price", "expected_reason"),
    [
        (
            "Parfums de Marly Delina Exclusif Women's EDP 75ml sealed",
            46,
            "below fake-price floor: $46 < $60 (almost certainly counterfeit)",
        ),
        (
            "Delina Exclusif decant 10ml",
            220,
            "fragrance listing is a decant/sample/tester/dupe/empty, not a sealed full bottle",
        ),
    ],
)
def test_fragrance_fake_price_floor_and_decants_reject_before_ai(
    tmp_path, title, price, expected_reason
):
    search = _fragrance_search()
    item = _fragrance_item("cheap-reject", title, price)
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    conn = engine.init_db(lane.seen_db_path)
    details = {
        "_http_status": 200,
        "description": "Sealed full retail bottle",
        "seller_feedback_count": 10,
        "image_urls": [],
    }
    with (
        mock.patch.object(engine, "fetch_vinted_item_page_details", return_value=details),
        mock.patch.object(engine, "check_photos_with_gemini") as photo,
    ):
        _listing, result, reason = lane._cheap_result(conn, None, item, search)
    conn.close()
    assert result is None
    assert reason == expected_reason
    photo.assert_not_called()


def test_parses_vinted_catalog_breadcrumb():
    page = (
        '<a href="/catalog/1904-women?referrer=item-crumbs" itemProp="url">'
        '<span itemProp="title">Women</span></a>'
        '<a href="/catalog/26-sunglasses?referrer=item-crumbs" itemProp="url">'
        '<span itemProp="title">Sunglasses</span></a>'
    )
    resp = mock.Mock(status_code=200, text=page)
    with mock.patch.object(engine.requests, "get", return_value=resp):
        details = engine.fetch_vinted_item_page_details("https://www.vinted.com/items/1")
    assert details["catalog_path"] == ["Women", "Sunglasses"]


def test_price_table_cuts_inflated_resale_and_never_raises_it():
    def capped(title, resale, summary="", key="estimated_resale_value", index=0):
        result = {"listing": {"title": title}, key: resale}
        fast._cap_to_price_table(result, key, summary, index)
        return result[key]

    # 11-10: the $20 "Brioni" alert's AI said $90 / strong $150; Brioni dress
    # shirts sell used for $30-$100. The title has no piece; the summary does.
    assert capped("Brioni", 150.0, "Brioni white cotton dress shirt") == 60
    assert capped("Peter Millar Summer Comfort polo L", 75.0) == 30
    assert capped("Canali merino sweater", 950.0, key="estimated_retail_price", index=1) == 550
    assert capped("Loro Piana cashmere quarter zip", 350.0) == 350  # never raised
    assert capped("Peter Millar pants 34", 45.0) == 45  # not in the table
    assert capped("Brioni suit jacket", 400.0, "Brioni shirt") == 400


@pytest.mark.parametrize(("title", "rejected"), [
    # Real 11-10 titles. Vague "shirt" goes to the photo check; clear woven cues don't.
    ("Peter Millar pullover shirt", False),
    ("Peter Millar Men's collared shirt", False),
    ("Peter Millar long sleeve men's shirt", False),
    ("Peter Millar Shirt Mens Large Blue Multicolor Plaid Cotton", True),
    ("Peter Millar Men's White Shirt with Brown & Blue Windowpane Check-Large", True),
    ("Peter Millar Shirt Mens XL Blue Plaid Cotton Button Up Long Sleeve", True),
    ("Canali Shirt Mens 16.5 / 42 Made in Italy White Striped", True),
])
def test_title_rejects_only_clear_dress_shirts(title, rejected):
    reason = fast._menswear_shape_reason({"title": title, "size": "L"}, "peter millar")
    assert (reason is not None and "dress shirt" in reason) is rejected


def test_photo_summary_mascot_and_hanger_words_need_context():
    # Audit 11-11: "bulldog print" and "on a hanger" rejected clean PM polos.
    assert fast._off_profile_reason("PM polo with a small bulldog print", schools_only=True) is None
    assert fast._off_profile_reason("polo with Georgia Bulldogs logo", schools_only=True)
    assert fast._off_profile_reason("Georgia Bulldogs Polo, Peter Millar")
    assert fast._ai_lone_tailoring_reason("Blue Peter Millar polo on a hanger", "other") is None
    assert fast._ai_lone_tailoring_reason("Tom Ford 100ml EDP bottle", "other")


def test_cloth_mill_suits_and_followup_numbers(tmp_path):
    shape = fast._menswear_shape_reason
    cremieux = {"title": "Daniel Cr\u00e9mieux Daniel Cremieux 40R Brown Loro Piana Wool 3Btn Suit",
                "size": "40R", "price": {"value": 23}}
    assert "another maker" in shape(cremieux, "loro piana")
    assert shape({"title": "Loro Piana wool suit 42R", "size": "42R", "price": {"value": 90}},
                 "loro piana") is None
    lane = _lane(tmp_path)
    text = lane._followup_numbers({
        "listing": {"title": "x", "shippingOptions": [{"shippingCost": {"value": 5.0}}]},
        "item_price": 23, "estimated_retail_price": 900, "estimated_resale_value": 120,
    })
    assert "$23 list" in text and "retail ~$900" in text and "resale ~$120" in text
    sent = []
    lane._ntfy = lambda *a, **k: sent.append(a)
    lane._instant_pinged.add("vinted:55")
    lane._followup_if_silent({"id": 55, "title": "Brioni shirt"})
    lane._followup_if_silent({"id": 55, "title": "Brioni shirt"})
    assert len(sent) == 1 and sent[0][0].startswith("CHECK UNFINISHED")


def test_alert_says_when_shipping_is_assumed():
    assert "assumed $3.99" in fast._ship_note({})
    assert fast._ship_note({"vinted_shipping": 1.99}) == ""


def test_vtg_rare_zegna_titles_are_mainline_shirts():
    assert fast._shirt_house("zegna", "Rare Vtg Ermenegildo Zegna Dot Print Cotton L/S M")
    assert not fast._shirt_house("zegna", "Tommy Hilfiger Ermenegildo Zegna Button Up")


def test_spelled_out_sizes_match_in_title_not_description():
    # Audit 11-11: "Men's Large Peter Millar Pullover" failed an L/XL search.
    search = {"size": ["L", "XL"]}
    assert engine.listing_matches_size_filter(search, {"title": "Men's Large Peter Millar Pullover"})
    assert engine.listing_matches_size_filter(search, {"title": "PM quarter zip Extra Large"})
    assert not engine.listing_matches_size_filter(
        search, {"title": "PM quarter zip", "size": "M", "description": "large logo, medium weight"}
    )


def test_review_fixes_price_caps_null_reply_and_age(tmp_path, monkeypatch):
    def row(title):
        return fast._price_table_row({"listing": {"title": title}})
    # "Polo Ralph Lauren" is the brand, not a polo shirt.
    assert row("Polo Ralph Lauren Purple Label cashmere sweater") == (235, 1200)
    assert row("Ralph Lauren Purple Label polo shirt") == (65, 450)
    for title in ("Loro Piana cashmere overcoat", "Canali cashmere sportcoat",
                  "Zegna knit sneakers", "Brioni silk ties"):
        assert row(title) is None, title
    # Title decides the piece; garments never borrow another garment's row.
    assert row("Kiton polo shirt") is None
    assert row("Loro Piana cashmere quarter zip") == (430, 1500)
    assert fast._price_table_row(
        {"listing": {"title": "Zegna cashmere sweater"}}, "navy shirt collar visible"
    ) == (275, 1400)
    assert fast._price_table_row(
        {"listing": {"title": "Brioni"}}, "white dress shirt, spread collar"
    ) == (60, 850)
    # A null OpenRouter content is a failed look, not a crashed run.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    lane = _lane(tmp_path)
    reply = mock.Mock()
    reply.json.return_value = {"choices": [{"message": {"content": None}}]}
    with (
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
        mock.patch.object(fast.requests, "post", return_value=reply),
    ):
        assert lane._openrouter_strong_look("p", [(b"j", "image/jpeg")], None) is None
    # A bumped first item no longer skews later ages.
    lane._note_listing_age("vinted:old", "10700000000")
    lane._note_listing_age("vinted:new", "10752000000")
    lane._note_listing_age("vinted:mid", str(10752000000 - 90 * 9000))
    assert lane._listed_age_m["vinted:mid"] == 90


def test_first_photo_check_goes_to_paid_flash_lite_then_free_backup(tmp_path, monkeypatch):
    # Owner 11-10: free Gemini 20 s timeouts made alerts land after the sale.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    lane = _lane(tmp_path)
    prepared = {"prompt": "p", "images": [(b"jpeg", "image/jpeg")]}
    with (
        mock.patch.object(engine, "check_photos_with_gemini", return_value=prepared),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
        mock.patch.object(engine, "_call_photo_check") as free,
        mock.patch.object(fast.requests, "post", return_value=_openrouter_reply()) as post,
    ):
        assert lane._first_photo_check({}, "knitwear", None, "q")["estimated_resale_value"]
    free.assert_not_called()
    assert post.call_args.kwargs["json"]["model"] == fast.VINTED_PHOTO_OPENROUTER_MODEL
    assert post.call_args.kwargs["timeout"] <= fast.VINTED_PHOTO_OPENROUTER_TIMEOUT_SECONDS
    with (
        mock.patch.object(engine, "check_photos_with_gemini", return_value=prepared),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
        mock.patch.object(engine, "_call_photo_check", return_value={"ok": 1}) as free,
        mock.patch.object(fast.requests, "post", side_effect=requests.exceptions.ReadTimeout("x")),
    ):
        assert lane._first_photo_check({}, "knitwear", None, "q") == {"ok": 1}
    free.assert_called_once_with(
        "p", prepared["images"], timeout=fast.VINTED_PHOTO_OPENROUTER_TIMEOUT_SECONDS, hard_stop=None,
    )


def test_resurfaced_old_listing_is_dated_from_its_item_id(tmp_path):
    # Owner 11-10: a zip knit jacket listed 6 h earlier alerted as if new.
    lane = _lane(tmp_path)
    now = [0.0]
    lane.clock = lambda: now[0]
    lane._note_listing_age("vinted:a", "10752000000")      # run start, frontier
    now[0] = 1200.0                                          # 20 min, +180k ids
    lane._note_listing_age("vinted:b", "10752180000")
    assert lane._listed_age_m["vinted:b"] == 0
    lane._note_listing_age("vinted:old", str(10752180000 - 6 * 60 * 9000))
    assert lane._listed_age_m["vinted:old"] == 360
    assert fast._age_note(360).startswith("Listed ~6h ago")
    assert fast._age_note(25) == ""
    assert "days" in fast._age_note(7 * 1440)


@pytest.mark.parametrize(("banner", "price"), [
    ('<span data-testid="item-shipping-banner-price">from $0.00</span>', 0.0),
    ('<span data-testid="item-shipping-banner-price"><span>from</span> <span>US$3.49</span></span>', 3.49),
    ('<div data-testid="item-shipping-banner-price" class="x">From&nbsp;$5.79</div>', 5.79),
    ('<span data-testid="item-shipping-banner-price">Free shipping</span>', 0.0),
    ("<p>no banner</p>", None),
    ('<span data-testid="item-shipping-banner-price">Free</span><span>Fee $1.35</span>', 0.0),
    # Owner 11-11: real logged-out page of a "from $1.99" listing.
    ('<h2 data-testid="item-shipping-banner-title">Free shipping</h2></div><span '
     'data-testid="item-shipping-banner-price"></span></div><div>$0.70 + 5%</div>', None),
])
def test_shipping_banner_formats(banner, price):
    # Audit 11-11: the strict pattern read only ~35% of pages.
    resp = mock.Mock(status_code=200, text=banner)
    with mock.patch.object(engine.requests, "get", return_value=resp):
        details = engine.fetch_vinted_item_page_details("https://www.vinted.com/items/1")
    assert details.get("shipping_price") == price


def test_real_listing_shipping_replaces_the_499_assumption():
    # Owner 11-10: 10681756063 showed "Shipping from $0.00" (logged-out HTML).
    page = (
        '<div id="item-shipping-banner--suffix"><span class="web_ui__Text__text" '
        'data-testid="item-shipping-banner-price">from $0.00</span></div>'
    )
    resp = mock.Mock(status_code=200, text=page)
    with mock.patch.object(engine.requests, "get", return_value=resp):
        details = engine.fetch_vinted_item_page_details("https://www.vinted.com/items/1")
    assert details["shipping_price"] == 0.0

    # Catalog row: $13 list, $1.35 buyer-protection fee + the $4.99 assumption.
    listing = marketplaces.make_listing(
        "vinted", "1", "Peter Millar polo", 13, "https://www.vinted.com/items/1",
        shipping=1.35 + marketplaces.VINTED_ASSUMED_SHIPPING,
    )
    fast._set_listing_shipping(listing, details["shipping_price"])
    assert engine.get_shipping_cost(listing) == 1.35
    fast._set_listing_shipping(listing, 3.49)
    assert engine.get_shipping_cost(listing) == 4.84

    # The offer price uses it too: (12 + 0.60 + 0.70 + 0.00) * 1.06
    fast._set_listing_shipping(listing, 0)
    view = fast._offer_view(
        {"item_price": 20.0, "estimated_resale_value": 45.0, "listing": listing}
    )
    assert round(view["price"], 2) == 14.10


@pytest.mark.parametrize(
    ("search_id", "top", "rejected"),
    [
        ("vinted-seiko-presage", "Women", True),
        ("vinted-seiko-presage", "Kids", True),
        ("vinted-seiko-presage", "Men", False),
        ("vinted-pdm-delina-exclusif", "Women", False),
        ("poker-paulson", "Kids", False),
        ("poker-paulson", "Women", True),
    ],
)
def test_womens_and_kids_catalog_rejects_except_perfume(tmp_path, search_id, top, rejected):
    search = _watch_search(search_id)
    title = "Paulson poker chip set" if search_id.startswith("poker") else "Seiko Presage SRPB43 automatic"
    item = _fragrance_item("cat", title, 150)
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    conn = engine.init_db(lane.seen_db_path)
    details = {"_http_status": 200, "catalog_path": [top, "Accessories"]}
    with (
        mock.patch.object(engine, "fetch_vinted_item_page_details", return_value=details),
        mock.patch.object(engine, "check_photos_with_gemini"),
    ):
        out = lane._cheap_result(conn, None, item, search)
    conn.close()
    reason = out[2] if out else None
    assert (reason == f"Vinted category is {top.lower()}, not menswear") is rejected


def test_removed_listing_rejects_before_ai(tmp_path):
    search = _fragrance_search()
    item = _fragrance_item("gone", "Parfums de Marly Delina Exclusif 75ml sealed", 220)
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    conn = engine.init_db(lane.seen_db_path)
    with (
        mock.patch.object(
            engine, "fetch_vinted_item_page_details", return_value={"_http_status": 404}
        ),
        mock.patch.object(engine, "check_photos_with_gemini") as photo,
    ):
        _listing, result, reason = lane._cheap_result(conn, None, item, search)
    conn.close()
    assert result is None
    assert reason == "listing already removed from Vinted (HTTP 404)"
    photo.assert_not_called()


def test_zero_review_fragrance_seller_rejects_before_ai(tmp_path):
    search = _fragrance_search()
    item = _fragrance_item(
        "zero-review-fragrance", "Parfums de Marly Delina Exclusif 75ml sealed", 220
    )
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    conn = engine.init_db(lane.seen_db_path)
    with (
        mock.patch.object(
            engine,
            "fetch_vinted_item_page_details",
            return_value={
                "_http_status": 200,
                "description": "Sealed full retail bottle",
                "seller_feedback_count": 0,
                "image_urls": [],
            },
        ),
        mock.patch.object(engine, "check_photos_with_gemini") as photo,
    ):
        lane._process_item(conn, None, item, search, None)
    conn.close()
    photo.assert_not_called()
    row = json.loads(lane.log_path.read_text(encoding="utf-8"))
    assert "seller has 0 reviews" in row["reason"]


@pytest.mark.parametrize(
    ("ai_result", "should_alert", "reason_fragment"),
    [
        (
            _fragrance_ai(counterfeit_suspected=True, counterfeit_reason="wrong box font"),
            False,
            "counterfeit assessment not explicitly clear",
        ),
        (_fragrance_ai(), True, None),
        (
            _fragrance_ai(summary="Authentic bottle, partially used and about 70% full"),
            False,
            "used/partially-full bottle",
        ),
    ],
)
def test_fragrance_ai_bar_requires_clean_great_deal(
    tmp_path, ai_result, should_alert, reason_fragment
):
    listing, result = _apparel_listing_result(
        "Parfums de Marly Delina Exclusif Women's EDP 75ml sealed",
        180,
        "fragrance",
        "delina exclusif",
        search_id="vinted-pdm-delina-ai-bar",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, ai_result
    )
    strong.assert_not_called()
    assert send.called is should_alert
    if should_alert:
        assert row["deal_rating"] == "Great Deal"
    else:
        assert reason_fragment in row["reason"]


@pytest.mark.parametrize(
    ("stats", "fragment"),
    [
        ({"_http_status": 200, "feedback_count": 0.0, "feedback_reputation": 0.0,
          "item_count": 1.0, "given_item_count": 0.0}, "0 reviews, 0.0 stars, 1 items"),
        (dict(TRUSTED_SELLER, feedback_reputation=0.7), "3.5 stars"),
        (dict(TRUSTED_SELLER, feedback_count=1.0, feedback_reputation=0.8,
              item_count=1.0, given_item_count=1.0), "2 items"),
        (dict(TRUSTED_SELLER, feedback_count=0.0, feedback_reputation=0.0), "0 reviews"),
        ({"_http_status": 500}, "history unavailable"),
        (dict(TRUSTED_SELLER, closet=[("1", "Tom Ford Oud Wood EDP 100ml"),
                                      ("2", "Flowerbomb 3.4 oz"), ("3", "Men's EDT")]),
         "perfume-shop"),
    ],
)
def test_fragrance_needs_a_proven_seller(tmp_path, monkeypatch, stats, fragment):
    monkeypatch.setattr(fast, "fetch_vinted_seller_stats", lambda _user_id: stats)
    listing, result = _apparel_listing_result(
        "Parfums de Marly Delina Exclusif 75ml sealed",
        220,
        "fragrance",
        "delina exclusif",
        search_id="vinted-pdm-delina-seller",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _fragrance_ai()
    )
    send.assert_not_called()
    assert "seller" in row["reason"] and fragment in row["reason"]


def test_menswear_new_seller_is_not_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(fast, "fetch_vinted_seller_stats", lambda _user_id: {
        "_http_status": 200, "feedback_count": 0.0, "feedback_reputation": 0.0,
        "item_count": 1.0, "given_item_count": 0.0,
    })
    listing, result = _apparel_listing_result(
        "Loro Piana Cashmere sweater", 40, "knitwear", "loro piana", search_id="vinted-loro"
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(400), strong_result=_passing_strong_result(400)
    )
    assert "seller" not in (row.get("reason") or "")


def test_menswear_seller_rule_only_cuts_obvious_throwaways():
    loose = lambda **kw: fast._seller_trust_reason(
        dict({"feedback_count": 0.0, "feedback_reputation": 0.0,
              "item_count": 0.0, "given_item_count": 0.0}, **kw), strict=False)
    assert loose(item_count=1.0) is None  # brand-new, one listing
    assert loose(item_count=13.0) is None  # new seller clearing a closet
    assert loose(feedback_count=4.0, feedback_reputation=0.8, item_count=45.0) is None
    assert loose(feedback_count=5.0, feedback_reputation=0.6, item_count=20.0) is not None
    assert fast._seller_trust_reason({"_http_status": 500}, strict=False) is None


@pytest.mark.parametrize(
    ("title", "size", "brand", "blocked"),
    [
        ("Peter Millar polo", "XL", None, True),
        ("Peter Millar polo", "L", None, False),
        ("Jack Victor x Loro Piana Blazer 46 L", "46", "loro piana", True),
        ("Tom Ford sport coat 42R", "42", "tom ford", True),
        ("Kiton suit pants", "34", "kiton", True),
        ("Canali suit 42R", "42", "canali", False),
        ("Zegna suit 40L", "", "zegna", False),
        ("Canali suit 46", "", "canali", True),
        ("Canali suit 42S", "", "canali", True),
        ("Peter Millar men's dress shirt", "L", "peter millar", True),
        ("Kiton dress shirt", "16", "kiton", False),
        ("Peter Millar T-shirt", "L", "peter millar", False),
        ("Zegna mens sweater", "L", "zegna", False),
        # Owner 11-08: mainline Zegna dress shirts are wanted for work.
        ("Ermenegildo Zegna Centoquaranta Lavender Check Cotton Dress Shirt L", "L", "zegna", False),
        ("Zegna Shirt Mens Medium White Plaid Long Slv Button Up", "M", "zegna", False),
        ("Men’s Zegna dress shirt", "L", "zegna", False),
        ("Tommy Hilfiger Ermenegildo Zegna Button Up Shirt Mens L", "L", "zegna", True),
        ("Z Zegna button down shirt", "L", "zegna", True),
        ("Zegna Sport button up shirt", "L", "zegna", True),
        ("Canali dress shirt", "L", "canali", True),
    ],
)
def test_menswear_shape_rule(title, size, brand, blocked):
    reason = fast._menswear_shape_reason({"title": title, "size": size}, brand)
    assert (reason is not None) is blocked, reason


@pytest.mark.parametrize(
    ("title", "description", "blocked"),
    [
        ("Jos A Bank Loro Piana fabric suit 42R", "", True),
        ("Jack Victor x Loro Piana suit 42R", "", True),
        ("Mens suit 42R", "Made with Ermenegildo Zegna cloth", True),
        ("Canali suit 42R", "Made with Ermenegildo Zegna fabric", False),
        ("Loro Piana cashmere sweater", "", False),
    ],
)
def test_mill_fabric_from_other_makers_is_not_the_house(title, description, blocked):
    reason = fast._menswear_shape_reason(
        {"title": title, "size": "L", "description": description}, None
    )
    assert (reason is not None) is blocked, reason


def test_named_fabric_makers_are_on_pass_list():
    for title in ("jos. a. bank loro piana suit", "joseph abboud zegna blazer"):
        assert engine.brand_in(title, engine.PASS_BRANDS)


@pytest.mark.parametrize(
    ("summary", "blocked"),
    [
        ("Zegna navy wool sport coat, two-button, size 42R", True),
        ("Tailored wool jacket in grey herringbone", True),
        ("Canali navy two-piece suit with matching trousers", False),
        ("Zegna quilted down jacket", False),
    ],
)
def test_ai_summary_lone_sport_coat_is_rejected(summary, blocked):
    reason = fast._ai_lone_tailoring_reason(summary, "other")
    assert (reason is not None) is blocked, reason


def _watch_search(search_id="vinted-seiko-presage"):
    return next(s for s in engine.SAVED_SEARCHES if s["id"] == search_id)


@pytest.mark.parametrize(
    ("title", "routed"),
    [
        ("Seiko Presage SRPB43 automatic", "vinted-seiko-presage"),
        ("Tissot PRX Powermatic 80 40mm", "vinted-tissot-prx"),
        ("Seiko watch stainless", None),  # no line named: never a catch-all
    ],
)
def test_watch_text_feed_routes_only_named_lines(tmp_path, title, routed):
    lane = fast.VintedFastLane(
        searches=fast.enabled_vinted_searches(),
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    item = _fragrance_item("watch-route", title, 60, brand=title.split()[0])
    search = lane._route(item, source_text=title.split()[0])
    assert (search or {}).get("id") == routed


@pytest.mark.parametrize(
    ("title", "description", "fragment"),
    [
        ("Seiko Presage NH35 custom build", "", "mod/homage"),
        ("Seiko Presage two-tone", "", "mod/homage"),
        ("Seiko Presage SRPB43", "Has an aftermarket dial", "mod/homage"),
        ("Seiko Presage SRPB43", "Message me on WhatsApp for a deal", "off Vinted"),
    ],
)
def test_watch_scam_shapes_rejected_before_ai(tmp_path, title, description, fragment):
    search = _watch_search()
    item = _fragrance_item("watch-scam", title, 150)
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "state",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    conn = engine.init_db(lane.seen_db_path)
    with (
        mock.patch.object(engine, "fetch_vinted_item_page_details", return_value={
            "_http_status": 200, "description": description, "image_urls": [],
        }),
        mock.patch.object(engine, "check_photos_with_gemini") as photo,
    ):
        lane._process_item(conn, None, item, search, None)
    conn.close()
    photo.assert_not_called()
    row = json.loads(lane.log_path.read_text(encoding="utf-8"))
    assert fragment in row["reason"]


@pytest.mark.parametrize(
    ("title", "blocked"),
    [
        ("Tom ford tobacco vanille, 100ml", True),
        ("Brunello Cucinelli Eau de Parfum Pour Homme 30ml", True),
        ("Tom Ford Slim Lip Color Shine", True),
        ("Tom Ford 3.4 oz cologne", True),
        ("Tom Ford cashmere crewneck sweater", False),
        ("Zegna 100% cashmere sweater", False),
    ],
)
def test_beauty_items_never_route_through_menswear(title, blocked):
    reason = fast._menswear_shape_reason({"title": title, "size": "L"}, "tom ford")
    assert (reason is not None) is blocked, reason


@pytest.mark.parametrize(
    ("title", "size", "blocked"),
    [
        ("Vintage Ermenegildo Zegna Double Breasted Jacket, 42R", "42", True),
        ("Canali sport jacket 42R", "42", True),
        ("Canali suit jacket and pants 42R", "42", False),
        ("Zegna quilted down jacket", "XL", False),
        ("Zegna rusty orange button up", "L", True),
        ("Peter Millar button down cardigan", "L", False),
        ("Belford Mock Neck Sweater Italian Yarn Zegna Baruffa", "XL", True),
        ("Loro piana empty shoe storage box", "", True),
        ("Canali men all over shower gel", "", True),
        ("Brunello Cucinelli Skinny Jeans", "M / US 8-10", True),
        ("Boys Brunello Cucinelli loafers (12 child)", "12", True),
        ("Peter Millar Crown Sport quarter zip", "M / US 38-40", False),
    ],
)
def test_live_audit_shapes(title, size, blocked):
    reason = fast._menswear_shape_reason({"title": title, "size": size}, None)
    assert (reason is not None) is blocked, reason


def test_steal_only_size_needs_a_steal():
    search = {"size": ["L", "XL"], "steal_only_size": ["M"]}
    medium = {"title": "Peter Millar Gamecocks quarter zip", "size": "M"}
    large = {"title": "Peter Millar Gamecocks quarter zip", "size": "L"}
    small = {"title": "Peter Millar Gamecocks quarter zip", "size": "S"}
    assert fast._fits_size_list(search, medium, "school-gear")
    assert not fast._fits_size_list(search, small, "school-gear")
    assert fast._steal_only_size_reason({"listing": medium, "deal_rating": "Great Deal"}, search)
    assert fast._steal_only_size_reason({"listing": medium, "deal_rating": "Steal"}, search) is None
    assert fast._steal_only_size_reason({"listing": large, "deal_rating": "Great Deal"}, search) is None
    assert fast._steal_only_size_reason({"listing": medium, "deal_rating": "Great Deal"}, {"size": ["M"]}) is None


def test_letter_size_ignores_apostrophe_s():
    search = {"size": ["S", "M", "L"]}
    fits = engine.listing_matches_size_filter
    assert not fits(search, {"title": "Peter Millar Men's Gamecocks Polo", "size": "XXL"})
    assert not fits(search, {"title": "Peter Millar Men’s Gamecocks Polo", "size": "XL"})
    assert fits(search, {"title": "Peter Millar Men's Gamecocks Polo", "size": "S"})
    assert fits(search, {"title": "Peter Millar Gamecocks Polo size L"})


def test_office_wardrobe_only():
    # Owner 11-11: trousers, shoes, belts, ties off; office dress shirts only.
    def listing(title, price=10, size="L"):
        return {"title": title, "size": size, "price": {"value": price}}

    shape = fast._menswear_shape_reason
    assert shape(listing("Peter Millar chinos 34x32", 5), "peter millar")
    assert shape(listing("Peter Millar", 5, "W32"), "peter millar")
    assert shape(listing("Kiton linen shorts", 40), "kiton")
    assert shape(listing("Crockett & Jones Size 12 men's", 52, "12"), "crockett")
    assert shape(listing("Tom Ford black leather belt 110 cm", 70), "tom ford")
    assert shape(listing("Brioni silk tie", 20), "brioni")
    assert shape(listing("Canali jacket", 20), "canali", "footwear")
    assert shape(listing("Men's Vintage Brioni French Cuff Shirt Italy L", 30), "brioni")
    assert shape(listing("Vintage Brioni dress shirt", 30), "brioni")
    assert shape(listing("Canali dress shirt", 20, "16 in"), "canali")
    assert shape(listing("Canali", 20, "16 in"), "canali")
    assert shape(listing("Kiton Napoli Dress Shirt 17 / 43", 100), "kiton")
    assert shape(listing("Brioni dress shirt", 20, "16.5 in"), "brioni") is None
    assert shape(listing("Brooks Brothers suit with pants 42R", 62), "brooks brothers") is None
    assert shape(listing("Peter Millar cashmere quarter zip", 40), "peter millar") is None
    assert shape(listing("Loro Piana cashmere sweater", 80), "loro piana") is None
    assert "diffusion" in shape(listing("Ermenegildo Zegna Zegna Sport quarter zip up fleece sweater size L", 25), "zegna")
    assert "diffusion" in shape(listing("Z Zegna merino crewneck", 25), "zegna")
    assert shape(listing("Ermenegildo Zegna cashmere quarter zip", 25), "zegna") is None
    assert shape(listing("Peter Millar Crown Crafted polo", 15, "M"), "peter millar") is None
    assert "French cuffs" in fast._ai_lone_tailoring_reason(
        "Brioni white shirt with French cuffs", "other", {}, "brioni")


def test_trousers_ping_only_as_megasteals():

    for resale, blocked in ((80, True), (120, False)):
        _listing, result = _apparel_listing_result("Zegna wool trousers 34", 8, "other", "zegna")
        result.update({
            "vinted_photo_ai_checked": True, "price_confidence": "high",
            "counterfeit_suspected": False, "damage_found": False,
            "estimated_resale_value": resale, "deal_rating": "Steal",
        })
        reason = fast._vinted_apparel_bar_reason(result, "other")
        assert (reason is not None) is blocked, (resale, reason)


@pytest.mark.parametrize(
    ("summary", "listing", "brand", "blocked"),
    [
        ("Canali navy sport coat; matching pants are not included", {}, "canali", True),
        ("Canali navy two-piece suit with matching trousers", {}, "canali", False),
        ("Peter Millar polo, navy", {"title": "Peter Millar Crown Crafted", "size": "XL"}, None, True),
        ("Canali white button-down shirt", {}, "canali", True),
        ("Kiton white button-down shirt", {}, "kiton", False),
        ("Zegna white dress shirt", {"title": "Ermenegildo Zegna Shirt", "size": "L"}, "zegna", False),
        ("Zegna white dress shirt", {"title": "Z Zegna Shirt", "size": "L"}, "zegna", True),
        ("Tom Ford Black Orchid 100ml bottle", {}, "tom ford", True),
    ],
)
def test_ai_summary_shape_recheck(summary, listing, brand, blocked):
    reason = fast._ai_lone_tailoring_reason(summary, "other", listing, brand)
    assert (reason is not None) is blocked, reason


def test_scam_text_negations_and_strap_are_not_scams():
    assert not fast._VINTED_OFF_PLATFORM.search("No PayPal; payment only through Vinted")
    assert fast._VINTED_OFF_PLATFORM.search("pay me on paypal")
    assert not fast._VINTED_WATCH_DESC_EXCLUDED.search("on an aftermarket leather strap")
    assert fast._VINTED_WATCH_DESC_EXCLUDED.search("has an aftermarket dial")
    assert fast._VINTED_WATCH_GOLD_SUMMARY.search("steel Jazzmaster with diamond markers")


@pytest.mark.parametrize(
    ("title", "blocked"),
    [
        ("Poker chip keychains", True),
        ("New York, New York hotel and casino five dollar chip", True),
        ("Paulson 500 clay poker chip set", False),
    ],
)
def test_poker_merch_regex(title, blocked):
    assert bool(fast._VINTED_POKER_MERCH.search(title)) is blocked


def test_golf_title_year_before_2012_rejected_pre_ai():
    assert "2010" in engine.golf_wrong_item_title_reason(
        "2010 Nike VR Pro Combo Iron Set 3-PW", "iron set"
    )
    assert engine.golf_wrong_item_title_reason("Ping G425 Iron Set 2021", "iron set") is None


def test_paperwork_in_description_is_not_a_hard_fail():
    assert engine.condition_hard_fail_keyword(
        "longines master collection automatic",
        "longines master collection automatic comes with box and warranty card",
    ) is None
    assert engine.condition_hard_fail_keyword(
        "longines warranty card", "longines warranty card"
    ) == "warranty card"
    assert engine.condition_hard_fail_keyword(
        "seiko watch", "seiko watch for parts"
    ) == "for parts"


@pytest.mark.parametrize("title", ["tom ford sunlgasses", "Tom Ford sunglases"])
def test_eyewear_typos_blocked(title):
    assert fast._VINTED_EYEWEAR.search(title)


def test_eyewear_titles_reject_typos_included():
    for title in ("tom ford sunlgasses", "Ray-Ban Sun Glasses", "Prada sunglases",
                  "Oliver Peoples eyeglasses frames", "Persol eyewear"):
        assert engine.EYEWEAR_TITLE_SIGNALS.search(title), title
    for title in ("Peter Millar quarter zip", "Loro Piana sunless tan jacket"):
        assert not engine.EYEWEAR_TITLE_SIGNALS.search(title), title


def test_eyewear_only_in_description_rejects_before_ai():
    result = engine.score_listing(
        {"title": "Tom Ford", "description": "Tom Ford francesca sunglasses.",
         "price": {"value": 70}},
        None,
        shipping_cost=0,
        category="menswear",
    )
    assert result["verdict"] == "PASS"
    assert result["reason"] == "sunglasses/eyewear not wanted"


def test_vinted_links_open_in_browser_not_app():
    assert engine.vinted_browser_url(
        "https://www.vinted.com/items/10542231373-peter-millar-golf-shirt-l"
    ) == "https://vinted.com/items/10542231373-peter-millar-golf-shirt-l"
    assert engine.vinted_browser_url("https://www.ebay.com/itm/1") == "https://www.ebay.com/itm/1"


def test_watch_honest_description_not_rejected():
    assert not fast._VINTED_WATCH_DESC_EXCLUDED.search(
        "100% authentic, not a replica, comes with box and papers"
    )


def test_strict_seller_needs_any_one_signal():
    strict = lambda **kw: fast._seller_trust_reason(dict(TRUSTED_SELLER, **kw))
    assert strict(feedback_count=1.0, feedback_reputation=1.0,
                  item_count=1.0, given_item_count=0.0) is None  # 1 review, 5 stars
    assert strict(feedback_count=2.0, feedback_reputation=0.8,
                  item_count=10.0) is None  # few reviews but many items
    assert strict(feedback_reputation=0.86) is None  # 4.3 stars, 3+ reviews/items


def test_watches_need_a_proven_seller():
    new_seller = {"feedback_count": 0.0, "feedback_reputation": 0.0,
                  "item_count": 1.0, "given_item_count": 0.0}
    assert "watches" in fast.VINTED_STRICT_SELLER_CATEGORIES
    assert fast._seller_trust_reason(new_seller, strict=True) is not None


@pytest.mark.parametrize(
    ("updates", "fragment"),
    [
        ({"counterfeit_suspected": None}, "counterfeit"),
        ({"damage_found": True}, "damage"),
        ({"vinted_ai_summary": "Seiko Presage with two-tone gold bracelet"}, "steel"),
        ({}, None),
    ],
)
def test_vinted_watch_bar(updates, fragment):
    result = dict(
        {"vinted_photo_ai_checked": True, "counterfeit_suspected": False,
         "damage_found": False, "vinted_ai_summary": "Seiko Presage SRPB43, steel case"},
        **updates,
    )
    reason = fast._vinted_watch_bar_reason(result, "watches")
    assert (reason is None) if fragment is None else (fragment in reason)


def test_seller_stats_parse_member_page():
    page = '{\\"feedback_count\\":45,\\"feedback_reputation\\":0.98,\\"item_count\\":26,\\"given_item_count\\":69}'
    resp = mock.Mock(status_code=200, text=page)
    with mock.patch.object(fast.requests, "get", return_value=resp):
        stats = _REAL_SELLER_STATS(123)
    assert stats == {
        "_http_status": 200,
        "feedback_count": 45.0,
        "feedback_reputation": 0.98,
        "item_count": 26.0,
        "given_item_count": 69.0,
    }
    assert fast._seller_trust_reason(stats) is None


def test_member_page_closet_flags_perfume_shops():
    def box(item_id, alt):
        return (f'<img src="x.jpeg" alt="{alt}, brand: Tom Ford, condition: New with tags, '
                f'$42.54, $45.37 includes Vinted fee" class="c" '
                f'data-testid="product-item-id-{item_id}--image--img"/>')
    page = '{\\"feedback_count\\":28}' + "".join((
        box(10, "Burberry Goddess"),
        box(11, "Tom Ford Oud Wood Unisex Eau de Parfum Spray, 3.4 fl oz100ml,"),
        box(12, "Flowerbomb By Viktor &amp; Rolf 100ml"),
        box(13, "Men&#x27;s EDT"),
    ))
    resp = mock.Mock(status_code=200, text=page)
    with mock.patch.object(fast.requests, "get", return_value=resp):
        stats = _REAL_SELLER_STATS(123)
    assert stats["closet"][2] == ("12", "Flowerbomb By Viktor & Rolf 100ml")
    assert "3 other bottles" in fast._perfume_shop_reason(stats, 10)
    # A household closet with one other bottle is a real one-off seller.
    stats["closet"] = stats["closet"][:2] + [("20", "Baby clothes"), ("21", "Puzzle")]
    assert fast._perfume_shop_reason(stats, 10) is None


def test_reseller_closets_of_new_stock_are_out():
    def box(item_id, alt, brand="Ermenegildo Zegna"):
        return (f'<img src="x.jpeg" alt="{alt}, brand: {brand}, condition: New with tags, '
                f'$40.00" class="c" data-testid="product-item-id-{item_id}--image--img"/>')
    page = "".join(box(30 + i, f"Baseball Cap {c}") for i, c in enumerate(
        ("Navy", "Black", "Grey", "Olive"))) + box(99, "Hat") + box(50, "Zegna shirt")
    with mock.patch.object(fast.requests, "get", return_value=mock.Mock(status_code=200, text=page)):
        stats = _REAL_SELLER_STATS(123)
    assert stats["closet_brands"]["30"] == "Ermenegildo Zegna"
    assert "4 other new Zegna hats" in fast._closet_reseller_reason(
        stats, "vinted:30", "Zegna cap", "zegna")
    # The same caps listed as used are a real collector clearing out.
    used = dict(stats, closet_new=set())
    assert fast._closet_reseller_reason(used, "vinted:30", "Zegna cap", "zegna") is None
    # One shirt among the caps is a normal closet.
    assert fast._closet_reseller_reason(stats, "vinted:50", "Zegna dress shirt", "zegna") is None
    # Other houses' caps do not count against a Zegna listing.
    stats["closet_brands"] = {k: "Nike" for k in stats["closet_brands"]}
    stats["closet"] = [(k, "Baseball Cap") for k, _ in stats["closet"]]
    assert fast._closet_reseller_reason(stats, "vinted:30", "Zegna cap", "zegna") is None


def test_mens_apparel_with_womens_title_still_fails_gender_gate():
    listing = marketplaces.make_listing(
        "vinted",
        "mens-regression",
        "Zegna Men's Women's Cashmere Sweater",
        50,
        "https://www.vinted.com/items/mens-regression",
    )
    result = engine.score_listing(listing, None, category="knitwear")
    assert result["verdict"] == "PASS"
    assert result["reason"] == "excluded gender keyword in title/description"


def test_phantom_lane_ledger_is_repaired_once_to_repo_effective(tmp_path, monkeypatch):
    repo_db, lane_db = tmp_path / "ai_spend.db", tmp_path / "lane.db"
    monkeypatch.setattr(engine, "AI_SPEND_DB_PATH", str(repo_db))
    month = fast.datetime.now(fast.timezone.utc).strftime("%Y-%m")
    for path, gross, credits in ((repo_db, 4.5, 3.3), (lane_db, 13.3, 3.3)):
        with closing(sqlite3.connect(path)) as conn:
            engine._ensure_paid_ai_schema(conn)
            conn.execute(
                "INSERT INTO ai_paid_accounting VALUES (?, ?, ?, 0, 0, 0, 0)",
                (month, gross, credits),
            )
            conn.commit()
    fast._repair_phantom_lane_ledger(lane_db)
    monkeypatch.setattr(engine, "DB_PATH", str(lane_db))
    effective, _calls = engine._reconcile_paid_ai_ledgers(month)
    assert effective == pytest.approx(1.2)
    # A second start must not reset spend the lane has since reserved.
    with closing(sqlite3.connect(lane_db)) as conn:
        conn.execute("UPDATE ai_paid_accounting SET gross_reserved_usd=gross_reserved_usd+1")
        conn.commit()
    fast._repair_phantom_lane_ledger(lane_db)
    assert engine._reconcile_paid_ai_ledgers(month)[0] == pytest.approx(2.2)


def test_page_details_parse_seller_feedback_count():
    page = '<meta property="og:description" content="x">{\\"feedback_count\\":7}'
    resp = mock.Mock(status_code=200, text=page)
    with mock.patch.object(engine.requests, "get", return_value=resp):
        assert engine.fetch_vinted_item_page_details("u")["seller_feedback_count"] == 7


def test_instant_alerted_item_that_fails_gets_quiet_skip_followup(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    conn = engine.init_db(lane.seen_db_path)
    listing, result = _apparel_listing_result(
        "Zegna Cashmere sweater", 10, "knitwear", "zegna", search_id="s"
    )
    result["vinted_instant_alerted"] = True
    with mock.patch.object(fast.requests, "post") as post:
        lane._finish(conn, result, listing["itemId"], "fp", 10, "AI resale $15 below floor")
    headers = post.call_args.kwargs["headers"]
    assert headers["Title"].startswith("SKIP: ") and headers["Priority"] == "2"
    assert b"AI resale $15 below floor" in post.call_args.kwargs["data"]
    conn.close()


def test_vinted_strong_label_naming_other_brand_vetoes(tmp_path):
    listing, result = _apparel_listing_result(
        "Loro Piana wool suit 42R", 30, "tailoring", '"loro piana"',
        search_id="vinted-loro-piana",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(200),
        strong_result=_passing_strong_result(
            200, label_shows_different_brand=True,
            brand_label_reason="Jacket label reads Jos. A. Bank",
        ),
    )
    send.assert_not_called()
    assert "garment label names a different brand: Jacket label reads Jos. A. Bank" in row["reason"]


def test_vinted_strong_label_not_visible_still_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Cashmere Quarter Zip",
        7,
        "knitwear",
        "peter millar crown crafted quarter zip",
        search_id="vinted-pm-no-label",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(120),
        strong_result=_passing_strong_result(
            120,
            brand_label_visible_and_consistent=False,
            brand_label_reason="No readable permanent label photo",
        ),
    )
    # Counterfeit signs still veto; a missing tag photo alone no longer does.
    send.assert_called_once()


def test_luxury_house_without_readable_label_is_vetoed(tmp_path):
    listing, result = _apparel_listing_result(
        "Brioni Men dress shirts",
        20,
        "other",
        "brioni",
        search_id="vinted-brioni-no-label",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(300),
        strong_result=_passing_strong_result(
            300,
            brand_label_visible_and_consistent=False,
            brand_label_reason="No readable permanent label photo",
        ),
    )
    send.assert_not_called()
    assert "luxury house needs a readable genuine label" in row["reason"]


def test_vinted_strong_unavailable_fails_closed_and_logs_code(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Cashmere Quarter Zip",
        18,
        "knitwear",
        "peter millar crown crafted quarter zip",
        search_id="vinted-pm-unavailable",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(120),
        strong_unavailable="HTTP 503 after primary/fallback",
    )
    send.assert_not_called()
    assert row["verdict"] == "STRONG_LOOK_UNAVAILABLE"
    assert "STRONG_LOOK_UNAVAILABLE" in row["reason"]


def test_loro_piana_passing_strong_look_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Loro Piana Mens Cashmere Sweater Size 52",
        40,
        "other",
        '"loro piana" -women',
        search_id="vinted-loro-piana-pass",
    )
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(400, visible_brand_evidence="Loro Piana permanent label"),
        strong_result=_passing_strong_result(360, size_on_tag="52"),
    )
    strong.assert_called_once()
    send.assert_called_once()
    assert row["delivered"] is True
    assert row["vinted_min_resale_floor"] == 150


def test_vinted_strong_counterfeit_indicators_veto(tmp_path):
    listing, result = _apparel_listing_result(
        "Loro Piana Mens Cashmere Sweater Size 52",
        40,
        "other",
        '"loro piana" -women',
        search_id="vinted-loro-piana-fake",
    )
    _lane_obj, _strong, send, row = _run_custom_pipeline(
        tmp_path,
        listing,
        result,
        _apparel_ai(400),
        strong_result=_passing_strong_result(
            400,
            counterfeit_indicators=True,
            counterfeit_reason="Label font and stitching are inconsistent",
        ),
    )
    send.assert_not_called()
    assert "counterfeit indicators" in row["reason"]


def test_vinted_knitwear_replaces_only_grab_on_sight_tier_rule():
    base = {
        "listing": {"title": "Peter Millar Merino Quarter Zip"},
        "vinted_fast_lane": True,
        "search_query": "peter millar quarter zip",
        "brand_tier": "standard",
        "peter_millar_back_crown_visible": True,
        "deal_rating": "Steal",
        "discount_pct": 80,
        "price_confidence": "high",
        "counterfeit_suspected": False,
    }
    assert engine.is_blocked_by_steal_quality_gate(base, category="knitwear") is None
    main_poll = dict(base)
    main_poll.pop("vinted_fast_lane")
    assert "brand not grab_on_sight-tier" in engine.is_blocked_by_steal_quality_gate(
        main_poll, category="knitwear"
    )
    # A Great Deal sweater passes only at <= half the brand floor ($12 vs $30).
    great = {**base, "deal_rating": "Great Deal", "vinted_min_resale_floor": 30}
    assert engine.is_blocked_by_steal_quality_gate(
        {**great, "item_price": 12}, category="knitwear"
    ) is None
    assert "below Steal" in engine.is_blocked_by_steal_quality_gate(
        {**great, "item_price": 26}, category="knitwear"
    )
    assert "below Steal" in engine.is_blocked_by_steal_quality_gate(
        {**great, "item_price": 12, "deal_rating": "Good Deal"}, category="knitwear"
    )


def test_poker_chips_pipeline_is_unchanged_and_skips_strong_look(tmp_path):
    listing, result = _apparel_listing_result(
        "Paulson Casino Poker Chips Complete 300 Chip Set",
        20,
        "poker-chips",
        "paulson poker chips",
        search_id="vinted-poker-unaffected",
    )
    poker_ai = {
        "chip_type": "compression-molded clay",
        "is_genuine_clay": True,
        "is_genuine_ceramic": False,
        "has_inlay_not_sticker": True,
        "has_hot_stamp_not_sticker": False,
        "ceramic_design_embedded_not_sticker": False,
        "edge_spots_consistent": True,
        "recolor_suspected": False,
        "recolor_reason": "",
        "identified_casino_or_maker": "Paulson",
        "estimated_chip_count": 300,
        "chip_count_visually_supported": True,
        "is_complete_usable_set": True,
        "set_completeness_reason": "Six full racks are visible",
        "summary": "Genuine coherent Paulson set",
    }
    _lane_obj, strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, poker_ai
    )
    strong.assert_not_called()
    send.assert_called_once()
    assert row["delivered"] is True


def test_strong_look_counts_toward_shared_and_separate_cost_caps(tmp_path):
    lane = _lane(tmp_path)
    listing, result = _apparel_listing_result(
        "Loro Piana Cashmere Sweater",
        40,
        "other",
        '"loro piana"',
        search_id="vinted-cost-cap",
    )
    result["visible_brand_evidence"] = "Loro Piana label"
    lane.ai_calls = 1
    with (
        mock.patch.object(fast, "VINTED_FAST_MAX_AI_CALLS_PER_RUN", 1),
        mock.patch.object(engine, "_call_gemini_json") as call,
    ):
        assert lane._run_vinted_strong_vision(result, None) is None
    call.assert_not_called()
    assert "shared per-run AI cap" in result["vinted_strong_unavailable_reason"]

    lane.ai_calls = 1
    with (
        mock.patch.object(fast, "VINTED_FAST_MAX_AI_CALLS_PER_RUN", 2),
        mock.patch.object(fast, "VINTED_STRONG_LOOK_MAX_PER_RUN", 10),
        mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
        mock.patch.object(
            engine,
            "_download_listing_image",
            return_value=(b"jpeg", "image/jpeg"),
        ),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True) as reserve,
        mock.patch.object(
            engine, "_call_gemini_json", return_value=_passing_strong_result(400)
        ) as call,
    ):
        assert lane._run_vinted_strong_vision(result, None)
    assert lane.ai_calls == 2
    assert lane.strong_look_calls == 1
    reserve.assert_called_once_with(
        engine.AI_PAID_VISION_RESERVATION_USD, kind="vision"
    )
    call.assert_called_once()

    capped = _lane(tmp_path)
    with (
        mock.patch.object(fast, "VINTED_STRONG_LOOK_MAX_PER_RUN", 0),
        mock.patch.object(engine, "_call_gemini_json") as call,
    ):
        assert capped._run_vinted_strong_vision(result, None) is None
    call.assert_not_called()
    assert "strong-look cap" in result["vinted_strong_unavailable_reason"]


def test_strong_look_uses_paid_backup_first_and_skips_free_gemini(tmp_path, monkeypatch):
    # Owner 11-10: free Gemini failed 3/3 looks and cost the $5 PM shorts ~10 s.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    lane = _lane(tmp_path)
    _listing, result = _apparel_listing_result(
        "Loro Piana Cashmere Sweater", 40, "other", '"loro piana"', search_id="vinted-fallback",
    )
    with (
        mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
        mock.patch.object(
            engine, "_download_listing_image", return_value=(b"jpeg", "image/jpeg")
        ),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True) as reserve,
        mock.patch.object(engine, "_call_gemini_json") as gemini,
        mock.patch.object(fast.requests, "post", return_value=_openrouter_reply()),
    ):
        assert lane._run_vinted_strong_vision(result, None)
    gemini.assert_not_called()
    assert reserve.call_count == 1
    assert result["vinted_strong_model"] == f"openrouter:{fast.VINTED_STRONG_OPENROUTER_MODEL}"


def test_strong_look_falls_back_on_timeout_or_bad_json_and_unwraps_list(tmp_path, monkeypatch):
    # Paid backup down (timeout or junk reply) -> free Gemini, which may wrap
    # its object in a list.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    junk = mock.Mock()
    junk.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
    for paid in (requests.exceptions.ReadTimeout("slow"), junk):
        for reply in (_passing_strong_result(400), [_passing_strong_result(400)]):
            lane = _lane(tmp_path)
            _listing, result = _apparel_listing_result(
                "Loro Piana Cashmere Sweater", 40, "other", '"loro piana"',
                search_id="vinted-fallback",
            )
            post = {"side_effect": paid} if isinstance(paid, Exception) else {"return_value": paid}
            with (
                mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
                mock.patch.object(
                    engine, "_download_listing_image", return_value=(b"jpeg", "image/jpeg")
                ),
                mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
                mock.patch.object(engine, "_call_gemini_json", side_effect=[reply]),
                mock.patch.object(fast.requests, "post", **post),
            ):
                assert lane._run_vinted_strong_vision(result, None)
            assert result["vinted_strong_model"] == fast.VINTED_STRONG_VISION_MODEL


def test_vinted_apparel_alert_starts_with_required_strict_summary(monkeypatch):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Cashmere Quarter Zip",
        18,
        "knitwear",
        "peter millar crown crafted quarter zip",
        search_id="vinted-alert-text",
    )
    result.update(
        {
            "vinted_strict_apparel": True,
            "vinted_brand": "Peter Millar",
            "vinted_alert_item": "Crown Crafted Cashmere Quarter Zip",
            "vinted_conservative_resale_value": 110,
            "vinted_conservative_discount_pct": 88,
            "vinted_strong_condition_grade": "excellent",
            "vinted_strong_size_on_tag": "L",
            "vinted_strong_premium_reason": "Crown Crafted cashmere tag is visible",
            "deal_rating": "Steal",
            "discount_pct": 88,
            "estimated_resale_value": 110,
        }
    )
    fast._set_vinted_negotiation_prices(result)
    response = mock.Mock(status_code=200)
    response.raise_for_status.return_value = None
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    with mock.patch.object(engine.requests, "post", return_value=response) as post:
        engine.send_alert(result)
    message = post.call_args.kwargs["data"].decode("utf-8")
    lines = message.splitlines()
    assert lines[0] == (
        "STEAL: Peter Millar Crown Crafted Cashmere Quarter Zip - "
        "~$19.08 all-in vs ~$110 resale (88% off)"
    )
    assert lines[1] == "Buy now at list, ~$19.08 all-in. An offer risks losing it."
    assert "condition: excellent | size on tag: L" in message
    assert "premium: Crown Crafted cashmere tag is visible" in message


def test_web_catalog_items_parse_route_and_brand_title(tmp_path):
    product = {
        "id": 10462300934, "title": "Cashmere crewneck sweater", "url": "/items/10462300934-x",
        "price": {"amount": "10.00", "currencyCode": "USD"},
        "totalItemPrice": {"amount": "11.20", "currencyCode": "USD"},
        "photos": [{"url": "https://images1.vinted.net/a.webp"}],
        "itemBox": {"firstLine": "Ermenegildo Zegna", "secondLine": "L · Very good"},
    }
    payload = json.dumps({"items": {"items": [{"id": 10462300934, "productItem": product}]}}, separators=(",", ":"))
    html = '<script>self.__next_f.push([1,' + json.dumps(payload) + '])</script>'
    items = fast.parse_web_catalog_items(html)
    assert [item["id"] for item in items] == [10462300934]
    assert items[0]["title"] == "Ermenegildo Zegna Cashmere crewneck sweater"
    assert fast.parse_web_catalog_items("<html></html>") is None
    searches = [
        {"id": "z", "query": 'zegna -"z zegna" -women', "max_price": 75, "platforms": ["vinted"]},
        {"id": "pm", "query": "peter millar", "max_price": 30, "platforms": ["vinted"]},
    ]
    lane = fast.VintedFastLane(
        searches=searches, state_path=tmp_path / "s", seen_db_path=tmp_path / "d",
        log_path=tmp_path / "l", schedule=fast.build_feed_schedule([1], [], 130),
    )
    assert lane._route(items[0])["id"] == "z"
    pricey = dict(items[0], price={"amount": "90.00"})
    assert lane._route(pricey) is None


def test_feed_schedule_gives_brand_feed_k_slots_per_text_feed():
    feeds = [{"query": "poker chip", "max_price": 150}, {"query": "seiko"}]
    schedule = fast.build_feed_schedule([1, 2], feeds, 130, brand_slots=1)
    assert [kind for kind, _ in schedule] == ["brands", "text", "brands", "text"]
    schedule = fast.build_feed_schedule([1, 2], feeds, 130, brand_slots=2)
    assert [kind for kind, _ in schedule] == ["brands", "brands", "text"] * 2
    # The web catalog page 429s above ~30 requests/min (production 11-03).
    assert fast.VINTED_BRAND_SLOTS_PER_TEXT == 1
    assert schedule[0][1]["brand_ids"] == [1, 2]
    assert fast.build_feed_schedule([], [{"query": "poker chip"}], None)[0][0] == "text"


def test_elite_feeds_run_twice_per_cycle_at_the_same_request_rate():
    # Owner 11-08, tops first: elite text feeds come round twice as often.
    feeds = [{"query": q} for q in ("a", "b", "c", "d")] + [{"query": "lp", "slots": 2}]
    texts = [s["query"] for kind, s in fast.build_feed_schedule([1], feeds, 250, brand_slots=1) if kind == "text"]
    assert texts == ["lp", "a", "b", "lp", "c", "d"]
    # Owner 11-08 "even quicker": weight 4, each elite feed every ~22 s.
    live = {f["query"]: f.get("slots", 1) for f in fast.VINTED_TEXT_FEEDS}
    assert all(live[q] == 4 for q in ("zegna", "loro piana", "cucinelli", "kiton", "brioni"))


def test_unrouted_elite_rows_are_logged(caplog):
    def item(title):
        return {"id": 9, "title": title, "url": "/items/9", "price": {"amount": "220"},
                "photo": {"url": "https://x/a.webp"}, "photos": [], "item_box": {}}

    with caplog.at_level("INFO"):
        fast._log_unrouted(item("Loro Piana cashmere quarter zip L"), None)
        fast._log_unrouted(item("Peter Millar quarter zip"), "peter millar")
    lines = [r.getMessage() for r in caplog.records if "unrouted" in r.getMessage()]
    assert len(lines) == 1 and "Loro Piana cashmere quarter zip L" in lines[0] and "$220" in lines[0]


def test_route_prefers_best_title_match_and_never_crosses_feeds(tmp_path):
    searches = [
        {"id": "pm", "query": "peter millar -women", "max_price": 30, "platforms": ["vinted"]},
        {"id": "pm-polo", "query": "peter millar polo -women", "max_price": 30, "platforms": ["vinted"]},
        {"id": "pm-qz", "query": "peter millar quarter zip -women", "max_price": 45, "platforms": ["vinted"]},
        {"id": "poker", "query": "casino poker chips set", "max_price": 150, "platforms": ["vinted"]},
        {"id": "bb", "query": "brooks brothers -women", "max_price": 30, "platforms": ["vinted"]},
    ]
    lane = fast.VintedFastLane(
        searches=searches, state_path=tmp_path / "s", seen_db_path=tmp_path / "d",
        log_path=tmp_path / "l", schedule=[],
    )

    def item(title, brand, price):
        return {"id": 1, "title": title, "url": "/items/1", "price": {"amount": price},
                "photo": {"url": "https://x/a.webp"}, "photos": [], "item_box": {"first_line": brand}}

    assert lane._route(item("Peter Millar Polo", "Peter Millar", "25.00"))["id"] == "pm-polo"
    assert lane._route(item("Peter Millar quarter zip pullover", "Peter Millar", "40.00"))["id"] == "pm-qz"
    assert lane._route(item("Brooks Brothers seersucker set", "Brooks Brothers", "21.00"))["id"] == "bb"
    assert lane._route(item("Casino chips lot", "unknown", "40.00"), "poker chip")["id"] == "poker"
    # Over the polo cap: must not fall through to a pricier garment search.
    assert lane._route(item("Peter Millar Polo", "Peter Millar", "40.00")) is None


@pytest.mark.parametrize(
    ("title", "brand", "price", "expected"),
    [
        ("Ermenegildo Zegna cashmere crewneck", "Ermenegildo Zegna", "110.00", "vinted-zegna-cashmere-sweater"),
        ("Zegna 1/4 zip knit", "Zegna", "90.00", "vinted-zegna-quarter-zip"),
        ("Kiton half zip cashmere", "Kiton", "140.00", "vinted-kiton-quarter-zip"),
        ("Zegna polo shirt", "Zegna", "60.00", "vinted-zegna-polo"),
        ("Z Zegna polo shirt", "Zegna", "60.00", None),
    ],
)
def test_luxury_brand_feed_routes_to_specific_searches(tmp_path, title, brand, price, expected):
    lane = fast.VintedFastLane(
        searches=[s for s in engine.SAVED_SEARCHES if "vinted" in (s.get("platforms") or [])],
        state_path=tmp_path / "s", seen_db_path=tmp_path / "d",
        log_path=tmp_path / "l", schedule=[],
    )
    item = {"id": 1, "title": title, "url": "/items/1", "price": {"amount": price},
            "photo": {"url": "https://x/a.webp"}, "photos": [], "item_box": {"first_line": brand}}
    routed = lane._route(item)
    assert (routed or {}).get("id") == expected


def test_shoe_search_accepts_us_12_to_13_and_uk_eu_equivalents():
    search = next(s for s in engine.SAVED_SEARCHES if s["id"] == "vinted-loro-piana-shoes")
    ok = lambda size: engine.listing_matches_size_filter(search, {"title": "Loro Piana loafers", "size": size, "platform": "vinted"})
    assert ok("12") and ok("12.5") and ok("13") and ok("UK 11") and ok("EU 46")
    assert not ok("11") and not ok("10.5") and not ok("14")


def test_designer_sneakers_bare_eu_size_and_margiela_replica_line(tmp_path):
    search = next(s for s in engine.SAVED_SEARCHES if s["id"] == "vinted-margiela-shoes")
    ok = lambda title, size=None, desc="": engine.listing_matches_size_filter(
        search, {"title": title, "size": size, "description": desc, "platform": "vinted"})
    assert ok("Maison Margiela Replica GAT 45") and ok("Margiela GAT", "46")
    assert not ok("Margiela GAT 43") and not ok("Margiela GAT", desc="$46 shipped")
    assert engine._text_safety_hard_fails("maison margiela replica gat sneakers") is None
    assert engine._text_safety_hard_fails("margiela gat replica 1:1") is not None
    assert engine._text_safety_hard_fails("nike dunk replica") is not None
    lane = fast.VintedFastLane(
        searches=[s for s in engine.SAVED_SEARCHES if "vinted" in (s.get("platforms") or [])],
        state_path=tmp_path / "s", seen_db_path=tmp_path / "d",
        log_path=tmp_path / "l", schedule=[],
    )
    item = {"id": 1, "title": "Maison Margiela Replica GAT sneakers", "url": "/items/1",
            "price": {"amount": "90.00"}, "photo": {"url": "https://x/a.webp"}, "photos": [],
            "item_box": {"first_line": "Maison Margiela"}}
    assert lane._route(item, "margiela replica") is None  # owner 11-08 dropped shoe and poker searches


@pytest.mark.parametrize(
    ("search_id", "source_text", "title", "brand"),
    [
        (
            "vinted-pdm-delina-exclusif",
            "delina exclusif",
            "Parfums de Marly Delina Exclusif 75ml",
            "Parfums de Marly",
        ),
        (
            "vinted-pdm-oriana",
            "oriana marly",
            "Parfums de Marly Oriana",
            "Parfums de Marly",
        ),
        (
            "vinted-kilian-angels-share",
            "angels share kilian",
            "Kilian Angels' Share EDP 50ml",
            "Kilian",
        ),
        (
            "vinted-kilian-angels-share",
            "angels share kilian",
            "By Kilian Angel's Share 100ml",
            "Kilian",
        ),
        (
            "vinted-burberry-goddess",
            "burberry goddess",
            "Goddess Eau de Parfum Intense 100ml sealed",
            "Burberry",
        ),
    ],
)
def test_fragrance_text_feeds_route_brand_and_apostrophe_variants(
    tmp_path, search_id, source_text, title, brand
):
    search = _fragrance_search(search_id)
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "s",
        seen_db_path=tmp_path / "d",
        log_path=tmp_path / "l",
        schedule=[],
    )
    item = _fragrance_item("route", title, search["max_price"], brand=brand)
    assert lane._route(item, source_text)["id"] == search_id


def test_fragrance_route_requires_the_exact_scent_words(tmp_path):
    search = _fragrance_search("vinted-kilian-angels-share")
    lane = fast.VintedFastLane(
        searches=[search],
        state_path=tmp_path / "s",
        seen_db_path=tmp_path / "d",
        log_path=tmp_path / "l",
        schedule=[],
    )
    item = _fragrance_item(
        "other-scent", "Sealed Kilian Good Girl Gone Bad EDP 100ml", 180, brand="Kilian"
    )
    assert lane._route(item, "angels share kilian") is None


def test_off_profile_rejects_dresses_and_other_schools_only():
    off = fast._off_profile_reason
    assert "dress" in off("Loro Piana cashmere t shirt dress")
    assert "Georgia" in off("Peter Millar skort with Georgia emblem on back")
    assert "UGA" in off("Peter Millar UGA full zip jacket")
    assert "clemson" in off("polo with clemson logo", schools_only=True)
    for fine in (
        "Brooks Brothers dress shirt",
        "Brooks Brothers navy polo",
        "Peter Millar Gamecocks polo",
        "Peter Millar University of South Carolina polo",
        "Zegna Cashmere sweater",
    ):
        assert off(fine) is None, fine
    assert off("Loro Piana t shirt dress", schools_only=True) is None
    assert engine._text_safety_hard_fails("peter millar golf skort") is not None
