"""Audit 11-20: negated flaws must not hard-fail; real flaws still must."""

import pytest

import ebay_deal_alert as m

NEGATED = [
    "No holes, tears, or major stains seen in photos",  # $20 Zegna polo shape
    "Light fabric wear and wrinkling visible. No holes, tears, or major stains "
    "seen in photos. Please review",
    "no stains",
    "without stains",
    "free of stains",
    "free of holes or stains",
    "never stained",
    "never been stained",
    "stain-free",
    "zero flaws",
    "No visible holes, tears, or noticeable stains",
    "Free of holes, tears, and major stains",
    # Past _benign_text's 8-word cap: failed before the fix.
    "No holes, tears, rips, snags, pilling, fading, odors, or major stains",
    "No holes, tears, rips, snags, pilling, fading, odors, or obvious stains. Fits well",
]

REAL = [
    "small stain on cuff",
    "stains (see photos)",
    "minor stain, no holes",
    "no holes, minor stain on back",
    "no rips, stain on the cuff",
    "no holes and one small stain",
    "No holes or tears. Small stain on the collar",
    "torn seam",
    # Review 11-20: a later clause naming a located flaw is real.
    "No stains, moth holes on the sleeve",
    "No holes, torn seam on the back",
    "no pilling, moth hole near the cuff",
    "No smells, moth damage on back",
    "no stains, ripped seam at the armpit",
    "No holes, mold spots on the collar",
    "No rips, mildew spots on the lining",
    "No major flaws, moth holes on sleeve",
]

# Owner 11-20: broad seller denials, none may hard-fail.
DENY = [
"No stains or holes", "NO STAINS!!", "no stains/holes/tears", "no rips, stains or holes",
"stains: none", "Stains - none", "stain free", "stain-free", "free from stains",
"without any stains", "no visible stains", "no noticeable stains or wear", "zero stains",
"never stained", "not stained", "w/o stains", "no marks or stains",
"no signs of wear, stains or pilling", "great condition, no stains",
"Excellent condition with no stains", "worn once no stains",
"No flaws (no stains, holes, pilling)", "clean, no stains", "has no stains",
"does not have any stains", "there are no stains", "I don't see any stains",
"no stains that I can see", "no stains or odors", "smoke free, pet free, no stains",
"no fading, stains, or snags", "no pulls/snags/stains", "NWOT no stains", "no stain",
"no staining", "no discoloration or stains", "no pit stains", "no collar stains",
"no stains on collar or cuffs", "stains? none", "condition: no stains",
"No Stains. No Holes. No Tears.", "\u2022 No stains\n\u2022 No holes\n\u2022 No pilling",
"- no stains\n- no holes", "NO STAINS \u2705 NO HOLES \u2705", "no stains \U0001F44D",
"Stains/holes: none", "Stains: N/A", "Flaws: none. Stains: none.",
"No holes, rips or stains", "Like new, no stains or tears, worn twice",
"In great condition with no stains, holes or tears", "There aren't any stains",
"Isn't stained anywhere", "Has never been stained", "Not a single stain",
"no stains whatsoever", "absolutely no stains", "No stains, holes, or signs of wear",
"no moth holes", "no moth damage", "no moths", "moth free", "free of moth holes",
"no peeling", "no peeling or cracking", "logo not peeling", "no cracking or peeling on the print",
"not torn", "no tears or rips", "nothing torn or ripped", "no rips or tears anywhere",
"no mold or mildew", "no water damage", "never had water damage",
"no pilling", "no snags", "no damage", "no flaws", "zero flaws",
"Excellent used condition - no stains, holes or pilling",
"Great shape. No stains. No holes. Smoke-free home.",
"Gently worn, no stains or tears, see photos",
"Worn a handful of times, no stains, no pilling, no holes",
"no holes rips or stains", "no stains, no holes, no smells", "free from holes and stains",
"i didn't notice any stains", "no stains or tears - like new",
"No stains or damage of any kind", "Without stains or holes", "doesnt have any stains",
"Dont see any stains", "can't find any stains", "never worn, no stains",
"No Stains Or Holes Great Condition", "no stains/no holes", "no stains & no holes",
"Condition: excellent, no stains, no holes", "stain and odor free",
]
# Disclosed flaws that must still hard-fail.
REAL_BROAD = [
"small stain on front", "faint stain near hem (see pic)", "has a few stains",
"stains on collar", "pit stains", "no holes but a small stain on back",
"minor stains, otherwise great", "stain on sleeve, no holes", "moth hole on sleeve",
"peeling logo", "logo is peeling", "Small stain on the cuff, barely noticeable",
"One tiny stain on the back (pictured)", "Stain on left sleeve", "There is a stain on the collar",
"has a stain", "slight stain near pocket", "some stains on the inside collar",
"Great condition except for a small stain", "Excellent condition, small stain on front",
"No holes. Small stain on hem.", "few small moth holes", "moth damage on the elbow",
"torn pocket lining", "ripped seam on the side", "mold on collar",
"\u2022 No holes\n\u2022 Small stain on cuff", "Stains: small one on the cuff",
"no holes, one stain on the back", "no rips, stain on the cuff", "no smoke, no pets, one small stain on sleeve",
"Not perfect - small stain", "never washed \u2014 stain on the collar", "not sure if the stain will come out",
"not very noticeable stain on the sleeve", "there are no holes and only one small stain",
"No stains, moth holes on the sleeve", "No holes, torn seam on the back",
"Barely worn but has a stain", "stain (see last photo)", "light stain, no other flaws",
"water damage on the box", "for repair", "sold for parts or not working",
"can not get the stain out", "stain did not come out in the wash", "tried but could not remove the stain",
"nothing wrong except a small stain",
]


