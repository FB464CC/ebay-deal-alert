"""Audit 11-20 blocker fixes for the eBay top-5 lane (collar, suit size, house, garment, seller)."""
from unittest.mock import Mock

import pytest

import ebay_top5 as lane


def item(title, *, size="L", score=120, pct=99.5, price=80):
    return {"itemId": "v1|7|0", "title": title, "price": {"value": str(price)}, "size": size,
            "buyingOptions": ["FIXED_PRICE"],
            "estimatedAvailabilities": [{"estimatedAvailableQuantity": 1}],
            "seller_feedback_score": score, "seller_feedback_percentage": pct,
            "itemWebUrl": "https://www.ebay.com/itm/7"}


def reason(listing):
    return lane.EbayTop5()._cheap_reason(listing, False)[2]


@pytest.mark.parametrize("title,size", [
    ("Kiton Dress Shirt Blue", ""),              # unstated collar, no size: unknown
    ("Kiton Dress Shirt Blue", "L"),             # unstated collar, letter size
    ("Kiton Dress Shirt 16.5 Blue", ""),
    ("Kiton Dress Shirt 16 1/2", ""),
    ("Kiton Dress Shirt 16½", ""),
    ("Kiton Dress Shirt Neck 16.5", ""),
    ("Kiton Dress Shirt 16.5 34/35", ""),
    ("Kiton Dress Shirt", "16 1/2"),
    ("Kiton Dress Shirt collar 42", ""),
    ("Kiton Dress Shirt 42 cm", ""),
])
def test_collar_unstated_or_16_5_passes(title, size):
    assert reason(item(title, size=size)) is None


@pytest.mark.parametrize("title,size", [
    ("Kiton Dress Shirt 17.5", ""),
    ("Kiton Dress Shirt 15 1/2", ""),
    ("Kiton Dress Shirt Neck 17", ""),
    ("Kiton Dress Shirt collar 43", ""),
    ("Kiton Dress Shirt", "17 in"),
])
def test_stated_wrong_collar_still_rejects(title, size):
    assert "collar" in reason(item(title, size=size))


@pytest.mark.parametrize("value,size", [
    ("16 1/2", "16.5 in L"), ("16½", "16.5 in L"), ("42", "16.5 in L"),
    ("16.5 (42)", "16.5 in L"), ("16.5", "16.5 in L"), ("43", "17 in L"),
])
def test_get_item_neck_aspect_normalizes(monkeypatch, value, size):
    response = Mock()
    response.json.return_value = {"localizedAspects": [{"name": "Neck Size", "value": value},
                                                       {"name": "Size", "value": "L"}]}
    monkeypatch.setattr(lane.requests, "get", Mock(return_value=response))
    assert lane._fetch_details("token", "v1|1|0")["size"] == size


@pytest.mark.parametrize("title", [
    "Kiton Full Suit Navy Wool 52", "Brioni Suit 42 Navy", "Kiton Suit Size 50",
    "Brioni Suit 40R",
])
def test_bare_suit_sizes_pass(title):
    assert reason(item(title, size="", price=200)) is None


def test_suit_without_any_size_still_rejects():
    assert "40-44" in reason(item("Kiton Full Suit Navy Wool", size="", price=200))


@pytest.mark.parametrize("title,house", [
    ("EUC Ermenegildo Zegna Cashmere Sweater", "zegna"),
    ("Gorgeous Navy Ermenegildo Zegna Wool Sweater", "zegna"),
    ("Zegna Cashmere Sweater", "zegna"),
    ("Cucinelli Cashmere Sweater", "brunello cucinelli"),
    ("Brunello Cucinelli Cashmere Sweater", "brunello cucinelli"),
])
def test_house_check(title, house):
    assert lane._brand(title) == house


@pytest.mark.parametrize("title", [
    "Navy Zegna Wool Sweater",           # bare Zegna mid-title stays anchored
    "Z Zegna Ermenegildo Zegna Sweater",  # diffusion
    "Ralph Lauren Cashmere Sweater",
])
def test_house_check_still_rejects(title):
    assert lane._brand(title) is None


@pytest.mark.parametrize("title", [
    "Loro Piana Cashmere Crewneck", "Brunello Cucinelli Cashmere Turtleneck",
    "Zegna Half Zip Cashmere", "Kiton 1/2 Zip Cashmere", "Kiton Shirt Blue",
])
def test_wanted_garments_pass(title):
    assert reason(item(title)) is None


def test_knit_words_set_knitwear_category():
    assert lane._category("Loro Piana Cashmere Crewneck") == "knitwear"


@pytest.mark.parametrize("title", [
    "Loro Piana Men's Trousers 34", "Kiton Leather Belt", "Brioni Silk Tie",
    "Loro Piana Suede Loafers", "Kiton Cotton T-Shirt",
])
def test_unwanted_garments_still_off(title):
    assert reason(item(title)) is not None


