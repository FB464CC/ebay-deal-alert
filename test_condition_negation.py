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


@pytest.mark.parametrize("text", NEGATED)
def test_negated_flaws_do_not_hard_fail(text):
    assert m.condition_hard_fail_keyword("Ermenegildo Zegna polo L", text) is None


@pytest.mark.parametrize("text", REAL)
def test_real_flaws_still_hard_fail(text):
    assert m.condition_hard_fail_keyword("Ermenegildo Zegna polo L", text) is not None


def test_flag_tier_words_still_flag():
    # "hole" / "stained" are not hard-fail keywords (unchanged); "hole" flags.
    assert m.brand_in(m._benign_text("has a hole"), m.CONDITION_FLAG_KEYWORDS)