@pytest.mark.parametrize("text", DENY)
def test_broad_denials_do_not_hard_fail(text):
    for case in (text, text.lower()):  # callers pass lowercased haystacks
        assert m.condition_hard_fail_keyword("x", case) is None, case


@pytest.mark.parametrize("text", REAL_BROAD)
def test_broad_real_flaws_hard_fail(text):
    # Keywords match lowercase text; every caller lowercases the haystack.
    assert m.condition_hard_fail_keyword("x", text.lower()) is not None, text


def test_reason_names_keyword_and_clause():
    reason = m.condition_hard_fail_reason(
        "x", "peter millar polo. great shape, no holes. small stain on front. ships fast"
    )
    assert reason == (
        "condition hard-fail keyword in title/description: 'stain' in 'small stain on front'"
    )
    long = m.condition_hard_fail_reason("x", "pullover " * 20 + "with a stain " + "word " * 20)
    assert "'stain' in '" in long and len(long.split(" in '", 1)[1]) <= 62
    assert m.condition_hard_fail_reason("x", "no stains or holes") is None


@pytest.mark.parametrize("text", NEGATED)
def test_negated_flaws_do_not_hard_fail(text):
    assert m.condition_hard_fail_keyword("Ermenegildo Zegna polo L", text) is None


@pytest.mark.parametrize("text", REAL)
def test_real_flaws_still_hard_fail(text):
    assert m.condition_hard_fail_keyword("Ermenegildo Zegna polo L", text) is not None


def test_flag_tier_words_still_flag():
    # "hole" / "stained" are not hard-fail keywords (unchanged); "hole" flags.
    assert m.brand_in(m._benign_text("has a hole"), m.CONDITION_FLAG_KEYWORDS)


# Owner 11-20: light damage is a note on Vinted (photo AI judges it); eBay unchanged.
LIGHT_VINTED = [
    "has three tiny stains on the front (see photos) but otherwise great",
    "great condition does have a small stain that has not been treated",
    "a few small stains at the bottom of the pullover",
    "good small stain",
    "small stain on front",
    "one minor little stain on the ankle",
    "faint stain near hem (see pic)",
    "slight paint stain shown",
    "very small stain near the arm",
    "light stain on cuff",
    "barely noticeable stain on back",
    "hard to see stain on the collar",
    "stain on left cuff, barely noticeable",
    "small stain hard to see (see third picture)",
    "minor stains, otherwise great",
    "no holes but a small stain on back",
]
HEAVY_VINTED = [
    "stains on collar",
    "large stain on the front",
    "pit stains",
    "small pit stains",
    "noticeable stain on the back",
    "several stains",
    "several small stains on the front",
    "has a stain",
    "sweat stains. haven't tried cleaning",
    "small stain on front and a large stain on back",
    "small stain and moth holes on sleeve",
    "tiny stain, torn pocket",
    "logo is peeling",
    "stain (see last photo)",
]


@pytest.mark.parametrize("text", LIGHT_VINTED)
def test_light_stain_is_a_vinted_note_not_a_block(text):
    assert m.condition_hard_fail_reason("x", text.lower(), light_stains_ok=True) is None
    assert m.light_stain_note("x", text.lower())
    assert m.condition_hard_fail_reason("x", text.lower()) is not None  # eBay unchanged


@pytest.mark.parametrize("text", HEAVY_VINTED)
def test_unqualified_or_heavy_damage_still_blocks_on_vinted(text):
    assert m.condition_hard_fail_reason("x", text.lower(), light_stains_ok=True) is not None
    assert m.light_stain_note("x", text.lower()) is None


def _listing(platform, desc):
    return {
        "platform": platform, "itemId": "1", "title": "Peter Millar Crown Polo L",
        "price": {"value": "20", "currency": "USD"}, "description": desc,
        "itemWebUrl": "https://x/1",
    }


def test_score_listing_vinted_light_stain_note_vs_ebay_block():
    desc = "great polo. has three tiny stains on the front (see photos)"
    vinted = m.score_listing(_listing("vinted", desc), None, category="knitwear")
    assert "condition hard-fail" not in str(vinted.get("reason"))
    assert "tiny stains" in vinted["vinted_light_damage"]
    ebay = m.score_listing(_listing("ebay", desc), None, category="knitwear")
    assert "condition hard-fail keyword in title/description: 'stains'" in ebay["reason"]
    bad = m.score_listing(_listing("vinted", "stains on collar"), None, category="knitwear")
    assert "condition hard-fail keyword" in bad["reason"]
