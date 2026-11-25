"""Owner 11-24: five sneaker models back on, real steals only; an offer that
makes one a steal still pings."""
import json
import os
from unittest import mock

import ebay_deal_alert as engine
import test_vinted_fast as t
import vinted_fast as fast

SEARCHES = {s["id"]: s for s in engine.SAVED_SEARCHES if s.get("steal_only")}
MARGIELA = "vinted-margiela-shoes"


def test_six_sneaker_searches_are_on_and_steal_only():
    assert set(SEARCHES) == {
        MARGIELA, "vinted-common-projects-shoes", "vinted-zegna-triple-stitch",
        "vinted-loro-piana-summer-walk", "vinted-loro-piana-open-walk",
        "vinted-brunello-cucinelli-sneakers",
    }
    assert all(s["enabled"] and s["category"] == "footwear" and s["size"] == ["12", "12.5", "13"]
               for s in SEARCHES.values())
    # Every other shoe search stays off (owner 11-11).
    assert not [s["id"] for s in fast.enabled_vinted_searches()
                if s.get("category") == "footwear" and not s.get("steal_only")]


def test_shoes_gate_opens_only_for_the_named_searches():
    shape = fast._menswear_shape_reason
    gat = {"title": "Maison Margiela Replica GAT sneakers", "size": "12"}
    assert "off" in shape(gat, "margiela", "footwear")
    assert shape(gat, "margiela", "footwear",
                 wanted_shoes=fast._wanted_shoes(SEARCHES[MARGIELA], "footwear")) is None
    assert not fast._wanted_shoes({"category": "footwear"}, "footwear")
    assert not fast._wanted_shoes(SEARCHES[MARGIELA], "other")


def test_feed_rows_route_to_the_named_models_only(tmp_path):
    lane = fast.VintedFastLane(
        searches=fast.enabled_vinted_searches(), state_path=tmp_path / "s",
        seen_db_path=tmp_path / "d", log_path=tmp_path / "l", schedule=[],
    )

    def route(title, feed, brand):
        item = {"id": 1, "title": title, "url": "/items/1", "price": {"amount": "90.00"},
                "photo": {"url": "https://x/a.webp"}, "photos": [],
                "item_box": {"first_line": brand}}
        return (lane._route(item, feed) or {}).get("id")

    assert route("Zegna Triple Stitch sneakers white", "zegna", "Zegna") == "vinted-zegna-triple-stitch"
    assert route("Loro Piana Summer Walk loafers suede", "loro piana", "Loro Piana") == "vinted-loro-piana-summer-walk"
    assert route("Loro Piana Open Walk boots", "loro piana", "Loro Piana") == "vinted-loro-piana-open-walk"
    assert route("Brunello Cucinelli suede sneakers", "cucinelli", "Brunello Cucinelli") == "vinted-brunello-cucinelli-sneakers"
    assert route("Common Projects Achilles Low white", "common projects", "Common Projects") == "vinted-common-projects-shoes"
    assert route("Maison Margiela Replica GAT sneakers", "margiela replica", "Maison Margiela") == MARGIELA
    # Other shoes from the same houses never reach a sneaker search.
    for title, feed, brand in [
        ("Zegna leather loafers", "zegna", "Zegna"),
        ("Brunello Cucinelli penny loafers", "cucinelli", "Brunello Cucinelli"),
        ("Loro Piana cashmere slippers", "loro piana", "Loro Piana"),
    ]:
        assert route(title, feed, brand) not in SEARCHES, title


def _run(tmp_path, price, resale):
    listing, result = t._apparel_listing_result(
        "Maison Margiela Replica GAT sneakers", price, "footwear", "margiela replica",
        search_id=MARGIELA,
    )
    lane = t._lane(tmp_path)
    lane._cheap_result = mock.Mock(return_value=(listing, result, None))
    conn = engine.init_db(lane.seen_db_path)
    strong = lambda merged, _stop: t._record_mock_strong(
        lane, merged, t._passing_strong_result(resale=resale))
    sane = {"is_complete_item": True, "is_part_or_accessory": False, "reason": ""}
    with (
        mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}),
        mock.patch.object(engine, "jev_vinted_score", return_value=0.9),
        mock.patch.object(engine, "check_photos_with_gemini", return_value=t._apparel_ai(resale)),
        mock.patch.object(engine, "_deepseek_alert_sanity_check", return_value=sane),
        mock.patch.object(engine, "send_alert") as send,
        mock.patch.object(fast.VintedFastLane, "_ntfy") as ntfy,
        mock.patch.object(lane, "_run_vinted_strong_vision", side_effect=strong),
    ):
        lane._process_item(conn, None, {}, SEARCHES[MARGIELA], None)
    conn.close()
    row = json.loads(lane.log_path.read_text(encoding="utf-8"))
    return row, send, ntfy


def test_a_steal_at_list_alerts(tmp_path):
    # $40 -> ~$45 all-in vs $200 resale: Steal.
    row, send, _ntfy = _run(tmp_path, 40, 200)
    send.assert_called_once()
    assert row["delivered"] is True, row["reason"]


def test_a_great_deal_at_list_pings_as_an_offer_when_the_offer_is_a_steal(tmp_path):
    # $60 -> ~$73 all-in (with assumed shipping) vs $160: only a Great Deal.
    # A $36 offer is ~$46 all-in, 71% off: Steal.
    row, send, ntfy = _run(tmp_path, 60, 160)
    send.assert_not_called()
    assert row["verdict"] == "OFFER_ALERTED", row["reason"]
    args = ntfy.call_args.args
    assert args[0].startswith("OFFER") and args[2] == "4"  # buzzes, unlike other offers
    assert ntfy.call_args.kwargs["silent_mirror"] is False


def test_a_great_deal_no_offer_can_make_a_steal_stays_quiet(tmp_path):
    # $100 vs $150: neither the 60% offer nor the 50% message reaches Steal.
    row, send, ntfy = _run(tmp_path, 100, 150)
    send.assert_not_called()
    ntfy.assert_not_called()
    assert row["verdict"] == "REJECTED"
