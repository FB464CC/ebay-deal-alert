"""Owner 11-25: Hermes ties on; dress shoes on for "only the absolute biggest steals"."""
import ebay_deal_alert as engine
import platforms as marketplaces
import vinted_fast as fast

BY_ID = {s["id"]: s for s in engine.SAVED_SEARCHES}
TIE = BY_ID["vinted-hermes-tie"]


def _lane(tmp_path):
    return fast.VintedFastLane(
        searches=fast.enabled_vinted_searches(), state_path=tmp_path / "s",
        seen_db_path=tmp_path / "d", log_path=tmp_path / "l", schedule=[],
    )


def _route(lane, title, feed, brand, price="40.00"):
    item = {"id": 1, "title": title, "url": "/items/1", "price": {"amount": price},
            "photo": {"url": "https://x/a.webp"}, "photos": [], "item_box": {"first_line": brand}}
    return (lane._route(item, feed) or {}).get("id")


def test_accented_hermes_title_matches_the_tie_search(tmp_path):
    item = {"id": 1, "title": "Hermès silk tie blue", "url": "/items/1",
            "price": {"amount": "40.00"}, "photo": {"url": "https://x/a.webp"}, "photos": []}
    listing = marketplaces._vinted_item_to_listing(item)
    assert listing["title"] == "Hermes silk tie blue"
    assert engine.is_relevant_marketplace_listing(listing, TIE["query"])
    lane = _lane(tmp_path)
    assert _route(lane, "Hermès silk tie blue", "hermes tie", "Hermès") == "vinted-hermes-tie"
    assert _route(lane, "Hermes Paris necktie 7 fold", "hermes tie", "Hermes") == "vinted-hermes-tie"
    for title in ("Hermes silk scarf", "Hermes belt", "Hermes bow tie", "Hermes tie box"):
        sid = _route(lane, title, "hermes tie", "Hermes")
        assert sid != "vinted-hermes-tie" or marketplaces.title_matches_exclusion(
            title, marketplaces.split_query_exclusions(TIE["query"])[1]) or not \
            fast._VINTED_NECKTIE.search(title), title


def test_ties_pass_the_off_gate_only_for_hermes_and_need_a_proven_seller():
    listing = {"title": "Hermes silk tie", "size": None}
    shape = fast._menswear_shape_reason
    assert "off" in shape(listing, "hermes", "neckwear")
    assert shape(listing, "hermes", "neckwear",
                 wanted_shoes=fast._wanted_shoes(TIE, "neckwear")) is None
    marinella = BY_ID["vinted-e-marinella-tie"]
    assert not fast._wanted_shoes(marinella, "neckwear")  # other ties stay off
    assert "neckwear" in fast.VINTED_STRICT_SELLER_CATEGORIES
    assert fast._seller_trust_reason({"feedback_count": 0}, strict=True)
    result = {"listing": listing}
    fast._vinted_brand_and_floor(result)
    assert result["vinted_brand"] == "Hermes"


def test_dress_shoes_are_on_but_steal_only(tmp_path):
    lane = _lane(tmp_path)
    assert _route(lane, "Alden 986 shell cordovan loafer 12D", "alden", "Alden", "150.00") == "vinted-alden-shoes"
    assert _route(lane, "Allen Edmonds Park Avenue oxford 12", "allen edmonds", "Allen Edmonds", "60.00") == "vinted-allen-edmonds-shoes"
    assert _route(lane, "Zegna leather loafers", "zegna", "Zegna", "80.00") == "vinted-zegna-shoes"
    for sid in ("vinted-alden-shoes", "vinted-allen-edmonds-shoes", "vinted-zegna-shoes",
                "vinted-john-lobb-shoes", "vinted-loro-piana-shoes"):
        search = BY_ID[sid]
        assert search["enabled"] and search["steal_only"]
        assert fast._wanted_shoes(search, "footwear")
        assert fast._steal_only_reason({"deal_rating": "Great Deal"}, search)
        assert fast._steal_only_reason({"deal_rating": "Steal"}, search) is None
