"""Regression tests for the highest-risk logic in this bot: the functions
that stand between a scraped listing and a "go spend money" push
notification, plus the dedupe machinery that decides whether a listing
gets a second look.

Every real bug found and fixed during the Aug 2026 audit session gets a
test here, named after what it guards against - so if one of these ever
regresses, it fails LOUD instead of shipping straight to production on a
5-minute cron and being discovered by a bad alert on someone's phone,
which is how every one of these was actually found the first time.

Pure stdlib unittest, no fixtures, no mocking framework - this repo's own
"deliberately plain `requests`, no scraper framework" philosophy (see
platforms.py's module docstring) applies here too. Run with:
    python test_ebay_deal_alert.py
or
    python -m unittest test_ebay_deal_alert -v
"""
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import requests

import ebay_deal_alert as m
import platforms as p


class ComputeDealRating(unittest.TestCase):
    def test_unclamped_discount_reports_true_magnitude(self):
        # Regression: discount_pct used to be clamped to [-1.0, 1.0], so a
        # listing at 2.5x resale value and one at 3x resale value both
        # logged as exactly -100%, destroying real signal. No clamp now.
        rating, discount = m.compute_deal_rating(price=250, estimated_resale_value=100)
        self.assertEqual(rating, "Marginal")
        self.assertLess(discount, -1.0, "a 2.5x-overpriced listing must report past -100%, not floor there")

    def test_rating_buckets(self):
        self.assertEqual(m.compute_deal_rating(30, 100)[0], "Steal")       # 70% under
        self.assertEqual(m.compute_deal_rating(50, 100)[0], "Great Deal")  # 50% under
        self.assertEqual(m.compute_deal_rating(70, 100)[0], "Good Deal")   # 30% under
        self.assertEqual(m.compute_deal_rating(100, 100)[0], "Marginal")   # 0%

    def test_missing_inputs_return_none(self):
        self.assertEqual(m.compute_deal_rating(None, 100), (None, None))
        self.assertEqual(m.compute_deal_rating(50, None), (None, None))
        self.assertEqual(m.compute_deal_rating(50, 0), (None, None))


class BrandMatching(unittest.TestCase):
    def test_whole_word_not_substring(self):
        # Regression: "arrow" (a PASS_BRANDS entry) used to substring-match
        # "Narrow", a standard shoe width - rejecting exactly the Alden/
        # Allen Edmonds listings the shoe searches exist to find.
        self.assertFalse(m.brand_in("allen edmonds 13 c narrow apron toe oxford", ["arrow"]))
        self.assertFalse(m.brand_in("barbour nautical astern quilted jacket", ["nautica"]))

    def test_real_brand_still_matches(self):
        self.assertTrue(m.brand_in("gap crewneck sweater", ["gap"]))
        self.assertTrue(m.brand_in("nautica mens polo", ["nautica"]))

    def test_multiword_brand_matches(self):
        self.assertTrue(m.brand_in("hart schaffner marx suit 42r", ["hart schaffner marx"]))


class CategoryClassification(unittest.TestCase):
    def test_sport_coat_reaches_tailoring_not_outerwear(self):
        # Regression: "jacket"/"coat" (outerwear) was checked before
        # "blazer"/"suit"/"sport coat" (tailoring), so "sport coat"
        # - which contains the substring "coat" - could never reach the
        # tailoring branch at all.
        self.assertEqual(m.classify_search_category("brioni sport coat"), "tailoring")
        self.assertEqual(m.classify_search_category("canali suit jacket"), "tailoring")

    def test_real_outerwear_unaffected(self):
        self.assertEqual(m.classify_search_category("barbour jacket"), "outerwear")
        self.assertEqual(m.classify_search_category("cordings jacket"), "outerwear")

    def test_allen_edmonds_is_footwear(self):
        # Regression: no "shoes"/"loafers" word in this brand-only query,
        # so it fell through to "other" and lost the off-season flag.
        self.assertEqual(m.classify_search_category("allen edmonds"), "footwear")

    def test_watches_and_knitwear(self):
        self.assertEqual(m.classify_search_category("rolex watch"), "watches")
        self.assertEqual(m.classify_search_category("zegna sweater"), "knitwear")


