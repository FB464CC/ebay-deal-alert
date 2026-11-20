"""Owner calls 11-20 in the eBay engine: #4 poly rule off for Vinted, #8 top-5 lane takes New."""
from unittest.mock import Mock

import ebay_deal_alert as engine
import ebay_top5 as lane

POLY_REASON = "poly over $15, no premium fabric keyword"


def _poly(platform, url):
    listing = {"itemId": "x", "title": "Peter Millar Crown Polo Polyester Performance Quarter Zip",
               "price": {"value": "30"}, "itemWebUrl": url}
    if platform:
        listing["platform"] = platform
    return engine.score_listing(listing, None, category="knitwear")


def test_poly_rule_skips_vinted():
    assert _poly("vinted", "https://www.vinted.com/items/1").get("reason") != POLY_REASON


def test_poly_rule_still_blocks_ebay():
    assert _poly(None, "https://www.ebay.com/itm/1")["reason"] == POLY_REASON


def _filter(call, search):
    response = Mock(status_code=200)
    response.json.return_value = {"itemSummaries": [], "total": 0}
    response.raise_for_status.return_value = None
    get = Mock(return_value=response)
    orig = engine.requests.get
    engine.requests.get = get
    try:
        call("token", search)
    finally:
        engine.requests.get = orig
    return get.call_args.kwargs["params"]["filter"].split("conditionIds:{", 1)[1].split("}", 1)[0].split("|")


def test_top5_lane_searches_include_new_conditions():
    buy, auction = lane.searches()
    for call, search in ((engine.search_ebay, buy), (engine.search_ebay_ending_soon_auctions, auction)):
        ids = _filter(call, search)
        assert {"1000", "1500"} <= set(ids)  # New with tags, New without tags
        assert "1750" not in ids and "7000" not in ids  # New with defects, For parts
        assert set(engine.EBAY_ALLOWED_SECONDHAND_CONDITION_IDS) <= set(ids)


def test_other_ebay_searches_keep_secondhand_only():
    search = {"query": "hamilton watch", "category_id": "31387", "max_price": 500}
    for call in (engine.search_ebay, engine.search_ebay_ending_soon_auctions):
        assert _filter(call, search) == list(engine.EBAY_ALLOWED_SECONDHAND_CONDITION_IDS)
