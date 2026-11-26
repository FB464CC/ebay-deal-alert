"""Audit 11-25: three false-block classes found in one day of rejections."""
import ebay_deal_alert as engine
import vinted_fast as fast


def test_brand_prefix_does_not_invent_the_lauren_diffusion_line():
    title = fast._prefix_brand("Polo Ralph Lauren", "Ralph Lauren Purple Label")
    assert title == "Polo Ralph Lauren Purple Label"
    assert not engine.brand_in(title.lower(), engine.PASS_BRANDS)
    assert fast._prefix_brand("Peter Millar", "Cashmere sweater") == "Peter Millar Cashmere sweater"
    assert fast._prefix_brand("Loro Piana", "piana cashmere") == "Loro Piana cashmere"
    # A real Lauren Ralph Lauren piece still reads as one.
    real = fast._prefix_brand("Lauren Ralph Lauren", "blazer 42R")
    assert engine.brand_in(real.lower(), engine.PASS_BRANDS)


def test_table_backed_value_does_not_need_ai_price_confidence():
    base = {"vinted_photo_ai_checked": True, "price_confidence": "low", "deal_rating": "Steal",
            "estimated_resale_value": 30, "listing": {"title": "Peter Millar polo"}}
    assert "confidence" in fast._vinted_apparel_bar_reason(dict(base), "golf")
    reason = fast._vinted_apparel_bar_reason(dict(base, vinted_price_table=True), "golf")
    assert not reason or "confidence" not in reason


def test_mens_title_beats_a_womens_format_size():
    assert fast._MENS_TITLE.search("Peter Millar Hoodie Men’s Size Medium Antique Cream")
    assert fast._MENS_TITLE.search("Peter Millar mens quarter zip")
    for title in ("Peter Millar Women's quarter zip", "Peter Millar skort", "Peter Millar Quarterzip"):
        assert not (fast._MENS_TITLE.search(title) and not fast._WOMENS_TITLE.search(title)), title
    assert fast._WOMENS_FORMAT_SIZE.match("M / US 8-10")
    assert not fast._WOMENS_FORMAT_SIZE.match("Youth 10/12")  # kids sizes still block


def test_a_stated_us_shoe_size_outranks_a_uk_reading():
    search = next(s for s in engine.SAVED_SEARCHES if s["id"] == "vinted-allen-edmonds-shoes")
    fits = lambda title, size: fast._fits_size_list(
        search, {"title": title, "size": size, "platform": "vinted"}, "footwear")
    # 11-25: this US 11.5 pair sent an offer alert; the owner wears US 12-13.
    assert not fits("Allen Edmonds Strandmok Men's Oxford Navy Black Men's US 11.5/ 10.5 UK", "11.5")
    assert fits("Allen Edmonds Park Avenue US 12 / UK 11", "12")
    assert fits("Allen Edmonds Park Avenue oxford", "13")
    assert fits("Alden loafer UK 11.5", None)