class JacketOnlySuitListing(unittest.TestCase):
    def test_tuxedo_jacket_with_no_pants_is_blocked(self):
        # Regression: "VTG Paul Stuart Tuxedo Jacket ... Union Coat" (no
        # pants) alerted for real - "tuxedo jacket" wasn't in the
        # jacket-only block list, and the title said "Suit" without ever
        # saying pants/trousers/2-piece, so the two-piece override never
        # fired either.
        self.assertTrue(m.is_jacket_only_suit_listing(
            "VTG Paul Stuart Tuxedo Jacket Size 42 Wool Black Suit Made In USA Union Coat"))
        self.assertTrue(m.is_jacket_only_suit_listing("Brioni Dinner Jacket Tuxedo Black 42R"))

    def test_two_piece_suit_always_allowed(self):
        self.assertFalse(m.is_jacket_only_suit_listing(
            "Canali Wool Suit Jacket + Pants 42R"))
        self.assertFalse(m.is_jacket_only_suit_listing(
            "Corbin Suit Mens 42 37x31 Blue Wool 2 Piece"))

    def test_plain_outerwear_never_matches(self):
        # Bare "jacket"/"coat" must never trip this - Barbour etc. is
        # genuine outerwear, not a mislabeled suit component.
        self.assertFalse(m.is_jacket_only_suit_listing("Barbour Bedale Waxed Jacket L"))
        self.assertFalse(m.is_jacket_only_suit_listing("Barbour Bradford Gilet Vest XL"))

    def test_blazer_alone_is_blocked(self):
        self.assertTrue(m.is_jacket_only_suit_listing(
            "Hickey Freeman Mens Blazer Size 42 Reg Blue Worsted Wool Jacket"))


class WatchAuthenticityRedFlags(unittest.TestCase):
    def test_fashion_watch_is_flagged(self):
        # Regression: "Cartier Fashion Watch" ($125 landed) alerted as a
        # 96% "Steal" - the AI described it as a genuine Pasha de Cartier
        # from photos. "Fashion watch" is standard resale terminology for
        # a non-luxury piece styled to resemble a designer one.
        self.assertTrue(m.WATCH_AUTHENTICITY_RED_FLAGS.search("Cartier Fashion Watch"))
        self.assertTrue(m.WATCH_AUTHENTICITY_RED_FLAGS.search("Michael Kors Watch Inspired by Rolex"))
        self.assertTrue(m.WATCH_AUTHENTICITY_RED_FLAGS.search("Replica Watch Cartier Style"))

    def test_genuine_vintage_style_phrasing_survives(self):
        # Deliberately does NOT match bare "style watch" - common,
        # legitimate vintage phrasing that would otherwise cost real
        # listings.
        self.assertFalse(m.WATCH_AUTHENTICITY_RED_FLAGS.search("Vintage 1960s Style Automatic Watch"))
        self.assertFalse(m.WATCH_AUTHENTICITY_RED_FLAGS.search("Art Deco Style Pocket Watch"))
        self.assertFalse(m.WATCH_AUTHENTICITY_RED_FLAGS.search("Rolex Submariner 116610"))


class WatchPriceBand(unittest.TestCase):
    def test_known_brand_returns_band(self):
        band = m.watch_price_band("Movado Museum Quartz Black Dial 40mm")
        self.assertIsNotNone(band)
        low, avg, high = band
        self.assertLess(low, avg)
        self.assertLess(avg, high)

    def test_unknown_brand_returns_none(self):
        self.assertIsNone(m.watch_price_band("Fossil Grant Chronograph 44mm"))

    def test_clamp_catches_the_movado_overestimate(self):
        # Regression: 3 real Movado listings alerted off AI resale guesses
        # of $595-795 against real comps of $150-550 for those exact
        # models. The clamp must actually bring an estimate like that back
        # down to the known band, not just widen the band to fit it.
        band = m.watch_price_band("Movado Bold Evolution 2.0 Chronograph")
        low, avg, high = band
        self.assertLess(high, 795, "band must be tight enough to catch the real live miss")
        self.assertEqual(m.clamp_watch_resale_estimate(795, band), high)

    def test_clamp_never_raises_a_low_estimate(self):
        # Live miss: "Bulova Watch Crystal CMT162 ... Dustproof Envelope" -
        # a watch-crystal storage envelope, not a watch - got an accurate
        # $10 AI estimate (the AI correctly recognized it wasn't a
        # complete watch). The old max(low, min(high, x)) clamp forced
        # that UP to the Bulova band's $60 floor, manufacturing a fake
        # 70% "Steal" that actually sent as a real push alert. The clamp
        # must never raise a below-floor estimate - a low estimate on a
        # "watches" listing is usually the AI correctly flagging that
        # it's an accessory/part, not a real watch.
        band = m.watch_price_band("Bulova Watch Crystal CMT162 Dustproof Envelope")
        low, avg, high = band
        self.assertEqual(low, 60, "sanity check against the real band that produced the live miss")
        self.assertEqual(m.clamp_watch_resale_estimate(10, band), 10, "must stay at the AI's real number, not jump to the floor")


