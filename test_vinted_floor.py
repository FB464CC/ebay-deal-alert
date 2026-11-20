"""Owner 11-20: no Vinted resale floor; the 40% steal bar decides."""
from unittest import mock

import vinted_fast as fast
from test_vinted_fast import (  # noqa: F401  (_no_live_seller_lookup is an autouse fixture)
    _apparel_ai,
    _apparel_listing_result,
    _no_live_seller_lookup,
    _passing_strong_result,
    _run_custom_pipeline,
)


def _zegna_polo(tmp_path):
    # Trigger case 11-20: $15 Zegna golf polo, first look $55, second look $35.
    listing, result = _apparel_listing_result(
        "Ermenegildo Zegna blue casual golf shirt sleeve polo men's size large cotton",
        15, "golf", "zegna", search_id="vinted-zegna-polo",
    )
    return _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(55),
        strong_result=_passing_strong_result(35),
    )


def test_zegna_polo_is_not_floor_rejected_and_faces_the_steal_bar(tmp_path):
    _lane, strong, _send, row = _zegna_polo(tmp_path)
    strong.assert_called_once()
    # Same steal bar as before: $15.90 expected paid vs 40% of min($55, $35).
    assert row["reason"] == (
        "vinted strong second-look veto: expected paid $15.9 exceeds 40% of "
        "conservative $35 resale"
    )


def test_fifty_cent_item_worth_ten_is_not_floor_rejected(tmp_path):
    listing, result = _apparel_listing_result(
        "Peter Millar polo size M", 0.5, "golf", "peter millar polo",
        search_id="vinted-fifty-cents",
    )
    _lane, _strong, send, row = _run_custom_pipeline(
        tmp_path, listing, result, _apparel_ai(10),
        strong_result=_passing_strong_result(10),
    )
    send.assert_called_once()
    assert row["delivered"] is True


def test_resale_minimum_is_zero_except_brooks_brothers():
    assert fast.VINTED_RESALE_FLOOR_MAX == 0
    assert fast.VINTED_OFFER_MIN_LIST == fast.VINTED_OFFER_MIN_LIST_TOPS == 0
    for title in ("Ermenegildo Zegna polo", "Peter Millar Hat", "Loro Piana sweater"):
        assert fast._vinted_resale_minimum({"listing": {"title": title}}) == 0
    bb = {"listing": {"title": "Brooks Brothers wool suit"}}
    assert fast._vinted_resale_minimum(bb) == fast.VINTED_MIN_RESALE_BY_BRAND["brooks brothers"] > 0


def test_brooks_brothers_still_floor_rejected(tmp_path):
    listing, result = _apparel_listing_result(
        "Brooks Brothers Wool Suit 42R", 15, "tailoring", "brooks brothers",
        search_id="vinted-brooks-brothers",
    )
    _lane, _strong, send, row = _run_custom_pipeline(tmp_path, listing, result, _apparel_ai(80))
    send.assert_not_called()
    assert "brand floor" in row["reason"]


def test_cap_of_40_restores_the_old_floor_reject(tmp_path):
    with mock.patch.object(fast, "VINTED_RESALE_FLOOR_MAX", 40.0):
        _lane, _strong, send, row = _zegna_polo(tmp_path)
    send.assert_not_called()
    assert row["reason"] == (
        "vinted strong second-look veto: second-look resale $35 below $40 brand floor"
    )


def test_ten_dollar_zegna_polo_reaches_the_offer_path(tmp_path):
    # 11-20: the $12 tops / $25 list floors stopped offers on cheap lists.
    # (At first look $55 / second look $35 a $10 list alerts outright; the
    # offer path is what runs when list fails a bar.)
    from test_vinted_fast import _lane
    import ebay_deal_alert as engine

    listing, result = _apparel_listing_result(
        "Ermenegildo Zegna blue casual golf shirt sleeve polo men's size large cotton",
        10, "golf", "zegna", search_id="vinted-zegna-ten",
    )
    result.update(_apparel_ai(55), vinted_brand_key="zegna", vinted_strong_estimated_resale_value=35)
    lane = _lane(tmp_path)
    conn = engine.init_db(lane.seen_db_path)
    try:
        for min_tops, reached in ((fast.VINTED_OFFER_MIN_LIST_TOPS, True), (12.0, False)):
            with mock.patch.object(fast, "VINTED_OFFER_MIN_LIST_TOPS", min_tops), \
                    mock.patch.object(fast, "_offer_view", return_value=None) as view:
                assert not lane._try_offer_alert(conn, result, "golf", {}, {"query": "zegna"}, None)
            assert view.called is reached
    finally:
        conn.close()


def test_reserved_listing_patterns():
    import vinted_fast as vf
    hits = ["Custom listing Peter millar", "Reserved for Sarah", "bundle for @jlee22",
            "Private listing - do not buy"]
    misses = ["Peter Millar polo listing for men", "Zegna polo bundle for summer",
              "Peter Millar quarter zip L", "Not reserved for anyone - Zegna polo",
              "Custom order Zegna suit 42R"]
    for t in hits:
        assert vf._VINTED_RESERVED.search(t), t
    for t in misses:
        assert not vf._VINTED_RESERVED.search(t), t
    assert vf._VINTED_RESERVED_DESC.search("Reserved for @mike, please don't buy")
    assert not vf._VINTED_RESERVED_DESC.search("Message me for a custom bundle!")
