"""Owner 11-20 calls on the Vinted lane: #1 mean resale, #2 light damage is a
note, #3 low-rated clothing seller is a note, #5 French cuffs note, #6 suit
pants waist note. Each: old behaviour fails, new passes, plus a no-regression."""
import vinted_fast as fast
from test_vinted_fast import (  # noqa: F401  (_no_live_seller_lookup is an autouse fixture)
    TRUSTED_SELLER,
    _apparel_ai,
    _apparel_listing_result,
    _no_live_seller_lookup,
    _passing_strong_result,
    _run_custom_pipeline,
)


def _zegna_sweater(tmp_path, ai=None, strong=None, title="Zegna Cashmere sweater", price=10):
    listing, result = _apparel_listing_result(
        title, price, "knitwear", "zegna", search_id="vinted-owner-calls",
    )
    return _run_custom_pipeline(
        tmp_path, listing, result, ai or _apparel_ai(150),
        strong_result=strong or _passing_strong_result(150),
    )


def _alert_text(send):
    return send.call_args[0][0]["vinted_alert_details"]


# 1. Steal bar: conservative resale = mean of the two looks (was min).
def _veto_view(cheap, strong, paid, title="Ermenegildo Zegna polo size L"):
    return {
        "listing": {"title": title},
        "category": "golf",
        "vinted_brand_key": "zegna",
        "vinted_strong_checked": True,
        "vinted_strong_label_other_brand": False,
        "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_brand_label_visible_and_consistent": True,
        "vinted_strong_condition_grade": "excellent",
        "vinted_strong_estimated_resale_value": strong,
        "estimated_resale_value": cheap,
        "vinted_expected_paid": paid,
    }


def test_steal_bar_uses_mean_of_both_looks():
    veto = fast.VintedFastLane._strong_look_veto_reason
    # 11-20 trigger: $15.90 paid, looks $55 / $35. min -> $14 bar (veto); mean -> $18.
    view = _veto_view(55, 35, 15.9)
    assert veto(None, view) is None
    assert view["vinted_conservative_resale_value"] == 45


def test_steal_bar_still_vetoes_over_forty_percent_of_mean():
    veto = fast.VintedFastLane._strong_look_veto_reason
    assert veto(None, _veto_view(55, 35, 18.5)) == (
        "expected paid $18.5 exceeds 40% of conservative $45 resale"
    )


# 2. Light damage is a note; real damage still blocks.
def test_light_damage_words():
    for text in (
        "minor wear", "tiny spot on the sleeve", "small smudge", "light pilling",
        "faint mark near the hem", "black smudge or mark on the front",
        "light picking, no holes", "Disclosed tiny spot; no holes, tears, or major stains",
    ):
        assert fast._light_damage_only(text), text
    for text in (
        "small hole near the hem", "tear at the seam", "large stain on the front",
        "obvious stain", "broken zipper", "missing buttons", "heavy pilling",
        "discoloration across the garment", "stain on the front", "moth damage",
        "light pilling and a hole on the sleeve", "", "damage visible",
    ):
        assert not fast._light_damage_only(text), text


def test_first_look_light_damage_alerts_with_note(tmp_path):
    ai = _apparel_ai(150, damage_found=True, damage_desc="tiny spot on the left cuff")
    _lane, _strong, send, row = _zegna_sweater(tmp_path, ai=ai)
    send.assert_called_once()
    assert row["delivered"] is True
    assert "light damage: tiny spot on the left cuff" in _alert_text(send)


def test_first_look_real_damage_still_blocks(tmp_path):
    ai = _apparel_ai(150, damage_found=True, damage_desc="small hole on the chest")
    _lane, _strong, send, row = _zegna_sweater(tmp_path, ai=ai)
    send.assert_not_called()
    assert row["reason"].startswith("AI photo check found damage or unwanted logo")


def test_first_look_light_damage_still_blocks_watches():
    result = {"listing": {"title": "Seiko 5"}}
    reason = fast._merge_ai_result(
        result, {"damage_found": True, "damage_desc": "light wear", "summary": "Seiko"}, "watches"
    )
    assert reason and "damage" in reason