class SizeMatching(unittest.TestCase):
    """The size-normalization step is inlined in run() (ebay_deal_alert.py,
    right after total_price is computed), not its own function - these
    tests exercise the identical regex logic in isolation."""

    def _normalize(self, title):
        return re.sub(r"\b(\d{2})\s?(R|L|S|XL|XS)\b", r"\1 \2", title, flags=re.IGNORECASE)

    def test_42R_matches_bare_42(self):
        # Regression: \b42\b can never match "42R" (R is a word char, no
        # boundary) - the entire suit-size fast lane was throwing away
        # every correctly-sized 2-piece suit as a result. Only 17 suit
        # listings had EVER been scored in the bot's whole history.
        normalized = self._normalize("Kiton Black Pinstripe Suit 42R")
        self.assertTrue(re.search(r"\b42\b", normalized))

    def test_42mm_and_1942_do_not_false_positive(self):
        self.assertFalse(re.search(r"\b42\b", self._normalize("Omega Seamaster Watch 42mm")))
        self.assertFalse(re.search(r"\b42\b", self._normalize("Vintage 1942 Advertisement")))

    def test_shoe_size_13_does_not_match_13_5(self):
        # Regression: "." is a word boundary, so \b13\b matched "13.5" -
        # a Gucci loafer in 13.5 alerted against a size ["13"] search.
        haystack = self._normalize("Gucci Horsebit Loafers Men's 13.5")
        self.assertFalse(re.search(r"\b13\b(?!\.\d)", haystack))
        haystack_real = self._normalize("Alden Cordovan 13 D")
        self.assertTrue(re.search(r"\b13\b(?!\.\d)", haystack_real))


class OversizedDressShirt(unittest.TestCase):
    def test_xl_dress_shirt_is_flagged(self):
        self.assertTrue(m.is_oversized_dress_shirt("Charvet Dress Shirt Mens XL French Cuff"))
        self.assertTrue(m.is_oversized_dress_shirt("Ralph Lauren Purple Label Button Down Shirt XL"))

    def test_xl_knitwear_and_outerwear_untouched(self):
        # Standing rule: user is L in long-sleeve dress shirts but
        # genuinely XL in knitwear/outerwear - must never conflate the two.
        for title in (
            "Zegna Cashmere Sweater XL",
            "Peter Millar Quarter Zip XL",
            "Barbour Waxed Jacket XL",
            "Loro Piana Cashmere Cardigan XXL",
        ):
            self.assertFalse(m.is_oversized_dress_shirt(title), title)


class ListingFingerprint(unittest.TestCase):
    def test_cross_platform_same_seller_matches(self):
        # Regression: make_listing() prefixes seller_username with
        # "platform:" for non-eBay marketplaces, and that prefix used to
        # leak into the relist fingerprint - so the same reseller
        # cross-posting one item to Poshmark and then eBay/Vinted under
        # the same handle hashed differently per platform and alerted
        # twice.
        ebay_listing = {"title": "Canali Wool Suit 42R", "seller": {"username": "izzysvintage"}}
        poshmark_listing = {"title": "Canali Wool Suit 42R", "seller": {"username": "poshmark:izzysvintage"}}
        vinted_listing = {"title": "Canali Wool Suit 42R", "seller": {"username": "vinted:izzysvintage"}}
        fps = {m.listing_fingerprint(l) for l in (ebay_listing, poshmark_listing, vinted_listing)}
        self.assertEqual(len(fps), 1, "same seller+title must fingerprint identically across platforms")

    def test_different_seller_does_not_collide(self):
        a = {"title": "Canali Wool Suit 42R", "seller": {"username": "poshmark:seller_a"}}
        b = {"title": "Canali Wool Suit 42R", "seller": {"username": "poshmark:seller_b"}}
        self.assertNotEqual(m.listing_fingerprint(a), m.listing_fingerprint(b))

    def test_no_seller_returns_none(self):
        self.assertIsNone(m.listing_fingerprint({"title": "No Seller Listing"}))