@pytest.mark.parametrize("score,pct", [(0, 0), (3, 100), (19, 90), (500, 97.8), (20, 95)])
def test_seller_flag_not_block(score, pct):
    assert lane._seller_reason(item("x", score=score, pct=pct)) is None


@pytest.mark.parametrize("score,pct", [(20, 94.9), (3000, 90)])
def test_seller_real_negative_history_blocks(score, pct):
    assert "95%" in lane._seller_reason(item("x", score=score, pct=pct))


def test_seller_missing_still_blocks():
    assert lane._seller_reason({"title": "x"}) == "seller feedback missing"


def test_seller_notes():
    assert "NEW eBay seller (3 feedback" in lane._seller_note(item("x", score=3, pct=100))
    assert "97.8% positive" in lane._seller_note(item("x", score=500, pct=97.8))
    assert lane._seller_note(item("x", score=500, pct=99.5)) == ""


def test_new_seller_note_reaches_alert_text(tmp_path, monkeypatch):
    bot = lane.EbayTop5(db_path=tmp_path / "seen.db")
    conn = lane._db(bot.db_path)
    listing = item("Brioni Cashmere Sweater Men Large", score=4, pct=100, price=30)
    monkeypatch.setattr(lane, "_fetch_details", lambda *_: {"shippingOptions": [{"shippingCost": {"value": "0"}}]})
    monkeypatch.setattr(bot.vision, "_first_photo_check", lambda *_: {"summary": "clean sweater", "counterfeit_suspected": False,
        "damage_found": False, "weird_logo_found": False, "estimated_resale_value": 400,
        "price_confidence": "high", "looks_good": True})
    monkeypatch.setattr(bot.vision, "_run_vinted_strong_vision", lambda result, _: result.update({
        "vinted_strong_checked": True, "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_brand_label_visible_and_consistent": True,
        "vinted_strong_condition_grade": "excellent", "vinted_strong_estimated_resale_value": 400}) or {})
    monkeypatch.setattr(bot.vision, "_strong_look_veto_reason", lambda result: result.update({
        "vinted_conservative_resale_value": 400}) or None)
    monkeypatch.setattr(lane.engine, "_deepseek_alert_sanity_check", lambda *_, **__: {
        "is_part_or_accessory": False, "is_complete_item": True})
    send = Mock(return_value=True)
    monkeypatch.setattr(lane.engine, "_send_ntfy_notice", send)
    bot._judge(conn, "token", listing, False, bot.clock() + 300)
    assert send.call_count == 1, conn.execute("SELECT reason FROM judged").fetchone()
    assert "NEW eBay seller (4 feedback" in send.call_args[0][0]
    conn.close()


# Review 11-20 follow-ups.
@pytest.mark.parametrize("title", [
    "Brunello Cucinelli Crewneck T-Shirt", "Loro Piana Crew Neck Tee",
    "Brunello Cucinelli Crew Neck Cotton Jersey T Shirt", "Kiton Crewneck Sweatshirt",
    "Brunello Cucinelli Tee Shirt",
])
def test_tees_and_sweatshirts_are_not_knitwear(title):
    assert reason(item(title)) == "not a wanted garment"


@pytest.mark.parametrize("title", [
    "Jos A Bank Ermenegildo Zegna Suit 42R", "Hickey Freeman Ermenegildo Zegna Suit 42R",
    "Brooks Brothers Ermenegildo Zegna Trofeo Suit 42R",
    "Samuelsohn Ermenegildo Zegna Traveller Suit 42R",
    "Canali Ermenegildo Zegna Cashmere Sweater L",
])
def test_other_makers_in_zegna_cloth_are_not_zegna(title):
    assert lane._brand(title) is None


@pytest.mark.parametrize("title", [
    "NWT $1,295 Ermenegildo Zegna Cashmere Sweater", "EUC - Ermenegildo Zegna Wool Suit 42R",
    "Brand New Navy Ermenegildo Zegna Sweater",
])
def test_condition_colour_price_prefixes_still_zegna(title):
    assert lane._brand(title) == "zegna"


@pytest.mark.parametrize("description", [
    "Jacket chest 46 in, pants waist 40", "Size 56 EU / 46 US, waist 42", "50% wool 50% silk",
])
def test_suit_size_only_in_description_rejects(description):
    listing = {**item("Kiton Suit Navy Wool", size="", price=200), "description": description}
    assert reason(listing) == "suit size only in the description"
    assert "suit size only in the description" in lane.SIZE_EVIDENCE


@pytest.mark.parametrize("title", [
    "Kiton Dress Shirt L 46 cm chest", "Kiton Shirt Chest 44 cm", "Kiton Shirt Shoulder 45 cm",
])
def test_body_measurement_cm_is_not_a_collar(title):
    assert lane._collar(item(title, size="")) is None


def test_neck_aspect_40_reads_as_15_75_and_rejects():
    assert reason(item("Kiton Dress Shirt", size="15.75 in L")) == "collar 15.75 won't fit (owner wears 16.5)"
