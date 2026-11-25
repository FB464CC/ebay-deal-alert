"""Owner 11-25: waxed Barbour jackets (Beaufort, Bedale, Ashby, Border), L and XL."""
import ebay_deal_alert as engine
import vinted_fast as fast

IDS = {f"vinted-barbour-{m}" for m in ("beaufort", "bedale", "ashby", "border")}


def _lane(tmp_path):
    return fast.VintedFastLane(
        searches=fast.enabled_vinted_searches(), state_path=tmp_path / "s",
        seen_db_path=tmp_path / "d", log_path=tmp_path / "l", schedule=[],
    )


def _route(lane, title, price="60.00"):
    item = {"id": 1, "title": title, "url": "/items/1", "price": {"amount": price},
            "photo": {"url": "https://x/a.webp"}, "photos": [], "item_box": {"first_line": "Barbour"}}
    return (lane._route(item, "barbour") or {}).get("id")


def test_only_the_four_waxed_models_route(tmp_path):
    lane = _lane(tmp_path)
    assert _route(lane, "Barbour Beaufort waxed jacket olive L") == "vinted-barbour-beaufort"
    assert _route(lane, "Barbour Bedale wax jacket C42") == "vinted-barbour-bedale"
    assert _route(lane, "Barbour Ashby jacket XL") == "vinted-barbour-ashby"
    assert _route(lane, "Barbour Border waxed coat") == "vinted-barbour-border"
    # Anything else Barbour either never routes or dies on the cheap title check
    # (the model name is a required phrase), before any item-page request.
    for title in ("Barbour Liddesdale quilted jacket L", "Barbour International wax jacket L",
                  "Barbour tattersall shirt L", "Barbour Beaufort hood", "Barbour Bedale liner",
                  "Barbour quilted Beaufort", "Women's Barbour Beaufort jacket"):
        sid = _route(lane, title)
        search = next((s for s in engine.SAVED_SEARCHES if s["id"] == sid), None)
        listing = {"title": title, "platform": "vinted"}
        assert sid not in IDS or not engine.is_relevant_marketplace_listing(
            listing, search["query"]) or fast.marketplaces.title_matches_exclusion(
            title, fast.marketplaces.split_query_exclusions(search["query"])[1]), title
    assert _route(lane, "Barbour Beaufort waxed jacket L", "120.00") not in IDS  # over the $90 cap


def test_sizes_and_gates(tmp_path):
    search = next(s for s in engine.SAVED_SEARCHES if s["id"] == "vinted-barbour-bedale")
    fits = lambda title, size: fast._fits_size_list(
        search, {"title": title, "size": size, "platform": "vinted"}, "outerwear")
    assert fits("Barbour Bedale", "L") and fits("Barbour Bedale", "XL")
    assert fits("Barbour Bedale C42", None) and fits("Barbour Bedale", "44")
    assert fits("Barbour Bedale", "42")
    assert not fits("Barbour Bedale", "M") and not fits("Barbour Bedale", "38")
    listing = {"title": "Barbour Bedale waxed jacket", "size": "L"}
    result = {"listing": listing}
    fast._vinted_brand_and_floor(result)
    assert result["vinted_brand"] == "Barbour"
    assert fast._menswear_shape_reason(listing, "barbour", "outerwear") is None
    assert fast._off_profile_reason(listing["title"]) is None