class MarkSeenFingerprintTiming(unittest.TestCase):
    """The core bug: fingerprints written at COLLECTION time (before a
    listing ever reached a real verdict) instead of at final-disposition
    time. A listing that hit MAX_ALERTS_PER_RUN mid-run was already
    fingerprinted at that price, so on the very next run - same listing,
    same price - it collided with its OWN fingerprint and was silently
    dropped as "a relist at the same price", never actually retried."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(f"{self.tmpdir}/test.db")
        self.conn.execute("CREATE TABLE seen (item_id TEXT PRIMARY KEY, seen_at TEXT)")
        self.conn.execute(
            "CREATE TABLE fingerprints (fingerprint TEXT PRIMARY KEY, best_price REAL, seen_at TEXT)"
        )

    def test_fingerprint_not_written_without_mark_seen(self):
        # Merely computing a fingerprint (as PASS 1 does for every listing,
        # to check against past relists) must NOT write anything - only
        # mark_seen() at a real final disposition should.
        fingerprint = "abc123"
        best_price = m.get_fingerprint_best_price(self.conn, fingerprint)
        self.assertIsNone(best_price, "no write should have happened yet")

    def test_mark_seen_writes_both_atomically(self):
        m.mark_seen(self.conn, "item1", "fp1", 50.0)
        self.assertFalse(m.is_new(self.conn, "item1"))
        self.assertEqual(m.get_fingerprint_best_price(self.conn, "fp1"), 50.0)

    def test_mark_seen_without_fingerprint_only_marks_item(self):
        # Items without a resolvable seller (fingerprint=None) must still
        # get is_new()-deduped without touching the fingerprints table.
        m.mark_seen(self.conn, "item2", None, None)
        self.assertFalse(m.is_new(self.conn, "item2"))

    def test_upsert_fingerprint_only_lowers_best_price(self):
        m.upsert_fingerprint(self.conn, "fp1", 100.0)
        m.upsert_fingerprint(self.conn, "fp1", 150.0)  # higher - must NOT overwrite
        self.assertEqual(m.get_fingerprint_best_price(self.conn, "fp1"), 100.0)
        m.upsert_fingerprint(self.conn, "fp1", 80.0)  # lower - must overwrite
        self.assertEqual(m.get_fingerprint_best_price(self.conn, "fp1"), 80.0)

    def tearDown(self):
        self.conn.close()


class ScoreListingHardFails(unittest.TestCase):
    def _listing(self, title, price=50.0):
        return {"title": title, "price": {"value": price, "currency": "USD"}, "itemId": "t1"}

    def test_gender_keyword_blocks(self):
        result = m.score_listing(self._listing("Women's Ralph Lauren Sweater M"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("gender", result["reason"])

    def test_womens_size_crossref_blocks_even_with_no_gender_word(self):
        # Live miss: "Brunello Cuccinelli Water-Resistant Jacket | Size 46
        # (US 10)" alerted as a 59% "Great Deal" - no gender word anywhere
        # in the title (GENDER_EXCLUDE_KEYWORDS had nothing to match), but
        # "(US 10)" is exactly how European designer women's ready-to-wear
        # cross-references its size tag.
        result = m.score_listing(
            self._listing("Brunello Cuccinelli Water-Resistant Jacket | Size 46 (US 10)"), gap_report=None
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("women's size", result["reason"])

    def test_womens_size_crossref_does_not_false_positive_on_mens_shoe_size(self):
        # Must not fire on the extremely common men's-shoe phrasing this
        # bot's shoe searches are full of - "US 10" bare, not inside an
        # "EU size (US N)" parenthetical cross-reference.
        self.assertFalse(m.WOMENS_SIZE_CROSSREF_SIGNAL.search("alden cap toe boot us 10 d"))
        self.assertFalse(m.WOMENS_SIZE_CROSSREF_SIGNAL.search("allen edmonds park avenue size 10 us"))

    def test_pass_brand_blocks(self):
        # "travismathew" is a real PASS_BRANDS entry (mall-tier golf brand).
        result = m.score_listing(self._listing("TravisMathew Golf Polo Shirt M"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("pass list", result["reason"])

    def test_grab_on_sight_brand_produces_review_verdict(self):
        # "canali" is a real GRAB_ON_SIGHT_BRANDS entry.
        result = m.score_listing(
            self._listing("Canali Wool 2-Piece Suit Jacket + Pants 42R", price=80.0),
            gap_report=None,
        )
        self.assertEqual(result["verdict"], "REVIEW")
        self.assertEqual(result["brand_tier"], "grab_on_sight")

    def test_pet_product_blocks(self):
        # Live miss: "Barbour waxed dog jacket" ($20 landed) alerted as a
        # 54% "Great Deal" - it's a pet product, not menswear, and "dog" was
        # right there in the title the whole time.
        result = m.score_listing(self._listing("Barbour Waxed Dog Jacket XL"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("pet product", result["reason"])

    def test_pet_product_does_not_false_positive_on_petite_or_cat_brand(self):
        # PET_PRODUCT_SIGNALS is whole-word matched on purpose - it
        # deliberately does NOT include bare "pet" or "cat": "pet"
        # substring-hits "petite", and "cat" is a real workwear brand
        # (Caterpillar/"CAT boots").
        self.assertFalse(m.PET_PRODUCT_SIGNALS.search("ralph lauren petite sweater m"))
        self.assertFalse(m.PET_PRODUCT_SIGNALS.search("cat caterpillar steel toe boots 10"))

    def test_stainless_steel_does_not_false_positive_as_condition_hard_fail(self):
        # Live miss found auditing a night's alerts_log.jsonl: "stain" is a
        # CONDITION_HARD_FAIL_KEYWORDS entry (meant to catch "stain"/
        # "stains" damage callouts) and was raw-substring-matched, so
        # "Stainless Steel" - present in the overwhelming majority of real
        # watch titles - silently PASS'd dozens of perfectly good watches
        # every run under the misleading fixed reason string "moth/hole
        # keyword in title". Confirmed real titles that were wrongly killed:
        # "Seiko Kinetic 5M42-0K09 Two Tone Stainless Steel Watch White Dial
        # 97.4g" and "Bulova 96X003 Stainless Steel Bangle Watch".
        result = m.score_listing(
            self._listing("Seiko Kinetic Two Tone Stainless Steel Watch White Dial"), gap_report=None
        )
        self.assertNotEqual(result["verdict"], "PASS")

    def test_dustbag_envelope_hard_fails_before_reaching_ai(self):
        # Belt-and-suspenders companion to the clamp fix - catches this
        # class of accessory listing at the title stage so it never even
        # burns an AI call, not just relying on the clamp to defang a
        # false-positive result after the fact.
        result = m.score_listing(
            self._listing("Bulova Watch Crystal CMT162 12.0 x 11.7 G-S Dustproof Envelope"),
            gap_report=None,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("condition hard-fail keyword", result["reason"])

    def test_watch_parts_accessory_hard_fails_before_reaching_ai(self):
        # Live miss: "Authentic Hamilton Watch Service Case Black Zip Around
        # w Insert & Watch Parts" - a watch service case/accessory bag, not
        # a complete watch - had no CONDITION_HARD_FAIL_KEYWORDS match at
        # all ("watch parts" wasn't in the list, only "for parts"/"for
        # repair" and specific container phrases), so it sailed through to
        # the AI price check, got priced as if it were a real watch, and
        # was blocked only for being "Good Deal" instead of "Steal" - a
        # correct block, but for a misleading reason that made it look like
        # a real watch that just wasn't a good enough deal.
        result = m.score_listing(
            self._listing("Authentic Hamilton Watch Service Case Black Zip Around w Insert & Watch Parts"),
            gap_report=None,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("condition hard-fail keyword", result["reason"])

    def test_real_condition_hard_fails_still_caught(self):
        # The whole-word fix must not lose real catches - these should
        # still hard-fail same as before.
        for title in (
            "Vintage Watch For Parts Or Repair",
            "Seiko Chronograph Watch For Repair",
            "Vintage Movado Watch Movement Gold Dial Untested",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertEqual(result["verdict"], "PASS", title)
            self.assertIn("condition hard-fail keyword", result["reason"])

    def test_classic_product_line_does_not_false_positive_as_corporate_logo(self):
        # Live miss: "Bulova Classic Blue Men's Watch - 96B334" hard-failed
        # as "corporate logo keyword match" off bare "classic" in
        # CORPORATE_LOGO_KEYWORDS. Unlike the "stain"/"stainless" bug this
        # wasn't a substring-match issue - "classic" really is a standalone
        # word there - it's just too generic a keyword: "Classic" is a real
        # Bulova/Movado/Seiko product-line name, not a corporate-event logo
        # signal like "tournament" or "invitational". Removed from
        # CORPORATE_LOGO_KEYWORDS entirely.
        result = m.score_listing(self._listing("Bulova Classic Blue Men's Watch - 96B334"), gap_report=None)
        self.assertNotEqual(result["verdict"], "PASS")

    def test_real_corporate_logo_still_caught(self):
        result = m.score_listing(self._listing("Peter Millar Pebble Beach Golf Tournament Polo M"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("corporate logo", result["reason"])


class StealQualityGate(unittest.TestCase):
    def test_watches_never_blind_trust(self):
        # No deal_rating at all (AI budget didn't reach it) must ALWAYS
        # block a watch, even on a grab_on_sight brand - counterfeit risk,
        # not a price question.
        result = {"deal_rating": None, "brand_tier": "grab_on_sight"}
        reason = m.is_blocked_by_steal_quality_gate(result, category="watches")
        self.assertIsNotNone(reason)
        self.assertIn("never blind-trust", reason)

    def test_watches_require_steal_or_great_deal(self):
        result = {"deal_rating": "Good Deal", "discount_pct": 35, "brand_tier": "grab_on_sight"}
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="watches"))
        result["deal_rating"] = "Great Deal"
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="watches"))

    def test_knitwear_requires_grab_on_sight_and_steal(self):
        result = {"deal_rating": "Steal", "discount_pct": 75, "brand_tier": "standard"}
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))
        result["brand_tier"] = "grab_on_sight"
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))

    def _gamecocks_result(self, title, **kwargs):
        result = {"listing": {"title": title}, "search_query": "peter millar gamecocks quarter zip"}
        result.update(kwargs)
        return result

    def test_gamecocks_bar_requires_ai_check_to_have_run(self):
        # Live miss: a generic Peter Millar plaid shirt (no Gamecocks
        # branding at all) alerted with NO AI check having run - the
        # original "doesn't require an AI check" design meant nothing ever
        # looked at its photos for a corporate logo, which is exactly what
        # the user then reported ("bad peter millar alerts...with logos").
        # Must now fall back to the same "no AI price estimate and brand
        # not grab_on_sight-tier" rule every other scoped bar uses.
        result = self._gamecocks_result("Peter Millar Gamecocks Quarter Zip", deal_rating=None, brand_tier="standard")
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))
        result["brand_tier"] = "grab_on_sight"
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))

    def test_gamecocks_bar_requires_at_least_good_deal(self):
        result = self._gamecocks_result(
            "Peter Millar Gamecocks Polo", deal_rating="Fair", discount_pct=15, brand_tier="standard"
        )
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="other"))
        result["deal_rating"] = "Good Deal"
        result["discount_pct"] = 35
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_gamecocks_bar_is_scoped_to_peter_millar_not_all_usc(self):
        # Explicit correction: "the above is for PETER MILLAR USC, not just
        # all usc" - a bare "gamecocks" search (no "peter millar") must not
        # get the loose bar.
        result = self._gamecocks_result("Gamecocks Polo", deal_rating=None, brand_tier="standard")
        result["search_query"] = "gamecocks polo"
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_gamecocks_bar_does_not_apply_to_off_target_matches(self):
        # Live miss: "peter millar gamecocks jacket" alerted a Stanford
        # quarter-zip and generic plaid PM shirts with zero South Carolina
        # connection - is_relevant_marketplace_listing() only requires ONE
        # non-stopword query token in the title (satisfied by "peter"/
        # "millar" alone), so the saved search's query string matching
        # "gamecocks" isn't enough on its own. The listing's own title must
        # say "gamecocks" or "south carolina" - anything else falls through
        # to normal (stricter) treatment instead of the loose bar.
        for title in ("Peter Millar Quarter Zip Stanford Men's L", "Peter Millar Shirt Plaid Check Button Down"):
            result = self._gamecocks_result(title, deal_rating=None, brand_tier="standard")
            reason = m.is_blocked_by_steal_quality_gate(result, category="knitwear")
            self.assertIsNotNone(reason)
            self.assertNotIn("gamecocks bar", reason, f"{title!r} must not get the loose gamecocks bar")

    def test_loro_piana_cucinelli_bar_requires_steal_tier(self):
        # Live miss: a $200 Brunello Cucinelli jacket alerted as a
        # well-evidenced 59% "Great Deal" (real Grailed sold comps) - not
        # what "still has to be a steal" meant. Scoped to these two
        # brands' searches specifically, not the whole knitwear category -
        # "brunello cucinelli jacket" doesn't trigger the knitwear
        # classifier at all.
        result = {"deal_rating": "Great Deal", "discount_pct": 59, "price_confidence": "high", "search_query": "brunello cucinelli jacket"}
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertIn("below Steal", reason)
        result["deal_rating"] = "Steal"
        result["discount_pct"] = 72
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))
        result2 = {"deal_rating": "Steal", "discount_pct": 75, "price_confidence": "high", "search_query": "loro piana suit"}
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result2, category="other"))

    def test_default_category_no_ai_data_blind_trusts_grab_on_sight_only(self):
        result = {"deal_rating": None, "brand_tier": "grab_on_sight"}
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))
        result["brand_tier"] = "standard"
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_all_never_got_ai_check_reasons_share_the_no_ai_price_substring(self):
        # run() decides whether to mark_seen() a gate-blocked candidate by
        # checking "no AI price" in gate_reason - a candidate the AI never
        # actually evaluated (GEMINI_CALL_LIMIT exhausted) must be left
        # unseen so it gets a real shot on a later run, vs. one the AI DID
        # evaluate and reject, which should never be reconsidered. Live
        # miss: Vinted alone surfaces 5,000-6,500 listings/run against an
        # 8-call AI budget, so most "watches" candidates were hitting
        # exactly this path and then getting permanently thrown away
        # before ever being evaluated once - user report: "vinted watches
        # ... sell almost instantly before I could even do any research."
        # This test locks in the substring every "never evaluated" reason
        # must contain, across all 4 gate variants, so a future wording
        # change can't silently break that dispatch.
        never_evaluated = {
            "watches": {"deal_rating": None, "brand_tier": "standard"},
            "suit": {"deal_rating": None, "brand_tier": "standard"},
            "other": {"deal_rating": None, "brand_tier": "standard"},
        }
        for category, result in never_evaluated.items():
            reason = m.is_blocked_by_steal_quality_gate(result, category=category)
            self.assertIn("no AI price", reason, f"category={category}")
        crown_crafted_result = {"deal_rating": None, "brand_tier": "standard", "search_query": "peter millar crown crafted polo"}
        reason = m.is_blocked_by_steal_quality_gate(crown_crafted_result, category="other")
        self.assertIn("no AI price", reason)

    def test_ai_evaluated_rejections_do_not_share_the_no_ai_price_substring(self):
        # The flip side of the above - an AI-evaluated-and-rejected
        # candidate must NOT match "no AI price", or run() would wrongly
        # leave it unseen and re-attempt it forever.
        evaluated_reject = {"deal_rating": "Good Deal", "discount_pct": 35, "brand_tier": "grab_on_sight"}
        reason = m.is_blocked_by_steal_quality_gate(evaluated_reject, category="watches")
        self.assertNotIn("no AI price", reason)


class MarketplaceQueryExclusions(unittest.TestCase):
    def test_exclusions_stripped_from_search_query(self):
        # Regression: "-term" is eBay-only search syntax. Every other
        # marketplace adapter was passing it straight through, so the
        # literal "-radio -canteen -mug" text was fed to their relevance
        # matching AS SEARCH TERMS, and the exclusions themselves did
        # nothing.
        clean, terms = p.split_query_exclusions("zenith watch -tv -radio -canteen -mug")
        self.assertEqual(clean, "zenith watch")
        self.assertEqual(set(terms), {"tv", "radio", "canteen", "mug"})

    def test_excluded_term_matches_title(self):
        self.assertTrue(p.title_matches_exclusion("Zenith radio", ["radio"]))
        self.assertTrue(p.title_matches_exclusion("Tudor watch brand canteen", ["canteen"]))

    def test_whole_word_exclusion_does_not_over_match(self):
        # "-hat" must not kill "Thatcher".
        self.assertFalse(p.title_matches_exclusion("Thatcher Wool Coat", ["hat"]))

    def test_garment_word_gate(self):
        # Regression: sold-comp data was being applied to brand-only
        # queries with no garment word ('ralph lauren "purple label"'),
        # blending comps across every garment type the brand makes (ties,
        # sweaters, suits) and applying that blended median to whatever
        # specific garment matched - e.g. a $120 median (dominated by
        # cashmere sweaters and a $420 suit) got forced onto a basic tee.
        self.assertFalse(m.GARMENT_TYPE_WORDS.search('ralph lauren "purple label"'))
        self.assertFalse(m.GARMENT_TYPE_WORDS.search("allen edmonds"))
        self.assertTrue(m.GARMENT_TYPE_WORDS.search("zegna sweater"))
        self.assertTrue(m.GARMENT_TYPE_WORDS.search("loro piana suit"))


class EbayRateLimitCheck(unittest.TestCase):
    """Previously the bot had NO visibility into its own eBay quota - it
    found the wall by 429ing into it repeatedly, which is what turned a
    busy day into a 13.5h outage (Aug 9). getRateLimits asks eBay directly
    instead."""

    def test_parses_real_response_shape(self):
        # This is the ACTUAL body eBay returns, captured live in production
        # - apiName is "Browse" (capital B, not the lowercase "browse" every
        # doc/writeup shows), and there are TWO resources under it:
        # buy.browse (what this bot calls) and buy.browse.item.bulk (a
        # different endpoint it never uses, with its own independent
        # remaining/limit). Both were real bugs caught only by deploying
        # and reading the live log - a body built from the documented shape
        # alone would have hidden both.
        body = {
            "rateLimits": [{
                "apiContext": "buy", "apiName": "Browse", "apiVersion": "v1",
                "resources": [
                    {"name": "buy.browse", "rates": [
                        {"count": 1170, "limit": 5000, "remaining": 3830,
                         "reset": "2026-08-14T07:00:00.000Z", "timeWindow": 86400},
                    ]},
                    {"name": "buy.browse.item.bulk", "rates": [
                        {"count": 0, "limit": 5000, "remaining": 5000,
                         "reset": "2026-08-14T07:00:00.000Z", "timeWindow": 86400},
                    ]},
                ],
            }],
        }
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: body
        with mock.patch("requests.get", return_value=fake_resp):
            remaining, limit = m.get_ebay_rate_limit_remaining("fake-token")
        self.assertEqual((remaining, limit), (3830, 5000), "must read buy.browse, not buy.browse.item.bulk")

    def test_failure_returns_none_never_raises(self):
        # Must be a safety net, not a hard dependency - a failed quota
        # check should never be able to block or crash a real run.
        with mock.patch("requests.get", side_effect=requests.exceptions.ConnectionError("boom")):
            remaining, limit = m.get_ebay_rate_limit_remaining("fake-token")
        self.assertEqual((remaining, limit), (None, None))


class AsciiSafeHeader(unittest.TestCase):
    """Live miss: a genuine 72%-under-resale "Steal" (Allen Edmonds
    LaSalle, size 13) sat completely unsent for 6+ hours because its
    title's apostrophe was a curly U+2019 from "Men's", and
    UnicodeEncodeError building the ntfy Title header isn't a
    requests.exceptions.RequestException - send_alert()'s retry loop
    never caught it, so it failed identically on every 5-min run without
    ever getting marked seen."""

    def test_curly_apostrophe_becomes_straight_and_stays_ascii(self):
        title = "Allen Edmonds LaSalle Brown Leather Derby Dress Shoes Men’s US 13 Round Toe"
        safe = m._ascii_safe_header(f"[EBAY] {title[:60]}")
        safe.encode("ascii")  # must not raise - this is the actual live crash
        self.assertIn("Men's", safe)

    def test_common_smart_punctuation_translated_not_just_stripped(self):
        safe = m._ascii_safe_header("‘quoted’ – em—dash …")
        self.assertEqual(safe, "'quoted' - em-dash ...")

    def test_unknown_non_ascii_stripped_rather_than_crashing(self):
        safe = m._ascii_safe_header("Café Watch \U0001f600")
        safe.encode("ascii")  # must not raise
        self.assertIn("Watch", safe)


class ShopGoodwillClosingSoon(unittest.TestCase):
    """currentPrice on a live ShopGoodwill auction isn't a real number until
    bidding is basically over - per explicit user instruction, only surface
    an auction once it's close enough to closing that the current price is
    close to final. Contested auctions (numBids > 0) get an even tighter
    window - a bid war is the strongest signal the price isn't done moving."""

    def _fake_response(self, items):
        resp = mock.Mock()
        resp.ok = True
        resp.json.return_value = {"searchResults": {"items": items, "itemCount": len(items)}}
        return resp

    def _item(self, item_id, remaining, num_bids=0):
        return {
            "itemId": item_id, "title": f"item {item_id}", "currentPrice": 20.0,
            "remainingTime": remaining, "numBids": num_bids,
            "imageURL": "http://x/img.jpg", "sellerName": "seller",
        }

    def test_uncontested_uses_wide_window_contested_uses_narrow_window(self):
        items = [
            self._item(1, "45m", num_bids=0),   # too early, uncontested (>30)
            self._item(2, "20m", num_bids=0),   # in window, uncontested
            self._item(3, "20m", num_bids=3),   # too early once contested (>15)
            self._item(4, "10m", num_bids=3),   # in window, contested
        ]
        with mock.patch.object(p.requests, "post", return_value=self._fake_response(items)):
            listings, _count = p.search_shopgoodwill({"query": "test watch"})
        surfaced_ids = {int(l["itemId"].split(":")[1]) for l in listings}
        self.assertEqual(surfaced_ids, {2, 4})