def test_strong_fair_grade_with_light_flaws_alerts_with_note(tmp_path):
    strong = _passing_strong_result(150, condition_grade="fair", flaws_found=["light pilling"])
    _lane, _strong, send, _row = _zegna_sweater(tmp_path, strong=strong)
    send.assert_called_once()
    assert "condition fair, light flaws only: light pilling" in _alert_text(send)


def test_strong_fair_grade_with_real_flaws_still_blocks(tmp_path):
    strong = _passing_strong_result(150, condition_grade="fair", flaws_found=["light pilling", "small hole"])
    _lane, _strong, send, row = _zegna_sweater(tmp_path, strong=strong)
    send.assert_not_called()
    assert "condition grade fair below good" in row["reason"]


# 3. Clothing seller 3+ reviews under 4.0 stars: note, not block.
def test_low_rated_clothing_seller_alerts_with_note(tmp_path, monkeypatch):
    low = dict(TRUSTED_SELLER, feedback_count=32.0, feedback_reputation=0.64)
    monkeypatch.setattr(fast, "fetch_vinted_seller_stats", lambda _uid: dict(low))
    _lane, _strong, send, _row = _zegna_sweater(tmp_path)
    send.assert_called_once()
    assert "seller LOW-RATED 3.2/5 (32 reviews): pay in-app only" in _alert_text(send)


def test_low_rated_watch_seller_still_blocked():
    low = dict(TRUSTED_SELLER, feedback_count=32.0, feedback_reputation=0.64)
    assert fast._seller_trust_reason(low, strict=True)


# 5. French cuffs: note, not block.
def test_french_cuffs_are_a_note():
    listing = {"title": "Ermenegildo Zegna Dress Shirt Mens 16.5 42 White French Cuff", "size": "16.5"}
    assert fast._menswear_shape_reason(listing, "zegna", "other") is None
    assert fast._ai_lone_tailoring_reason("white shirt with French cuffs", "other", listing, "zegna") is None
    assert "French cuffs: needs cufflinks" in fast._alert_details({"listing": listing})
    # Photo-only French cuffs also get the note.
    plain = {"listing": {"title": "Brioni shirt"}, "vinted_ai_summary": "white shirt, French cuffs"}
    assert "French cuffs: needs cufflinks" in fast._alert_details(plain)


def test_vintage_french_cuff_shirt_still_blocked():
    listing = {"title": "Men's Vintage Brioni French Cuff Shirt Italy L", "size": "L"}
    assert fast._menswear_shape_reason(listing, "brioni", "other")
    assert "French cuffs" not in fast._alert_details({"listing": {"title": "Brioni shirt"}})


# 6. Full suit with an off pants waist: note when the jacket fits.
def test_suit_with_off_waist_alerts_with_note():
    listing = {"title": "Brooks Brothers 346 Suit Blazer 40R Pants 39x30", "size": "40R"}
    assert fast._off_size_reason(listing, "tailoring") is None
    assert "pants waist 39: needs tailoring" in fast._alert_details({"listing": listing})


def test_lone_trousers_and_off_jacket_still_blocked():
    trousers = {"title": "Zegna wool trousers W38", "size": "W38"}
    assert fast._off_size_reason(trousers, "tailoring") == "pants waist won't fit (owner wears 32-36)"
    assert fast._menswear_shape_reason(trousers, "zegna", "tailoring")
    off_jacket = {"title": "Canali suit 48R pants 38x30", "size": "48R"}
    assert fast._off_size_reason(off_jacket, "tailoring")
    fits = {"title": "Canali suit 42R pants 34x30", "size": "42R"}
    assert "pants waist" not in fast._alert_details({"listing": fits})


def test_suit_size_gate_eu_and_italian_short():
    fits = ["52", "52L", "EU52", "IT 52", "EU 52 L", "42L", "50 R"]
    shorts = ["50C", "50 C", "52/C", "50 Corto", "40S", "40 Short", "EU 56"]
    assert all(fast._VINTED_SUIT_SIZE.search(s) for s in fits)
    assert not any(fast._VINTED_SUIT_SIZE.search(s) for s in shorts)
