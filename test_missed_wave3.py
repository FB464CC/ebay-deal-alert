import os
from unittest import mock

import ebay_deal_alert as engine
import platforms as marketplaces
import vinted_fast as fast
from test_vinted_fast import TRUSTED_SELLER, _lane


def _pm_polo(price):
    listing = marketplaces.make_listing(
        "vinted", "pm-1", "Peter Millar Polo Size L", price,
        "https://www.vinted.com/items/pm-1", image_url="https://images.example/main.jpg",
        shipping=0.0, size="L",
    )
    result = engine.score_listing(listing, None, shipping_cost=0.0, category="golf")
    result.update(item_price=float(price), shipping_cost=0.0, category="golf",
                  search_query="peter millar polo", search_id="vinted-peter-millar-polo")
    return listing, result


def test_price_table_reject_spends_no_photo_check(tmp_path, monkeypatch):
    # Audit 11-23: $13 PM polo, ~$15 all-in vs the $30 row -> rejected
    # "no AI check", yet the early photo look had already been paid for.
    monkeypatch.setattr(fast, "VINTED_PRESCREEN_MAX_PER_RUN", 0)
    lane = _lane(tmp_path)
    listing, result = _pm_polo(25)
    lane._cheap_result = mock.Mock(return_value=(listing, result, None))
    conn = engine.init_db(lane.seen_db_path)
    with (
        mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": "test-key"}),
        mock.patch.object(fast, "fetch_vinted_seller_stats", return_value=dict(TRUSTED_SELLER)),
        mock.patch.object(lane, "_first_photo_check") as check,
    ):
        lane._process_item(conn, None, {}, {"query": result["search_query"]}, None)
    conn.close()
    assert check.call_count == 0 and lane.ai_calls == 0
    assert result["reason"].startswith("price table:")


def test_cheap_polo_still_gets_its_early_look(tmp_path, monkeypatch):
    monkeypatch.setattr(fast, "VINTED_PRESCREEN_MAX_PER_RUN", 0)
    lane = _lane(tmp_path)
    listing, result = _pm_polo(8)
    lane._cheap_result = mock.Mock(return_value=(listing, result, None))
    conn = engine.init_db(lane.seen_db_path)
    with (
        mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "OPENROUTER_API_KEY": "test-key"}),
        mock.patch.object(fast, "fetch_vinted_seller_stats", return_value=dict(TRUSTED_SELLER)),
        mock.patch.object(lane, "_first_photo_check", return_value=None) as check,
    ):
        lane._process_item(conn, None, {}, {"query": result["search_query"]}, None)
    conn.close()
    assert check.call_count == 1 and lane.ai_calls == 1


def _lp_suit_view(label_reason, title="Loro Piana high end Italian Suit, size 54"):
    return {
        "listing": {"title": title},
        "category": "other",
        "vinted_brand_key": "loro piana",
        "vinted_strong_checked": True,
        "vinted_strong_label_other_brand": False,
        "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_brand_label_visible_and_consistent": True,
        "vinted_strong_brand_label_reason": label_reason,
        "vinted_strong_condition_grade": "excellent",
        "vinted_strong_estimated_resale_value": 250,
        "estimated_resale_value": 600,
        "vinted_expected_paid": 139.867,
    }


def test_mill_cloth_label_on_a_suit_is_not_the_house():
    # 11-19 10888080078: under the 11-20 mean bar ($425 -> $170) this alerts at list.
    veto = fast.VintedFastLane._strong_look_veto_reason
    reason = veto(None, _lp_suit_view("Label reads 'Ing. Loro Piana & C.' Tasmanian, Fabric made in Italy"))
    assert reason and reason.startswith("another maker using Loro Piana/Zegna fabric")


def test_house_label_suit_still_passes():
    veto = fast.VintedFastLane._strong_look_veto_reason
    assert veto(None, _lp_suit_view("Neck label reads Loro Piana, Made in Italy")) is None


def test_gender_block_logs_the_matched_word(caplog):
    caplog.set_level("INFO")
    reason = engine._text_safety_hard_fails(
        "peter millar summer comfort polo. bought for my husband, my son's school colors, fits boys too"
    )
    assert reason == "excluded gender keyword in title/description"
    assert "gender keyword context:" in caplog.text and "fits boys too" in caplog.text


def test_house_garment_cloth_labels_are_not_a_mill_label():
    # Review 11-23 S4: genuine Zegna/LP tailoring carries a cloth label too.
    house = [
        {"vinted_brand_key": "zegna", "listing": {"title": "Ermenegildo Zegna navy wool suit 52R"},
         "vinted_ai_summary": "brand label and Trofeo fabric label inside the jacket"},
        {"vinted_brand_key": "zegna", "listing": {"title": "Ermenegildo Zegna blazer 42R"},
         "vinted_strong_brand_label_reason": "Zegna house label; cloth label reads Electa"},
        {"vinted_brand_key": "loro piana", "listing": {"title": "Loro Piana cashmere sport coat"},
         "vinted_ai_summary": "Loro Piana label, fabric made in Italy, excellent condition"},
    ]
    for view in house:
        assert fast._ai_mill_label(view) is False, view
    mill = {"vinted_brand_key": "zegna", "listing": {"title": "Jos A Bank suit 42R"},
            "vinted_ai_summary": "Jos. A. Bank maker label; fabric by Ermenegildo Zegna"}
    assert fast._ai_mill_label(mill) is True
    # The original 11-19 case is still blocked.
    assert fast._ai_mill_label(_lp_suit_view("Label reads 'Ing. Loro Piana & C.' Tasmanian"))