class GrailedBatching(unittest.TestCase):
    """172 sequential Algolia calls (86 enabled searches x 2 queries each)
    ate ~60s of the ~90s marketplace fetch budget - Grailed alone was
    eating two-thirds of it, which is why prefetch_marketplaces() needed a
    rotating start-offset just to spread which searches got cut off each
    run, rather than ever covering all of them. Batching replaces that with
    1-4 HTTP round trips total."""

    def test_batch_adapter_called_once_not_per_search(self):
        from datetime import datetime, timezone

        calls = []

        def fake_grailed_batch(saved_searches):
            calls.append(len(saved_searches))
            return {
                s["query"]: [{"itemId": f"grailed:{s['query']}", "title": s["query"], "seller": {}}]
                for s in saved_searches
            }

        def grailed_must_not_be_called_per_search(_saved_search):
            raise AssertionError("grailed must be dispatched via BATCH_ADAPTERS, not the per-task queue")

        orig_batch, orig_adapters = dict(p.BATCH_ADAPTERS), dict(p.ADAPTERS)
        orig_searches, orig_enabled = m.SAVED_SEARCHES, m.MARKETPLACES_ENABLED
        try:
            p.BATCH_ADAPTERS.clear()
            p.BATCH_ADAPTERS["grailed"] = fake_grailed_batch
            p.ADAPTERS.clear()
            p.ADAPTERS["grailed"] = grailed_must_not_be_called_per_search
            p.ADAPTERS["poshmark"] = lambda s: (
                [{"itemId": f"poshmark:{s['query']}", "title": s["query"], "seller": {}}], 1
            )
            m.MARKETPLACES_ENABLED = ["grailed", "poshmark"]
            m.SAVED_SEARCHES = [
                {"query": "canali suit", "enabled": True, "platforms": ["grailed", "poshmark"]},
                {"query": "zegna sweater", "enabled": True, "platforms": ["grailed", "poshmark"]},
            ]
            result = m.prefetch_marketplaces(datetime.now(timezone.utc))
        finally:
            p.BATCH_ADAPTERS.clear()
            p.BATCH_ADAPTERS.update(orig_batch)
            p.ADAPTERS.clear()
            p.ADAPTERS.update(orig_adapters)
            m.SAVED_SEARCHES = orig_searches
            m.MARKETPLACES_ENABLED = orig_enabled

        self.assertEqual(calls, [2], "expected exactly one batch call covering both searches")
        for query in ("canali suit", "zegna sweater"):
            ids = {l["itemId"] for l in result.get(query, [])}
            self.assertIn(f"grailed:{query}", ids)
            self.assertIn(f"poshmark:{query}", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
