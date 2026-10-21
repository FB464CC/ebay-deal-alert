import base64
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image
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


def test_only_items_newer_than_high_water_enter_pipeline():
    baseline, water = fast.items_newer_than_high_water(
        [_raw_item(3, 300), _raw_item(2, 200)], None
    )
    assert baseline == []
    assert water["item_id"] == "3"
    newer, next_water = fast.items_newer_than_high_water(
        [_raw_item(4, 400), _raw_item(3, 300), _raw_item(2, 200)], water
    )
    assert [item["id"] for item in newer] == ["4"]
    assert next_water["item_id"] == "4"


def test_vinted_negotiation_price_math_uses_item_and_landed_list_prices():
    result = {"item_price": 100.0, "price": 120.0}
    assert fast._set_vinted_negotiation_prices(result) == 84.0
    assert result["vinted_opening_offer"] == 60.0
    assert result["vinted_expected_paid"] == 84.0
    assert result["vinted_opening_offer_fraction"] == 0.60
    assert result["vinted_expected_paid_fraction"] == 0.70


def test_vinted_brand_resale_floors_match_owner_profile():
    expected = {
        "peter millar": 30,
        "canali": 150,
        "brooks brothers": 35,
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
    assert canali["size"] == ["42"]
    assert canali["size_equivalents"] == ["52"]
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
        canali, {"title": "Canali Navy Wool Suit 40R", "size": "EU 50"}
    )
    assert searches["vinted-brooks-brothers"]["size"] == ["M", "L", "XL"]
    assert searches["vinted-e-marinella-tie"]["size"] is None
    assert searches["vinted-peter-millar-crown-crafted"]["size"] == ["L", "XL"]
    assert searches["vinted-peter-millar-polo"]["size"] == ["M", "L", "XL"]
    assert len(fast.enabled_vinted_searches()) == 39


def test_vinted_menswear_caps_are_negotiation_adjusted_list_price_caps():
    expected_caps = {
        "vinted-peter-millar-polo": 30,
        "vinted-peter-millar-quarter-zip": 45,
        "vinted-peter-millar-jacket": 65,
        "vinted-peter-millar": 30,
        "vinted-peter-millar-crown-crafted": 50,
        "vinted-canali-suit": 130,
        "vinted-canali": 90,
        "vinted-brooks-brothers": 30,
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
    assert round(row["vinted_expected_paid"], 2) == 7.42
    assert row["deal_rating"] == "Steal"


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
    assert round(row["vinted_expected_paid"], 2) == 14.84
    assert row["deal_rating"] == "Great Deal"
    assert "exceeds 40% of conservative" in row["reason"]


def test_peter_millar_crown_crafted_cashmere_quarter_zip_alerts(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar Crown Crafted Cashmere Quarter Zip Size M",
        18,
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
    assert row["vinted_conservative_resale_value"] == 110


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
        ("Peter Millar Crown Crafted quarter zip", 5, "knitwear", False),  # floor < $100
        ("Loro Piana cashmere crewneck", 30, "knitwear", True),
        ("Canali Camisa de manga larga", 8, "other", False),  # mis-tagged shirt
    ]
    for title, price, category, expected in cases:
        _listing, result = _apparel_listing_result(
            title, price, category, title.split()[0].lower(), search_id="s"
        )
        with mock.patch.object(fast.requests, "post") as post:
            assert lane._maybe_instant_alert(result, category) is expected, title
        assert post.called is expected, title
        if expected:
            headers = post.call_args.kwargs["headers"]
            assert headers["Priority"] == "5"
            assert headers["Title"].startswith("FAST ")


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


def test_identical_title_and_price_batch_is_rejected_before_ai(tmp_path):
    lane = _lane(tmp_path)
    conn = engine.init_db(lane.seen_db_path)
    first = _listing_and_result()
    second = _listing_and_result()
    second[0]["itemId"] = "v|duplicate"
    second[1]["listing"] = second[0]
    second[0]["price"] = {"value": float(first[0]["price"]["value"]) + 1, "currency": "USD"}
    lane._cheap_result = mock.Mock(side_effect=[(*first, None), (*second, None)])
    with (
        mock.patch.object(engine, "check_photos_with_gemini", return_value=None) as photo,
        mock.patch.object(lane, "_run_vinted_prescreen", return_value=None),
    ):
        lane._process_item(conn, None, {}, {"query": first[1]["search_query"]}, None)
        lane._process_item(conn, None, {}, {"query": first[1]["search_query"]}, None)
    assert photo.call_count == 1
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
    assert "necktie cannot clear the $120 brand resale floor" in row["reason"]


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
        "Loro Piana fabric wool blazer", 30, "tailoring", '"loro piana"',
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
        18,
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


def test_strong_look_uses_golf_fallback_on_503_and_ledgers_both_attempts(tmp_path):
    lane = _lane(tmp_path)
    _listing, result = _apparel_listing_result(
        "Loro Piana Cashmere Sweater",
        40,
        "other",
        '"loro piana"',
        search_id="vinted-fallback",
    )
    response = mock.Mock(status_code=503)
    primary_error = requests.exceptions.HTTPError("busy", response=response)
    with (
        mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
        mock.patch.object(
            engine,
            "_download_listing_image",
            return_value=(b"jpeg", "image/jpeg"),
        ),
        mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True) as reserve,
        mock.patch.object(
            engine,
            "_call_gemini_json",
            side_effect=[primary_error, _passing_strong_result(400)],
        ) as call,
    ):
        assert lane._run_vinted_strong_vision(result, None)
    assert [entry.kwargs["model"] for entry in call.call_args_list] == [
        fast.VINTED_STRONG_VISION_MODEL,
        engine.GOLF_STRONG_VISION_FALLBACK_MODEL,
    ]
    assert reserve.call_count == 2
    assert result["vinted_strong_model"] == engine.GOLF_STRONG_VISION_FALLBACK_MODEL


def test_strong_look_falls_back_on_timeout_or_bad_json_and_unwraps_list(tmp_path):
    for primary in (requests.exceptions.ReadTimeout("slow"), None, "not json"):
        lane = _lane(tmp_path)
        _listing, result = _apparel_listing_result(
            "Loro Piana Cashmere Sweater", 40, "other", '"loro piana"',
            search_id="vinted-fallback",
        )
        with (
            mock.patch.object(engine, "_configured_gemini_api_keys", return_value=["key"]),
            mock.patch.object(
                engine, "_download_listing_image", return_value=(b"jpeg", "image/jpeg")
            ),
            mock.patch.object(engine, "_reserve_paid_ai_spend", return_value=True),
            mock.patch.object(
                engine,
                "_call_gemini_json",
                side_effect=[primary, [_passing_strong_result(400)]],
            ),
        ):
            assert lane._run_vinted_strong_vision(result, None)
        assert result["vinted_strong_model"] == engine.GOLF_STRONG_VISION_FALLBACK_MODEL


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
        "~$13.36 expected vs ~$110 resale (88% off)"
    )
    assert lines[1] == (
        "List $18 -> open $10.80 (60%) -> expect ~$13.36 "
        "(owner median 70%)"
    )
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


def test_feed_schedule_puts_brand_feed_on_every_other_request():
    schedule = fast.build_feed_schedule(
        [1, 2], [{"query": "poker chip", "max_price": 150}, {"query": "marinella"}], 130
    )
    assert [kind for kind, _ in schedule] == ["brands", "text", "brands", "text"]
    assert schedule[0][1]["brand_ids"] == [1, 2]
    assert fast.build_feed_schedule([], [{"query": "poker chip"}], None)[0][0] == "text"


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
