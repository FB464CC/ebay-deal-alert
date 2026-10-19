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


def _run_one_pipeline(tmp_path, *, jev_score=0.9, ai_result=None, cap=40):
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
    ):
        lane._process_item(conn, None, {}, {"query": result["search_query"]}, None)
    seen = not engine.is_new(conn, listing["itemId"])
    conn.close()
    return lane, photo, send, seen


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
    _lane_obj, photo, send, seen = _run_one_pipeline(tmp_path, jev_score=0.05)
    photo.assert_not_called()
    send.assert_not_called()
    assert seen


def test_ai_pass_sends_alert(tmp_path):
    _lane_obj, photo, send, seen = _run_one_pipeline(tmp_path)
    photo.assert_called_once()
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
    lane, photo, send, seen = _run_one_pipeline(tmp_path, cap=0)
    photo.assert_not_called()
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
