import io
import json
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


def test_hardcoded_four_second_interval_is_respected(tmp_path):
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
    assert fast.VINTED_FAST_POLL_SECONDS == 4


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
    assert "below $45 brand floor" in row["reason"]


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


def test_vinted_strong_condition_good_vetoes(tmp_path):
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
            120, condition_grade="good", flaws_found=["light pilling"]
        ),
    )
    send.assert_not_called()
    assert "condition grade good not nwt/excellent" in row["reason"]


def test_vinted_strong_label_not_visible_vetoes(tmp_path):
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
    send.assert_not_called()
    assert "brand label/tag not visibly consistent" in row["reason"]


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
            "vinted_conservative_discount_pct": 83,
            "vinted_strong_condition_grade": "excellent",
            "vinted_strong_size_on_tag": "L",
            "vinted_strong_premium_reason": "Crown Crafted cashmere tag is visible",
            "deal_rating": "Steal",
            "discount_pct": 83,
            "estimated_resale_value": 110,
        }
    )
    response = mock.Mock(status_code=200)
    response.raise_for_status.return_value = None
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    with mock.patch.object(engine.requests, "post", return_value=response) as post:
        engine.send_alert(result)
    message = post.call_args.kwargs["data"].decode("utf-8")
    assert message.startswith(
        "STEAL: Peter Millar Crown Crafted Cashmere Quarter Zip - "
        "$19.08 vs ~$110 resale (83% off)"
    )
    assert "condition: excellent | size on tag: L" in message
    assert "premium: Crown Crafted cashmere tag is visible" in message
