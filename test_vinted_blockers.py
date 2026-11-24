"""Audit 11-20 blocker fixes: logo veto, IT/US men's sizing, Zegna sport shirts."""
import json

import vinted_fast as fast


def _lane(tmp_path):
    return fast.VintedFastLane(
        searches=[],
        state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        main_seen_db_path=tmp_path / "missing-main.db",
        run_seconds=1,
    )


def _veto(lane, desc, brand_key):
    return lane._strong_look_veto_reason({
        "listing": {"title": "jacket", "price": {"value": 70}},
        "category": "outerwear",
        "vinted_brand_key": brand_key,
        "vinted_strong_checked": True,
        "vinted_strong_label_other_brand": False,
        "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_brand_label_visible_and_consistent": True,
        "vinted_strong_third_party_logo": True,
        "vinted_strong_logo_desc": desc,
    }) or ""


def test_logo_on_hanger_label_or_own_mark_is_not_vetoed(tmp_path):
    lane = _lane(tmp_path)
    # Before: the $70 Purple Label suede jacket was vetoed over the hanger.
    assert "logo" not in _veto(lane, "The hanger has an 'EMPORIO ARMANI' logo.", "purple label")
    assert "logo" not in _veto(lane, "Store name printed on the price tag", "zegna")
    assert "logo" not in _veto(
        lane, "small circular logo with a '4' on the neck label, which is the Peter Millar "
        "Crown Sport logo", "peter millar")
    assert "logo" not in _veto(
        lane, "Crown emblem on the collar which is the Ralph Lauren Purple Label mark",
        "purple label")


def test_club_and_corporate_logos_still_vetoed(tmp_path):
    lane = _lane(tmp_path)
    assert "Pebble Beach" in _veto(lane, "Pebble Beach crest on chest", "peter millar")
    assert "logo" in _veto(lane, "Goldman Sachs logo on the sleeve; brand label at neck", "zegna")
    assert "logo" in _veto(lane, "Augusta National logo on the chest", "peter millar")
    # Another brand's mark on the garment is still third-party.
    assert "logo" in _veto(lane, "Callaway logo on the collar, which is not Zegna", "zegna")
    # Owner 11-20: an own-mark clause doesn't excuse a club logo elsewhere.
    assert "Pebble Beach" in _veto(lane, "Pebble Beach logo; the rest is Peter Millar", "peter millar")
    assert "Pebble Beach" in _veto(
        lane, "Pebble Beach logo, and the hanger is Peter Millar", "peter millar")
    assert "logo" in _veto(lane, "Club crest on the chest; initials inside the collar", "zegna")
    assert "no detail" in _veto(lane, "", "zegna")
    # Review 11-20: a label/tag as a reference point doesn't exempt an outside mark.
    for desc, brand in [
        ("Nike swoosh embroidered beside the brand label at the hip", "zegna"),
        ("Titleist logo near the waistband tag", "peter millar"),
        ("Goldman Sachs logo above the label", "zegna"),
        ("Augusta National embroidered logo next to the label on the hip", "zegna"),
        ("Pebble Beach woven label sewn at the left hip", "peter millar"),
        # Review 11-20: an own-mark phrase doesn't excuse a club logo in the same clause.
        ("Embroidered Pebble Beach crest on the left breast which is the Peter Millar "
         "Crown Sport collection", "peter millar"),
        ("Pebble Beach logo on a shirt that is Peter Millar", "peter millar"),
        ("Embroidered TPC Sawgrass logo with the Peter Millar crown which is the "
         "Peter Millar mark", "peter millar"),
    ]:
        assert "logo" in _veto(lane, desc, brand), desc
    # Owner 11-24: USC marks ("Carolina" included) never count as third-party
    # (an $18 "Peter Millar Gamecock shirt" was vetoed here).
    for desc in ["USC Gamecocks logo", "South Carolina Gamecocks block C on chest",
                 "Carolina script on the left chest", "Cocky mascot on the sleeve",
                 "Gamecocks logo on the chest; initials inside the collar"]:
        assert "logo" not in _veto(lane, desc, "peter millar"), desc
    # Other Carolinas, the Trojans' USC, and a club logo beside a USC one still veto.
    for desc in ["North Carolina Tar Heels logo on chest", "USC Trojans logo on chest",
                 "Carolina Panthers logo on sleeve", "Carolina Country Club crest on chest",
                 "Gamecocks logo on the chest; Pebble Beach logo on the sleeve",
                 "Gamecocks logo and a Titleist logo on the sleeve"]:
        assert "logo" in _veto(lane, desc, "peter millar"), desc


def test_it_us_mens_suit_size_is_not_womens():
    title = "Vintage Canali Men's Gray Striped Wool 2-Piece Suit IT 50 US 40 Made in Italy"
    assert not fast._VINTED_NOT_MENS.search(title.replace("Men's ", ""))
    assert not fast._VINTED_NOT_MENS.search("Zegna suit IT 52 US 42")
    assert not fast._VINTED_NOT_MENS.search("Brioni blazer IT52/US42")
    # Real women's sizes still match.
    assert fast._VINTED_NOT_MENS.search("Max Mara coat IT 42 USA 6")
    assert fast._VINTED_NOT_MENS.search("Prada skirt IT 46 US 10")
    assert fast._VINTED_NOT_MENS.search("Gucci dress IT44/US8")
    # Review 11-20: women's plus sizes stay blocked.
    assert fast._VINTED_NOT_MENS.search("Max Mara coat IT 56 US 20")
    assert fast._VINTED_NOT_MENS.search("Marina Rinaldi blazer IT 54 US 22")
    assert fast._VINTED_NOT_MENS.search("Escada jacket IT 52/US 20")


def test_zegna_sport_shirt_is_not_the_zegna_sport_line():
    title = ("Ermenegildo Zegna Zegna Sport Shirt Sz L Black White Plaid "
             "Pin Point Twisted Cotton")
    assert not fast._VINTED_ZEGNA_DIFFUSION.search(title)
    assert not fast._VINTED_ZEGNA_DIFFUSION.search("Zegna sport polo M navy")
    assert not fast._VINTED_ZEGNA_DIFFUSION.search("Zegna Sport-Shirts L")
    # The real diffusion lines still match.
    assert fast._VINTED_ZEGNA_DIFFUSION.search("Zegna Sport chinos 34")
    assert fast._VINTED_ZEGNA_DIFFUSION.search("ZEGNA SPORT hoodie L")
    assert fast._VINTED_ZEGNA_DIFFUSION.search("Z Zegna shirt L")


def test_routing_caps_raised_and_lp_kiton_untouched():
    cfg = json.load(open("config.json", encoding="utf-8"))
    caps = {s["id"]: s["max_price"] for s in fast.engine.SAVED_SEARCHES if s.get("id")}
    feeds = {f["query"]: f["max_price"] for f in cfg["VINTED_TEXT_FEEDS"]}
    assert caps["vinted-zegna-cashmere-sweater"] == 235
    assert caps["vinted-zegna-quarter-zip"] == 235
    assert caps["vinted-zegna-polo"] == 120
    assert caps["vinted-zegna"] == 115
    assert caps["vinted-purple-label"] == 200
    assert feeds["zegna"] == 235
    assert feeds["purple label"] == 200
    # Owner 11-20: Loro Piana / Kiton caps raised to $475.
    assert caps["vinted-loro-piana"] == 475 and caps["vinted-kiton"] == 475
    assert feeds["loro piana"] == 475 and feeds["kiton"] == 475
