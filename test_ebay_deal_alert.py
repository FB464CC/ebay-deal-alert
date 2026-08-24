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
import json
import pathlib
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_golf_clubs_not_swallowed_by_apparel_golf_branch(self):
        # Regression risk: the apparel branch matches any query containing
        # "golf" (e.g. "peter millar golf polo"), which would swallow a
        # real clubs/equipment query into the clothing category+prompt+gate
        # if checked first.
        self.assertEqual(m.classify_search_category("golf club set"), "golf-equipment")
        self.assertEqual(m.classify_search_category("complete golf iron set"), "golf-equipment")
        self.assertEqual(m.classify_search_category("peter millar golf polo"), "golf")


class GolfEquipmentGate(unittest.TestCase):
    def _result(self, price=200, **golf_fields):
        result = {"price": price, "search_query": "golf club set", "listing": {"title": "Golf Set"}}
        result.update(golf_fields)
        return result

    def test_no_ai_check_yet_is_retry_eligible(self):
        reason = m.is_blocked_by_steal_quality_gate(self._result(), category="golf-equipment")
        self.assertIn("no AI price", reason)

    def test_over_price_cap_is_permanently_blocked(self):
        reason = m.is_blocked_by_steal_quality_gate(
            self._result(price=300, golf_ai_checked=True, golf_is_complete_set=True,
                          golf_is_starter_kit=False, damage_found=False),
            category="golf-equipment",
        )
        self.assertIsNotNone(reason)
        self.assertNotIn("no AI price", reason)  # permanent, not retry-eligible

    def test_starter_kit_is_blocked(self):
        reason = m.is_blocked_by_steal_quality_gate(
            self._result(golf_ai_checked=True, golf_is_complete_set=True,
                          golf_is_starter_kit=True, damage_found=False),
            category="golf-equipment",
        )
        self.assertIsNotNone(reason)

    def test_incomplete_set_is_blocked(self):
        reason = m.is_blocked_by_steal_quality_gate(
            self._result(golf_ai_checked=True, golf_is_complete_set=False,
                          golf_is_starter_kit=False, damage_found=False),
            category="golf-equipment",
        )
        self.assertIsNotNone(reason)

    def test_complete_quality_set_under_cap_clears_the_gate(self):
        reason = m.is_blocked_by_steal_quality_gate(
            self._result(price=250, golf_ai_checked=True, golf_is_complete_set=True,
                          golf_is_starter_kit=False, damage_found=False),
            category="golf-equipment",
        )
        self.assertIsNone(reason)


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

    def test_bare_jacket_word_blocked_only_when_query_was_a_suit_search(self):
        # Real live miss: "Ermenegildo Zegna ... Soft 100% Silk Jacket Blue
        # Check Jacket" alerted from the "ermenegildo zegna suit" search -
        # bare "jacket" is deliberately never flagged on its own (see
        # test_plain_outerwear_never_matches), but a SUIT search returning
        # a title that never once says pants/trousers/2-piece is a
        # mismatched blazer, not a real suit.
        title = "Ermenegildo Zegna Italy EU 52R/US 42R Soft 100% Silk Jacket Blue Check Jacket"
        self.assertTrue(m.is_jacket_only_suit_listing(title, "ermenegildo zegna suit"))
        # No query, or a query that isn't suit-worded, must NOT flag it -
        # this is exactly what keeps dedicated jacket searches (e.g. "loro
        # piana jacket", added this session) working correctly.
        self.assertFalse(m.is_jacket_only_suit_listing(title))
        self.assertFalse(m.is_jacket_only_suit_listing(title, "loro piana jacket"))

    def test_bare_jacket_word_still_allowed_when_suit_search_has_pants(self):
        # A genuine 2-piece suit from a suit-worded search must still pass -
        # the new bare-word check only fires when NO pants signal exists at
        # all, same as every other branch of this function.
        self.assertFalse(m.is_jacket_only_suit_listing(
            "Ermenegildo Zegna Suit Jacket and Pants 42R", "ermenegildo zegna suit"))

    def test_description_disclaimer_blocks_a_complete_looking_suit_title(self):
        # Explicit user report: "i keep getting suit jackets that dont have
        # the full suits, just the jackets...maybe read the descriptions for
        # pants/trouser." Confirmed live: these titles pass EVERY title-only
        # check (they say "Suit" but never "pants"/"2-piece", and carry no
        # blazer/sport-coat/bare-jacket word), so the seller's disclaimer in
        # the description was the only available signal.
        title = "Canali Wool Suit 42R Navy"
        for description in (
            "Jacket only, pants not included.",
            "Blazer only. No pants.",
            "Canali suit jacket - pants sold separately",
            "Beautiful suit, however the trousers are not included",
            "Does not come with pants",
            "Missing the pants unfortunately",
            "Top only, no matching trousers included",
        ):
            self.assertTrue(
                m.is_jacket_only_suit_listing(title, "canali suit", description), description
            )

    def test_description_check_does_not_overblock_real_two_piece_suits(self):
        # "No pants POCKETS damage" on a genuine complete suit was a real
        # false positive caught in testing before shipping - a bare \b after
        # "no pants" matched it. The pattern is clause-boundary anchored now.
        title = "Canali Wool Suit 42R Navy"
        for description in (
            "Two piece suit, jacket and pants both included. Excellent condition.",
            "Suit with pants, 42R jacket 34W trousers. No pants pockets damage.",
            "Full suit. Pants hemmed. Smoke free home.",
            "Navy wool suit, includes trousers, minor wear",
            "Jacket and pants, no pants alterations needed",
        ):
            self.assertFalse(
                m.is_jacket_only_suit_listing(title, "canali suit", description), description
            )


class EmptyPackagingSignals(unittest.TestCase):
    def test_single_word_dustbag_only_is_caught(self):
        # Regression: dust\s+bag (requires a space) never matched the
        # extremely common single-word "Dustbag" spelling at all - only
        # the spaced "dust bag only" phrasing did. Real risk: a listing
        # like "Louis Vuitton Dustbag Only" (packaging, not the actual
        # bag) alerting as if it were the genuine item.
        self.assertTrue(m.EMPTY_PACKAGING_SIGNALS.search("Louis Vuitton Dustbag Only"))
        self.assertTrue(m.EMPTY_PACKAGING_SIGNALS.search("Authentic LV Dustbag Only, No Item"))
        self.assertTrue(m.EMPTY_PACKAGING_SIGNALS.search("Gucci dust bag only"))

    def test_genuine_item_with_dustbag_included_survives(self):
        # A real item that happens to come WITH its dust bag as an
        # accessory must never false-positive - only "only" phrasing means
        # the packaging is all that's for sale.
        self.assertFalse(m.EMPTY_PACKAGING_SIGNALS.search(
            "Louis Vuitton Neverfull with box and dustbag"))
        self.assertFalse(m.EMPTY_PACKAGING_SIGNALS.search(
            "Genuine LV bag with original dustbag included"))


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


class WatchLotSignals(unittest.TestCase):
    """Real live miss: "Bulova Emporio Armani Citizen Skagen Dress Watch
    Lot" (4 different brands bundled) alerted as a 65% "Great Deal" off a
    $600 AI "retail" estimate for the whole lot - the watch-pricing
    methodology has no coherent meaning applied to a grab-bag of unrelated
    watches, authenticity/condition unverifiable per-item."""

    def test_lot_listings_flagged(self):
        self.assertTrue(m.WATCH_LOT_SIGNALS.search("Bulova Emporio Armani Citizen Skagen Dress Watch Lot"))
        self.assertTrue(m.WATCH_LOT_SIGNALS.search("Lot of 5 Vintage Watches for Parts or Repair"))
        self.assertTrue(m.WATCH_LOT_SIGNALS.search("5 Piece Watch Lot Seiko Bulova Timex"))
        self.assertTrue(m.WATCH_LOT_SIGNALS.search("Assorted Watches Untested"))

    def test_single_watch_listings_untouched(self):
        self.assertFalse(m.WATCH_LOT_SIGNALS.search("Rolex Submariner 116610 Automatic"))
        self.assertFalse(m.WATCH_LOT_SIGNALS.search("Vintage Omega Seamaster Automatic Watch"))


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


class OversizedFittedShirt(unittest.TestCase):
    def test_xl_dress_shirt_is_flagged(self):
        self.assertTrue(m.is_oversized_fitted_shirt("Charvet Dress Shirt Mens XL French Cuff"))
        self.assertTrue(m.is_oversized_fitted_shirt("Ralph Lauren Purple Label Button Down Shirt XL"))

    def test_xl_polo_is_flagged(self):
        # Real live miss: this exact title alerted before polo was added
        # to FITTED_SHIRT_SIGNALS - polo was wrongly grouped with knitwear
        # (assumed XL-correct) until the user corrected it live: they're L
        # in polos too, same as dress shirts.
        self.assertTrue(m.is_oversized_fitted_shirt(
            "Ralph Lauren Men's Green/White Striped Short Sleeve Polo Shirt XL purple label"))
        self.assertTrue(m.is_oversized_fitted_shirt("Peter Millar Crown Crafted Polo XL"))

    def test_l_polo_is_not_flagged(self):
        self.assertFalse(m.is_oversized_fitted_shirt("Ralph Lauren Purple Label Polo Shirt L"))

    def test_xl_knitwear_and_outerwear_untouched(self):
        # Standing rule: user is L in fitted collared shirts (dress
        # shirts/polos) but genuinely XL in knitwear/outerwear - must
        # never conflate the two.
        for title in (
            "Zegna Cashmere Sweater XL",
            "Peter Millar Quarter Zip XL",
            "Barbour Waxed Jacket XL",
            "Loro Piana Cashmere Cardigan XXL",
        ):
            self.assertFalse(m.is_oversized_fitted_shirt(title), title)


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
        self.conn.execute("CREATE TABLE ai_pending (item_id TEXT PRIMARY KEY, first_seen_at TEXT)")

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


class AiPendingBacklogAging(unittest.TestCase):
    """Real live bug: a Brooks Brothers suit was re-scored 49 times over
    ~4 hours, blocked every single run on "no AI price estimate," because
    _ai_check_priority's only tiebreak within the must-have-AI bucket was
    descending price with no memory of how long a candidate had already
    waited - any run producing enough pricier must-have-AI candidates
    bumped it back down forever. 154 distinct items hit this retry loop at
    least once; 14 retried more than 5 times; the worst went 57 rounds.
    These test the persistence primitives that let PASS 2 age a stuck
    candidate up instead of losing to fresh, pricier competitors
    indefinitely."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(f"{self.tmpdir}/test.db")
        self.conn.execute("CREATE TABLE seen (item_id TEXT PRIMARY KEY, seen_at TEXT)")
        self.conn.execute(
            "CREATE TABLE fingerprints (fingerprint TEXT PRIMARY KEY, best_price REAL, seen_at TEXT)"
        )
        self.conn.execute("CREATE TABLE ai_pending (item_id TEXT PRIMARY KEY, first_seen_at TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_untracked_item_has_no_pending_minutes(self):
        self.assertEqual(m.get_ai_pending_minutes(self.conn, ["item1"]), {})

    def test_mark_ai_pending_is_tracked_and_ages(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=42)).isoformat()
        self.conn.execute("INSERT INTO ai_pending (item_id, first_seen_at) VALUES (?, ?)", ("item1", past))
        minutes = m.get_ai_pending_minutes(self.conn, ["item1"])
        self.assertAlmostEqual(minutes["item1"], 42, delta=1)

    def test_mark_ai_pending_does_not_overwrite_original_timestamp(self):
        # A candidate stuck across multiple runs must keep its ORIGINAL
        # first-seen time, not get reset to "now" on every retry - that
        # would defeat aging entirely (it would look freshly-stuck forever).
        past = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        self.conn.execute("INSERT INTO ai_pending (item_id, first_seen_at) VALUES (?, ?)", ("item1", past))
        m.mark_ai_pending(self.conn, "item1")  # simulates a later run re-hitting the same block
        minutes = m.get_ai_pending_minutes(self.conn, ["item1"])
        self.assertGreater(minutes["item1"], 55)

    def test_mark_seen_clears_pending_backlog_row(self):
        # Once an item reaches a genuine final disposition, it's resolved -
        # the backlog row must be cleaned up so it can't linger forever.
        m.mark_ai_pending(self.conn, "item1")
        self.assertIn("item1", m.get_ai_pending_minutes(self.conn, ["item1"]))
        m.mark_seen(self.conn, "item1")
        self.assertEqual(m.get_ai_pending_minutes(self.conn, ["item1"]), {})

    def test_mark_ai_pending_actually_commits(self):
        # Real live bug: mark_ai_pending() never called conn.commit(). A
        # read on the SAME connection sees its own uncommitted writes (so
        # every other test here would pass even with the bug present) -
        # the only way to catch it is a fresh connection to the same file,
        # exactly what run()'s conn.close() at the end of every run does.
        # Without the commit, that close silently rolled back every
        # pending row written that run unless something else (mark_seen)
        # happened to commit the same connection afterward - the common
        # case is PASS 3 calling this back-to-back with nothing else
        # committing in between, re-opening the exact starvation bug this
        # table exists to close.
        m.mark_ai_pending(self.conn, "item1")
        self.conn.close()
        reopened = sqlite3.connect(f"{self.tmpdir}/test.db")
        try:
            self.assertIn("item1", m.get_ai_pending_minutes(reopened, ["item1"]))
        finally:
            reopened.close()


class SeenTablePruning(unittest.TestCase):
    """Real live bug: seen_items.db grew to 57.66 MB (over GitHub's 50 MB
    warning threshold) - 264,822 rows in `seen` going back to Aug 7 with
    NO retention policy at all, committed to git on every run that
    touched it. Same class of problem as the binary-file git issues
    behind the Aug 9 outage."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(f"{self.tmpdir}/test.db")
        self.conn.execute("CREATE TABLE seen (item_id TEXT PRIMARY KEY, seen_at TEXT)")
        self.conn.execute(
            "CREATE TABLE fingerprints (fingerprint TEXT PRIMARY KEY, best_price REAL, seen_at TEXT)"
        )

    def tearDown(self):
        self.conn.close()

    def test_old_rows_pruned_recent_rows_kept(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.conn.execute("INSERT INTO seen VALUES ('old-item', ?)", (old,))
        self.conn.execute("INSERT INTO seen VALUES ('recent-item', ?)", (recent,))
        self.conn.execute("INSERT INTO fingerprints VALUES ('old-fp', 50.0, ?)", (old,))
        self.conn.execute("INSERT INTO fingerprints VALUES ('recent-fp', 50.0, ?)", (recent,))
        self.conn.commit()
        seen_deleted, fp_deleted = m.prune_old_seen_entries(self.conn)
        self.assertEqual((seen_deleted, fp_deleted), (1, 1))
        self.assertTrue(m.is_new(self.conn, "old-item"))
        self.assertFalse(m.is_new(self.conn, "recent-item"))
        self.assertIsNone(m.get_fingerprint_best_price(self.conn, "old-fp"))
        self.assertEqual(m.get_fingerprint_best_price(self.conn, "recent-fp"), 50.0)

    def test_no_deletions_returns_zero_without_vacuum_error(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.conn.execute("INSERT INTO seen VALUES ('recent-item', ?)", (recent,))
        self.conn.commit()
        self.assertEqual(m.prune_old_seen_entries(self.conn), (0, 0))


class ScoreListingHardFails(unittest.TestCase):
    def _listing(self, title, price=50.0, description=None):
        listing = {"title": title, "price": {"value": price, "currency": "USD"}, "itemId": "t1"}
        if description is not None:
            listing["description"] = description
        return listing

    def test_gender_keyword_blocks(self):
        result = m.score_listing(self._listing("Women's Ralph Lauren Sweater M"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("gender", result["reason"])

    def test_obfuscated_brand_name_blocks(self):
        # Real live miss: "Go- Yard Men's Slim Luxury Leather Card Holder
        # Wallet blue" ($33 landed) alerted as a 91% "Steal" with HIGH
        # price confidence - real Grailed sold comps ($375 median) got
        # applied to it as if genuine. "Go- Yard" is a textbook eBay
        # counterfeit-listing evasion spelling.
        result = m.score_listing(self._listing("Go- Yard Men's Slim Luxury Leather Card Holder Wallet blue"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("obfuscated", result["reason"])

    def test_clean_brand_spelling_not_flagged_as_obfuscated(self):
        result = m.score_listing(self._listing("Goyard Blue Card Holder Wallet Excellent Condition"), gap_report=None)
        self.assertNotIn("obfuscated", result.get("reason", ""))
        self.assertEqual(result["verdict"], "REVIEW")

    def test_swatch_collab_does_not_inherit_luxury_partner_tier(self):
        # Real live miss: "Swatch X Audemars Piguet Royal Pop Huit Blanc
        # Pocket Watch" ($125 landed) alerted as a 56% "Great Deal" with
        # brand_tier "grab_on_sight" - a Swatch collab piece is a genuine
        # Swatch (mass-produced, ~$50-300), not a real Audemars Piguet.
        result = m.score_listing(self._listing("Swatch X Audemars Piguet Royal Pop Huit Blanc Pocket Watch"), gap_report=None)
        self.assertIsNone(result.get("brand_tier"))

    def test_real_luxury_brand_still_gets_credit_without_swatch_collab(self):
        result = m.score_listing(self._listing("Audemars Piguet Royal Oak Chronograph", price=5000.0), gap_report=None)
        self.assertEqual(result.get("brand_tier"), "grab_on_sight")

    def test_condition_flag_caught_from_description_not_just_title(self):
        # Real live example: a Poshmark "Canali Travel Single Breasted...
        # Suit Blazer" listing's TITLE said nothing about damage, but its
        # description read "...Gently worn; FLAWS ... small hole on t...".
        # "hole" alone is CONDITION_FLAG_KEYWORDS (a soft flag, not
        # CONDITION_HARD_FAIL_KEYWORDS's "moth hole"), so this must surface
        # as a flag on the result rather than block it outright - but
        # before this fix it was invisible either way, since only the
        # title was ever checked and the title said nothing about it.
        result = m.score_listing(
            self._listing(
                "Canali Travel Single Breasted Wool Suit Blazer Men Size 44R",
                description="Gently worn; FLAWS: small hole on the left sleeve cuff.",
            ),
            gap_report=None,
        )
        self.assertIn("condition keyword flagged", " ".join(result.get("flags") or []))

    def test_condition_hard_fail_caught_from_description_too(self):
        # The hard-fail tier ("moth hole", not bare "hole") must also reach
        # into the description, not just the flag tier above.
        result = m.score_listing(
            self._listing("Canali Wool Suit Blazer Sz 44R", description="Has a small moth hole near the pocket."),
            gap_report=None,
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("condition hard-fail keyword", result["reason"])

    def test_fabric_recognized_from_description_not_just_title(self):
        # Same principle, the other direction: a description that states
        # the fabric should clear the "fabric not stated" flag even when
        # the title itself says nothing about material.
        no_desc = m.score_listing(self._listing("Canali Suit Blazer Sz 44R"), gap_report=None)
        self.assertIn("fabric not stated", " ".join(no_desc.get("flags") or []))
        with_desc = m.score_listing(
            self._listing("Canali Suit Blazer Sz 44R", description="100% cashmere, made in Italy."),
            gap_report=None,
        )
        self.assertNotIn("fabric not stated", " ".join(with_desc.get("flags") or []))

    def test_gender_keyword_caught_from_description_too(self):
        result = m.score_listing(
            self._listing("Canali Suit Blazer Sz 44R", description="Beautiful women's blazer, great fit."),
            gap_report=None,
        )
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

    # ---- EMPTY-PACKAGING HARD FAIL (real miss: a "literal box" alerted) ----

    def test_packaging_only_listings_hard_fail(self):
        for title in (
            "Rolex Box Only No Watch",
            "Empty Box",
            "Just The Box",
            "Box And Dust Bag Only",
            "Dust Bag Only",
            "Packaging Only",
            "Authenticity Card Only",
            "Receipt Only",
            "No Item Included",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertEqual(result["verdict"], "PASS", title)
            self.assertIn("packaging", result["reason"])

    def test_bag_only_with_box_or_dust_wording_hard_fails(self):
        # "bag only" is packaging-only evidence only when box/dust wording
        # is also present ("box and bag only", "dust bag + bag only") -
        # bare "bag only" is a real handbag, not an empty accessory.
        result = m.score_listing(self._listing("Box And Bag Only No Watch"), gap_report=None)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIn("packaging", result["reason"])

    def test_included_packaging_mention_is_not_blocked(self):
        # A genuine item that merely MENTIONS included packaging is a
        # POSITIVE signal ("comes with box and papers", "with dust bag"),
        # not packaging-only - these must all still reach REVIEW.
        for title in (
            "Goyard Card Holder With Original Box And Papers",
            "Goyard Card Holder Includes Original Box",
            "Goyard Card Holder With Dust Bag",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertNotEqual(result["verdict"], "PASS", title)
            self.assertNotIn("packaging", result.get("reason", ""))

    # ---- STRENGTHENED DAMAGE DETECTION (real miss: "heavily damaged" LV wallet) ----

    def test_heavily_damaged_terms_hard_fail(self):
        for title in (
            "Heavily Damaged LV Wallet",
            "Goyard Wallet As-Is Damage",
            "Torn Lining Goyard Card Holder",
            "Ripped Leather Wallet",
            "Cracked Leather Goyard Wallet",
            "Peeling Leather Wallet",
            "Water Damage Goyard Card Holder",
            "Mold On Leather Wallet",
            "Mildew Goyard Wallet",
            "Restoration Project Goyard Wallet",
            "Parts Only Goyard Wallet",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertEqual(result["verdict"], "PASS", title)
            self.assertIn("condition hard-fail keyword", result["reason"])

    def test_ambiguous_damage_terms_flag_but_do_not_block(self):
        # "smells" (could be "smells like new") and "heavily worn" (could be
        # desirable patina) are CONDITION_FLAG_KEYWORDS, not hard-fails -
        # they must surface as a flag without blocking the listing.
        for title in (
            "Goyard Wallet Heavily Worn",
            "Goyard Card Holder Smells Like Smoke",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertNotEqual(result["verdict"], "PASS", title)
            self.assertIn("condition keyword flagged", " ".join(result.get("flags") or []))

    def test_normal_wear_and_patina_are_not_blocked(self):
        # Honest wear descriptions must keep passing - "minor wear" and
        # "light patina" are normal secondhand condition language, not
        # "heavily damaged".
        for title in (
            "Goyard Card Holder Minor Wear",
            "Goyard Wallet Light Patina",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertNotEqual(result["verdict"], "PASS", title)

    # ---- COUNTERFEIT SIGNALS (real miss: a "fake goyard wallet" alerted) ----

    def test_replica_language_hard_fails(self):
        for title in (
            "Goyard Replica Card Holder",
            "Card Holder Inspired By Goyard",
            "Goyard Mirror Quality Wallet",
            "Goyard 1:1 Wallet",
            "Unauthenticated Goyard Card Holder",
            "Not Authentic Goyard Wallet",
            "No Guarantee Of Authenticity Goyard Wallet",
            "AAA Quality Goyard Wallet",
            "Faux Designer Goyard Wallet",
        ):
            result = m.score_listing(self._listing(title), gap_report=None)
            self.assertEqual(result["verdict"], "PASS", title)
            self.assertIn("counterfeit/replica", result["reason"])

    def test_honest_authenticity_hedging_is_not_blocked(self):
        # Honest hedging a real reseller uses - a judgment call for the
        # buyer, not a seller openly advertising a fake. These must still
        # reach REVIEW (not hard-fail as counterfeit).
        cases = (
            ("Goyard Card Holder Guaranteed Authentic", None),
            ("Goyard Card Holder Please Authenticate Yourself", None),
            ("Goyard Card Holder", "authenticity not verified by me but purchased from authorized retailer"),
        )
        for title, description in cases:
            result = m.score_listing(self._listing(title, description=description), gap_report=None)
            self.assertNotEqual(result["verdict"], "PASS", title)
            self.assertNotIn("counterfeit", result.get("reason", ""))

    def test_faux_leather_is_not_blocked_as_counterfeit(self):
        # "faux leather" is a legitimate material (synthetic), not a
        # counterfeit claim - only the "faux designer" phrase is blocked.
        self.assertFalse(m.COUNTERFEIT_SIGNALS.search("faux leather card holder"))


class StealQualityGate(unittest.TestCase):
    def test_narrow_category_grab_on_sight_searches_never_blind_trust(self):
        # Root issue this mechanism exists for: "montblanc pen"
        # (grab_on_sight tier) fired alerts for an umbrella, perfume, an
        # empty leather gift box, a cosmetic bag, a sunglasses case, and
        # even AFTER adding a real exclusion list, STILL let through
        # "Montblanc red pen ink refills new" - Montblanc spans too many
        # product lines for a hand-curated exclusion list to ever fully
        # keep up with. Removed entirely afterward per explicit user
        # instruction ("i do NOT need a pen...dont waste ur time and
        # resources on those"), but the underlying mechanism (require a
        # real AI check for brand-new/narrow searches, same as a non-
        # grab_on_sight brand needs everywhere else) still protects the
        # remaining searches in this tuple - exercised here via
        # "smythson cardholder", a real live entry with no exclusion
        # terms attached.
        result = {"deal_rating": None, "brand_tier": "grab_on_sight", "search_query": "smythson cardholder"}
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertIn("narrow-category bar", reason)
        # But once the AI DID actually check it and it clears the normal
        # bar, it must alert same as anything else.
        result["deal_rating"] = "Steal"
        result["discount_pct"] = 75
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_narrow_category_bar_survives_real_exclusion_terms_in_query(self):
        # search_query carries the RAW config query, "-exclusion" terms
        # and all (e.g. a real search might read "foo bar -baz -qux") -
        # the membership check must strip those first or it can never
        # match. Uses turnbull asser shirt's real shape as a stand-in
        # since it (like montblanc pen used to) has no exclusions of its
        # own today - constructing one here to guard against a future
        # search in this tuple gaining exclusions and silently breaking
        # this check the same way it broke on montblanc pen.
        result = {
            "deal_rating": None, "brand_tier": "grab_on_sight",
            "search_query": "turnbull asser shirt -logo -damaged",
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertIn("narrow-category bar", reason)

    def test_other_grab_on_sight_brands_still_blind_trust_normally(self):
        # This must be scoped ONLY to the specific narrow-category
        # searches, not grab_on_sight brands in general - e.g. Alden
        # (shoes-only, long track record) should be unaffected.
        result = {"deal_rating": None, "brand_tier": "grab_on_sight", "search_query": "alden shoes"}
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

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

    def test_watch_brand_mismatch_blocks_even_with_a_great_deal_rating(self):
        # Real live miss: a genuine Oris was listed with its own eBay
        # item-specifics metadata mislabeled as "Seiko". A real AI check
        # confirmed the mismatch - must block regardless of how good the
        # price otherwise looks, and permanently (not retry-eligible).
        result = {
            "deal_rating": "Steal", "discount_pct": 80, "brand_tier": "grab_on_sight",
            "watch_brand_mismatch": True,
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="watches")
        self.assertIsNotNone(reason)
        self.assertNotIn("no AI price", reason)

    def test_watch_without_brand_mismatch_unaffected(self):
        result = {
            "deal_rating": "Steal", "discount_pct": 80, "brand_tier": "grab_on_sight",
            "watch_brand_mismatch": False,
        }
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="watches"))

    def test_suit_bar_retail_discount_path_requires_recognized_brand(self):
        # Live bug: 5 "Hunter Haig" suits (brand_tier None - totally
        # unrecognized, an eBay fuzzy-match on the "huntsman suit" query,
        # not the searched-for brand) all cleared the suit bar on the
        # retail-discount path despite Marginal/Fair resale ratings - the
        # AI's own retail guess for this unknown brand was self-
        # inconsistent ($250-600 for the same style of vintage suit).
        # Real fields from one: $58.82 landed, $250 "retail", Marginal.
        result = {
            "deal_rating": "Marginal", "discount_pct": -47, "price_confidence": "medium",
            "brand_tier": None, "search_query": "huntsman suit -hunter",
            "estimated_retail_price": 250.0, "price": 58.82,
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="tailoring")
        self.assertIsNotNone(reason)
        self.assertIn("suit bar", reason)
        # A brand the AI actually has pricing knowledge of (the Zegna case
        # the retail-discount path was built for) must still clear it.
        # discount_pct raised to 0 (break-even vs resale) to match the real
        # Zegna fixture this path exists for - $200 ask / $200 resale /
        # $2500 retail. The original -47 here is now correctly blocked by
        # the newer "don't pay MORE than resale" rule, so it can't double
        # as the passing case anymore; that rule has its own test below.
        result["brand_tier"] = "standard"
        result["discount_pct"] = 0
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="tailoring"))

    def test_suit_retail_path_rejects_paying_more_than_resale(self):
        # Real user report after the suit caps were raised to $200: a flood
        # of alerts like "$212 landed vs $130 resale" (-63%) and "$175 vs
        # $75" (-134%), all rated Marginal, all correctly identified as bad
        # by the AI, all alerted anyway. Luxury suit RETAIL is always
        # $1200-2500 while the used market is $75-300, so "70% off retail"
        # was trivially true for anything in range - a rubber stamp.
        result = {
            "deal_rating": "Marginal", "discount_pct": -63, "price_confidence": "medium",
            "brand_tier": "grab_on_sight", "search_query": "brooks brothers golden fleece suit",
            "estimated_retail_price": 1500.0, "price": 212.0,
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="tailoring")
        self.assertIsNotNone(reason)
        self.assertIn("suit bar", reason)
        # Break-even or better against resale still clears (the Zegna case).
        result["discount_pct"] = 0
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="tailoring"))

    def test_no_ai_candidate_is_never_permanently_discarded(self):
        # THE contract that makes the pre-AI skip safe. PASS 3 calls this
        # gate BEFORE spending an AI call and permanently mark_seen()s
        # anything whose reason lacks the "no AI price" retry marker. So
        # any bar that rejects purely because deal_rating is None (= "not
        # checked yet", the normal pre-AI state) silently throws the
        # candidate away before it is ever evaluated.
        #
        # Real bug this pins, caught by an independent audit and confirmed
        # live: the loro piana/cucinelli and knitwear bars both did
        # `if deal_rating != "Steal": return "...below Steal"`, which fires
        # on None - so every Loro Piana, Brunello Cucinelli and knitwear
        # candidate was discarded before its first AI check and those
        # searches could never alert at all.
        #
        # Exhaustive on purpose: a targeted test would only have covered
        # the bars someone thought to check.
        import itertools
        categories = ["other", "knitwear", "watches", "tailoring", "school-gear", "outerwear", "shoes"]
        tiers = ["grab_on_sight", "standard", None]
        queries = [
            "loro piana cashmere sweater", "brunello cucinelli jacket", "johnstons elgin cashmere",
            "canali suit", "omega watch", "peter millar gamecocks polo",
            "peter millar crown crafted polo", "smythson cardholder", "alden shoes", "zegna sweater",
        ]
        for category, tier, query in itertools.product(categories, tiers, queries):
            result = {
                "deal_rating": None, "brand_tier": tier, "price": 100.0, "search_query": query,
                "listing": {"title": "Test Item L"}, "peter_millar_back_crown_visible": None,
            }
            reason = m.is_blocked_by_steal_quality_gate(result, category=category)
            if reason is None or "no AI price" in reason:
                continue
            # The legitimate exceptions: a permanent block an AI check
            # could never resolve, because it doesn't depend on price at
            # all. brand_tier is fixed before the AI ever runs, and so is
            # whether the listing's own title names the brand that was
            # searched for (the generic "Test Item L" fixture here never
            # names loro piana/cucinelli, so that query legitimately hits
            # the title-mismatch bar added after a real live miss).
            self.assertTrue(
                "brand not grab_on_sight-tier" in reason or "title names neither brand" in reason,
                f"{category}/{tier}/{query} is permanently discarded before any AI check: {reason}",
            )

    def test_permanent_blocks_are_distinguishable_from_needs_ai(self):
        # PASS 3 relies on this split twice: it skips spending a scarce AI
        # call on anything permanently blocked, and _ai_check_priority uses
        # it to rank candidates that are one check away from alerting. Any
        # block an AI check could resolve MUST carry the shared "no AI
        # price" retry marker; anything else must not.
        needs_ai = {
            "deal_rating": None, "brand_tier": "grab_on_sight", "price": 200.0,
            "search_query": "omega watch", "listing": {"title": "Omega Seamaster"},
        }
        reason = m.is_blocked_by_steal_quality_gate(needs_ai, category="watches")
        self.assertIn("no AI price", reason)
        permanent = {
            "deal_rating": None, "brand_tier": "standard", "price": 40.0,
            "search_query": "zegna sweater", "listing": {"title": "Zegna Sweater L"},
        }
        reason = m.is_blocked_by_steal_quality_gate(permanent, category="knitwear")
        self.assertIsNotNone(reason)
        self.assertNotIn("no AI price", reason)

    def test_suit_blind_trust_has_a_price_ceiling(self):
        # 10 of 15 alerts in one real flood had deal_rating None - zero
        # price evidence, alerted purely on grab_on_sight brand tier, at
        # $126-$206 each. Blind-trusting a cheap suit is a reasonable bet;
        # blind-trusting a $200 one is paying for brand recognition alone.
        # User's stated comfort zone: "rather purchase suits for like
        # $80-150 tops...i just dont wanna filter out steal of lifetimes."
        base = {
            "deal_rating": None, "brand_tier": "grab_on_sight",
            "search_query": "hickey freeman suit", "listing": {"title": "Hickey Freeman Suit 42R"},
        }
        over = dict(base, price=206.0)
        reason = m.is_blocked_by_steal_quality_gate(over, category="tailoring")
        self.assertIsNotNone(reason)
        # Retry-eligible so it keeps competing for an AI slot on later runs.
        self.assertIn("no AI price", reason)
        # Inside the comfort zone, brand tier alone is still enough.
        under = dict(base, price=126.0)
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(under, category="tailoring"))

    def test_suit_bar_resale_path_unaffected_by_brand_recognition(self):
        # The retail-discount path is only ONE of two ways to clear the
        # bar - a genuinely good resale-based deal_rating must still pass
        # regardless of brand_tier, unrecognized brand included - as long
        # as the listing's own title actually names the brand searched for.
        result = {
            "deal_rating": "Good Deal", "discount_pct": 40, "price_confidence": "medium",
            "brand_tier": None, "search_query": "huntsman suit -hunter",
            "listing": {"title": "Huntsman Savile Row Suit 42R"},
        }
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="tailoring"))

    def test_suit_bar_blocks_title_brand_mismatch_from_search(self):
        # Real live miss: "Jones New York...Black Pinstripe Cashmere Wool
        # Suit" ($14.99) cleared the resale path via eBay's own loose
        # search matching on a real target-brand query - discount math off
        # a near-zero price trivially clears 30%+ for ANY suit, brand
        # irrelevant, once nothing checks that the searched brand and the
        # actual listing agree.
        result = {
            "deal_rating": "Good Deal", "discount_pct": 60, "price_confidence": "medium",
            "brand_tier": None, "search_query": "gieves & hawkes suit",
            "listing": {"title": "Jones New York Black Pinstripe Suit 42R"},
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="tailoring")
        self.assertIsNotNone(reason)

    def test_knitwear_requires_grab_on_sight_and_steal(self):
        result = {"deal_rating": "Steal", "discount_pct": 75, "brand_tier": "standard"}
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))
        result["brand_tier"] = "grab_on_sight"
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))

    def test_crown_crafted_bar_applies_to_quarter_zips_not_the_stricter_knitwear_bar(self):
        # Real gap: "peter millar crown crafted quarter zip" (added per
        # explicit user instruction - "$20 for a crown crafted polo and
        # $25 for a crown crafted quarter zip are a great deal...even just
        # a good deal, doesn't have to be crazy") classifies as "knitwear"
        # (quarter-zip triggers that classifier), and the knitwear check
        # used to run BEFORE the crown-crafted carve-out - so a standard-
        # tier, Good-Deal-rated crown crafted quarter zip would have been
        # silently caught by knitwear's much stricter grab_on_sight+Steal-
        # only bar instead. Same ordering bug already fixed once for the
        # gamecocks bar.
        result = {
            "deal_rating": "Good Deal", "discount_pct": 40, "price_confidence": "medium",
            "brand_tier": "standard", "search_query": "peter millar crown crafted quarter zip",
        }
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))
        # Marginal/Fair still correctly blocked - the bar is looser, not gone.
        result["deal_rating"] = "Marginal"
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="knitwear"))

    def _gamecocks_result(self, title, **kwargs):
        # peter_millar_back_crown_visible defaults to True so these tests
        # exercise the GAMECOCKS bar specifically, isolated from the newer,
        # stricter back-crown requirement (see PeterMillarBackCrownRequired) -
        # every Peter Millar title now needs the crown confirmed first,
        # regardless of which more specific bar it would otherwise hit.
        result = {
            "listing": {"title": title},
            "search_query": "peter millar gamecocks quarter zip",
            "peter_millar_back_crown_visible": True,
        }
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
        # say "gamecocks" - anything else falls through to normal
        # (stricter) treatment instead of the loose bar. Deliberately does
        # NOT include "south carolina" - explicit user correction, that
        # phrase alone hits golf courses and plenty of other things with
        # no connection to the team ("Peter Millar South Carolina Country
        # Club Polo" would otherwise wrongly qualify).
        for title in (
            "Peter Millar Quarter Zip Stanford Men's L",
            "Peter Millar Shirt Plaid Check Button Down",
            "Peter Millar South Carolina Country Club Polo",
        ):
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
        result = {
            "deal_rating": "Great Deal", "discount_pct": 59, "price_confidence": "high",
            "search_query": "brunello cucinelli jacket",
            "listing": {"title": "Brunello Cucinelli Cashmere Jacket 52"},
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertIn("below Steal", reason)
        result["deal_rating"] = "Steal"
        result["discount_pct"] = 72
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))
        result2 = {
            "deal_rating": "Steal", "discount_pct": 75, "price_confidence": "high",
            "search_query": "loro piana suit",
            "listing": {"title": "Loro Piana Wool Suit 42R"},
        }
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result2, category="other"))

    def test_loro_piana_cucinelli_bar_blocks_title_brand_mismatch_from_search(self):
        # Same gap class as the suit bar's title-mismatch fix: a real Jones
        # New York suit cleared the suit bar via eBay's loose search
        # matching on a real target-brand query. This bar had the identical
        # hole - only the SEARCH query was ever checked, never the listing's
        # own title.
        result = {
            "deal_rating": "Steal", "discount_pct": 80, "price_confidence": "high",
            "search_query": "loro piana sweater",
            "listing": {"title": "Random Unbranded Cashmere Sweater L"},
        }
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertNotIn("no AI price", reason)

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


class SellerFeedbackWatchGate(unittest.TestCase):
    """Real motivating case: a "Rolex Two-Tone Datejust" alerted at $208
    against the AI's own $7,500 retail estimate - a genuine Rolex never
    sells that cheap, and an established, high-feedback seller is far less
    likely to be running a counterfeit-listing scam than a brand-new/
    low-feedback account. This gate adds seller feedbackScore/
    feedbackPercentage as a trust signal ON TOP of the existing watches bar
    (which already requires a real AI check + Steal/Great Deal). It only
    ever fires on eBay listings that actually carry the fields - the
    motivating example itself was on Poshmark, which has no public
    seller-feedback field, so non-eBay listings (both fields None) must
    never be blocked here."""

    def _steal_watch(self, **kwargs):
        result = {
            "deal_rating": "Steal", "discount_pct": 75, "price_confidence": "high",
        }
        result.update(kwargs)
        return result

    def test_steal_watch_with_low_feedback_score_is_blocked(self):
        reason = m.is_blocked_by_steal_quality_gate(
            self._steal_watch(seller_feedback_score=5, seller_feedback_percentage=90.0),
            category="watches",
        )
        self.assertIsNotNone(reason)
        self.assertIn("watches bar", reason)
        self.assertNotIn("no AI price", reason)  # permanent reject, not retry-eligible

    def test_steal_watch_with_high_feedback_score_allowed(self):
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(
            self._steal_watch(seller_feedback_score=500, seller_feedback_percentage=99.5),
            category="watches",
        ))

    def test_high_percentage_or_high_score_alone_satisfies_the_bar(self):
        # The bar is "score >= 50 OR percentage >= 95.0" - either signal
        # on its own must rescue a watch the other would otherwise block.
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(
            self._steal_watch(seller_feedback_score=5, seller_feedback_percentage=99.8),
            category="watches",
        ))
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(
            self._steal_watch(seller_feedback_score=80, seller_feedback_percentage=90.0),
            category="watches",
        ))

    def test_absent_feedback_fields_do_not_block(self):
        # Non-eBay platform (Poshmark/Vinted/Grailed/ShopGoodwill) or an
        # eBay response that omitted seller feedback - both fields None, so
        # there's no signal to check against and the watch must clear the
        # existing watches bar unchanged.
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(
            self._steal_watch(), category="watches",
        ))

    def test_non_watch_categories_unaffected_regardless_of_feedback(self):
        # The gate is watches-only - a low-feedback seller on any other
        # category must still clear that category's normal bar, not be
        # newly blocked on feedback. brand_tier=grab_on_sight keeps the
        # knitwear case on its normal pass path so only the feedback signal
        # (which must be ignored) is being exercised here.
        for category in ("other", "tailoring", "knitwear", "golf"):
            self.assertIsNone(m.is_blocked_by_steal_quality_gate(
                self._steal_watch(
                    brand_tier="grab_on_sight",
                    seller_feedback_score=3, seller_feedback_percentage=80.0,
                ),
                category=category,
            ), category)


class MarketSaturationGate(unittest.TestCase):
    """Real live miss: "TRAFALGAR HANDMADE BLACK GLOVE LEATHER BELT" ($9.99)
    alerted as a 52% "Great Deal" against a $22 AI resale guess with zero
    real sold-comp backing - search_total_listings was 977 for that exact
    query, meaning ~1,000 near-identical belts were already live on eBay.
    User's own words: "trafalgar has like a billion items for like $15 on
    ebay, not special.\""""

    def _result(self, **kwargs):
        result = {
            "deal_rating": "Great Deal", "discount_pct": 52, "price_confidence": "medium",
            "brand_tier": "standard", "liquidity": "medium", "search_total_listings": 977,
            "flags": ["fabric not stated", "AI fabric tag: Leather (high confidence)"],
        }
        result.update(kwargs)
        return result

    def test_oversaturated_market_with_no_real_comps_blocks(self):
        reason = m.is_blocked_by_steal_quality_gate(self._result(), category="other")
        self.assertIsNotNone(reason)
        self.assertIn("oversaturated market", reason)

    def test_real_sold_comps_bypass_the_saturation_check(self):
        # Comps already reflect real completed sales in this same
        # saturated market - the AI-guess-specific risk doesn't apply.
        result = self._result(flags=["Grailed sold comps: median $25 across 8 recent sales"])
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_steal_tier_survives_saturation(self):
        # A genuine Steal is a big enough apparent gap to survive scrutiny
        # even in a saturated market - only "Great Deal" is the borderline
        # case this guards.
        result = self._result(deal_rating="Steal")
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_low_listing_count_unaffected(self):
        result = self._result(search_total_listings=50)
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_missing_listing_count_does_not_block(self):
        result = self._result(search_total_listings=None)
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))


class PeterMillarBackCrownRequired(unittest.TestCase):
    """Explicit, standing user instruction: "every incoming Peter Millar
    top (polo, quarter-zip, mid-layer) must feature the raised/metallic or
    silicone back crown below the rear collar. No back crown = automatic
    PASS...regardless of price or fabric...in general i need crowns in
    them all rn anyways." Runs before every other Peter Millar-specific
    bar - a stricter, universal precondition on top of them, not an
    alternate looser path."""

    def _pm_result(self, title="Peter Millar Green Polo L", **kwargs):
        result = {"listing": {"title": title}}
        result.update(kwargs)
        return result

    def test_no_ai_check_yet_blocks_and_is_retry_eligible(self):
        result = self._pm_result(deal_rating=None, brand_tier="grab_on_sight")
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertIn("no AI price", reason)  # retry-eligible substring

    def test_crown_confirmed_present_clears_it(self):
        result = self._pm_result(
            deal_rating="Great Deal", discount_pct=50, brand_tier="grab_on_sight",
            peter_millar_back_crown_visible=True,
        )
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_crown_confirmed_absent_blocks_regardless_of_deal_rating(self):
        result = self._pm_result(
            deal_rating="Great Deal", discount_pct=50, brand_tier="grab_on_sight",
            peter_millar_back_crown_visible=False,
        )
        reason = m.is_blocked_by_steal_quality_gate(result, category="other")
        self.assertIsNotNone(reason)
        self.assertNotIn("no AI price", reason)  # permanent, not retry-eligible

    def test_crown_unconfirmed_after_a_real_check_still_blocks(self):
        # AI ran and looked, but couldn't tell either way (null) - still a
        # hard block, not a pass-by-default.
        result = self._pm_result(
            deal_rating="Great Deal", discount_pct=50, brand_tier="grab_on_sight",
            peter_millar_back_crown_visible=None,
        )
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_non_peter_millar_item_unaffected(self):
        result = self._pm_result(
            title="Ralph Lauren Polo L", deal_rating="Great Deal", discount_pct=50, brand_tier="grab_on_sight",
        )
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_non_top_garment_skips_the_crown_gate_entirely(self):
        # Real live bug: this used to key off "peter millar" appearing
        # ANYWHERE in the title, but the AI prompt only ever evaluates
        # crown visibility for a polo/quarter-zip/mid-layer - a jacket
        # always gets peter_millar_back_crown_visible=null (not applicable,
        # not "missing"), so the enabled "peter millar gamecocks jacket"
        # search could never alert on anything at all. Must fall through to
        # the gamecocks bar instead of being blocked here.
        result = self._pm_result(
            title="Peter Millar Gamecocks Full-Zip Jacket XL",
            deal_rating="Good Deal", discount_pct=30, brand_tier="grab_on_sight",
            search_query="peter millar gamecocks jacket",
            peter_millar_back_crown_visible=None,
        )
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))

    def test_quarter_zip_and_mid_layer_still_require_the_crown(self):
        for title in ("Peter Millar Quarter Zip Pullover L", "Peter Millar 1/4 Zip Mid Layer L"):
            result = self._pm_result(
                title=title, deal_rating="Great Deal", discount_pct=50, brand_tier="grab_on_sight",
                peter_millar_back_crown_visible=None,
            )
            reason = m.is_blocked_by_steal_quality_gate(result, category="other")
            self.assertIsNotNone(reason, title)
            self.assertIn("back-crown", reason)


class MarketplaceRelevanceTokenMatching(unittest.TestCase):
    """Real live bug: is_relevant_marketplace_listing()'s final fallback
    check used bare substring matching with no minimum token length -
    "n peal sweater" (enabled search) tokenized to ["n", "peal"] after
    "sweater" dropped as a stopword, and "n" in title matched virtually
    any English title, defeating the whole relevance check for that
    search. "tom james merino" had the same problem in a subtler form:
    "tom" (3 chars, looks like a real token) still substring-matched
    "custom"/"bottom"/"Tommy Hilfiger" - a different brand entirely."""

    def test_single_letter_token_no_longer_matches_everything(self):
        listing = {"platform": "vinted", "title": "Vintage brown wool cardigan sweater, knit"}
        self.assertFalse(m.is_relevant_marketplace_listing(listing, "n peal sweater"))

    def test_real_n_peal_listing_still_matches(self):
        listing = {"platform": "vinted", "title": "N.Peal Cashmere Crew Neck Sweater Navy"}
        self.assertTrue(m.is_relevant_marketplace_listing(listing, "n peal sweater"))

    def test_short_token_no_longer_substring_matches_a_different_brand(self):
        listing = {"platform": "poshmark", "title": "Custom Tommy Hilfiger merino sweater"}
        self.assertFalse(m.is_relevant_marketplace_listing(listing, "tom james merino"))

    def test_real_tom_james_listing_still_matches(self):
        listing = {"platform": "poshmark", "title": "Tom James Custom Merino Wool Sweater"}
        self.assertTrue(m.is_relevant_marketplace_listing(listing, "tom james merino"))

    def test_hat_synonym_check_is_whole_word_not_substring(self):
        # Real live bug: REQUIRED_ITEM_TYPE_SYNONYMS' "hat" set (hat/cap/
        # beanie/snapback/bucket hat/fitted) was checked with a bare `in`
        # substring test, the one spot in this function that missed the
        # \b whole-word fix everything else got. "cap" matched inside
        # "cape". Enabled search: `"maison margiela" hat`. NOTE: "fitted"
        # itself remains a genuine (if ambiguous - fitted cap vs fitted
        # blazer) whole-word synonym in this list; that ambiguity is
        # separate from and not fixed by this whole-word change.
        self.assertFalse(m.is_relevant_marketplace_listing(
            {"platform": "poshmark", "title": "Maison Margiela Cape"}, '"maison margiela" hat'))
        self.assertTrue(m.is_relevant_marketplace_listing(
            {"platform": "poshmark", "title": "Maison Margiela Wool Beanie Hat"}, '"maison margiela" hat'))


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


class EbayItemDescription(unittest.TestCase):
    """eBay's item_summary/search (search_ebay()) never returns a
    description, only this separate per-item call does - added per
    explicit user instruction to use descriptions for size/fabric/
    condition context the AI decides with. Must degrade gracefully:
    description enrichment is a nice-to-have, never worth failing or
    slowing down a run over."""

    def test_strips_html_and_unescapes_entities(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"description": "<p>100% wool &amp; cashmere.</p><br/>Size 44R"}
        with mock.patch("requests.get", return_value=fake_resp):
            text = m.fetch_ebay_item_description("fake-token", "v1|123|0")
        self.assertEqual(text, "100% wool & cashmere. Size 44R")

    def test_failure_returns_none_never_raises(self):
        with mock.patch("requests.get", side_effect=requests.exceptions.ConnectionError("boom")):
            text = m.fetch_ebay_item_description("fake-token", "v1|123|0")
        self.assertIsNone(text)

    def test_missing_description_field_returns_none(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"title": "no description key at all"}
        with mock.patch("requests.get", return_value=fake_resp):
            text = m.fetch_ebay_item_description("fake-token", "v1|123|0")
        self.assertIsNone(text)


class EbayEndingSoonAuctions(unittest.TestCase):
    """Per explicit user instruction: "auctions that are underwatched and
    that I can get alerted like 15 min before it ends, do some research
    quick, and then immediately scoop it up last second." eBay auctions
    run 3-7 days - alerting the moment a search finds one means alerting
    on a bid nowhere near final, same reason search_shopgoodwill() gates
    on remaining time. Contested (bidCount > 0) gets a tighter window -
    a bid war means the price is already being pushed toward fair value,
    the opposite of "underwatched"."""

    def _item(self, minutes_from_now, bid_count=0, item_id="v1|1|0"):
        end = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
        return {
            "itemId": item_id, "title": "Rolex Datejust",
            "price": {"value": 200.0, "currency": "USD"},
            "itemWebUrl": "https://example.com/1",
            "itemEndDate": end.isoformat().replace("+00:00", "Z"),
            "bidCount": bid_count,
        }

    def _search(self, items):
        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"itemSummaries": items, "total": len(items)}
        with mock.patch("requests.get", return_value=fake_resp):
            return m.search_ebay_ending_soon_auctions(
                "fake-token", {"query": "watch", "category_id": "31387", "max_price": 6000}
            )

    def test_uncontested_within_15min_window_included(self):
        listings, _ = self._search([self._item(10, bid_count=0)])
        self.assertEqual(len(listings), 1)
        self.assertTrue(listings[0]["is_ending_soon_auction"])
        self.assertEqual(listings[0]["bid_count"], 0)

    def test_uncontested_too_far_out_excluded(self):
        listings, _ = self._search([self._item(45, bid_count=0)])
        self.assertEqual(listings, [])

    def test_contested_uses_tighter_window(self):
        # 10 min out clears the 15-min uncontested bar but not the 6-min
        # contested one - a bid war means it's NOT underwatched anymore.
        listings, _ = self._search([self._item(10, bid_count=3)])
        self.assertEqual(listings, [])
        listings, _ = self._search([self._item(4, bid_count=3)])
        self.assertEqual(len(listings), 1)

    def test_already_ended_excluded(self):
        listings, _ = self._search([self._item(-5)])
        self.assertEqual(listings, [])

    def test_missing_or_unparseable_end_date_excluded_not_crashed(self):
        item = self._item(10)
        del item["itemEndDate"]
        listings, _ = self._search([item])
        self.assertEqual(listings, [])
        item2 = self._item(10)
        item2["itemEndDate"] = "not-a-date"
        listings, _ = self._search([item2])
        self.assertEqual(listings, [])

    def test_auction_minutes_remaining_recorded(self):
        listings, _ = self._search([self._item(12)])
        self.assertAlmostEqual(listings[0]["auction_minutes_remaining"], 12, delta=1)


class VintedItemDescription(unittest.TestCase):
    """search_vinted()'s catalog API never returns a description (confirmed
    live) - only the item's public page does, via its og:description meta
    tag. Live bug: "Vintage Seiko SQ gold-tone quartz watch" (gender-
    neutral title) blind-trust alerted, but its real page opened "This is
    a vintage women's Seiko SQ Gold-Tone Day-Date Quartz Watch..." -
    invisible to GENDER_EXCLUDE_KEYWORDS because Vinted had no description
    to check it against at all."""

    def test_extracts_and_unescapes_og_description(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.text = (
            '<html><head><meta property="og:description" '
            'content="This is a vintage women&#x27;s Seiko SQ watch.">'
            "</head></html>"
        )
        with mock.patch("requests.get", return_value=fake_resp):
            text = m.fetch_vinted_item_description("https://www.vinted.com/items/123-seiko")
        self.assertEqual(text, "This is a vintage women's Seiko SQ watch.")

    def test_failure_returns_none_never_raises(self):
        with mock.patch("requests.get", side_effect=requests.exceptions.ConnectionError("boom")):
            text = m.fetch_vinted_item_description("https://www.vinted.com/items/123-seiko")
        self.assertIsNone(text)

    def test_missing_meta_tag_returns_none(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.text = "<html><head></head></html>"
        with mock.patch("requests.get", return_value=fake_resp):
            text = m.fetch_vinted_item_description("https://www.vinted.com/items/123-seiko")
        self.assertIsNone(text)


class MakeListingDescription(unittest.TestCase):
    def test_description_passed_through_when_present(self):
        listing = p.make_listing(
            "poshmark", "abc123", "Canali Suit", 50.0, "https://example.com/x", description="Small hole on cuff."
        )
        self.assertEqual(listing["description"], "Small hole on cuff.")

    def test_no_description_key_when_absent(self):
        listing = p.make_listing("poshmark", "abc123", "Canali Suit", 50.0, "https://example.com/x")
        self.assertNotIn("description", listing)


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


class SendAlertRetailResaleLine(unittest.TestCase):
    """Per explicit user instruction: "it could be nice to see estimated
    retail + what its worth now etc. so i can see at a quick glance.\""""

    def _send_and_capture(self, result):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["message"] = data.decode("utf-8")
            return fake_resp

        with mock.patch("requests.post", side_effect=fake_post):
            m.send_alert(result)
        return captured["message"]

    def test_retail_and_resale_both_shown(self):
        result = {
            "listing": {"title": "Canali Suit", "itemWebUrl": "https://x", "platform": None},
            "price": 100.0, "item_price": 90.0, "shipping_cost": 10.0,
            "estimated_retail_price": 800, "estimated_resale_value": 250,
            "deal_rating": "Steal", "discount_pct": 60,
        }
        message = self._send_and_capture(result)
        self.assertIn("retail ~$800", message)
        self.assertIn("resale ~$250", message)

    def test_gracefully_omitted_when_no_estimate_available(self):
        result = {
            "listing": {"title": "Canali Suit", "itemWebUrl": "https://x", "platform": None},
            "price": 100.0, "item_price": 90.0, "shipping_cost": 10.0,
        }
        message = self._send_and_capture(result)
        self.assertNotIn("retail", message)
        self.assertNotIn("resale", message)

    def test_seller_feedback_line_appears_when_present(self):
        # Feature 1: seller feedback shown for EVERY eBay listing that
        # carries it, so the user can factor it in even outside watches.
        result = {
            "listing": {"title": "Canali Suit", "itemWebUrl": "https://x", "platform": None},
            "price": 100.0, "item_price": 90.0, "shipping_cost": 10.0,
            "seller_feedback_score": 120, "seller_feedback_percentage": 99.2,
        }
        message = self._send_and_capture(result)
        self.assertIn("seller: 120 feedback, 99.2% positive", message)

    def test_seller_feedback_line_omitted_when_absent(self):
        # Non-eBay listings never carry the field - no stray "seller:"
        # line must appear in their alerts.
        result = {
            "listing": {"title": "Canali Suit", "itemWebUrl": "https://x", "platform": None},
            "price": 100.0, "item_price": 90.0, "shipping_cost": 10.0,
        }
        message = self._send_and_capture(result)
        self.assertNotIn("seller:", message)

    def test_verify_sold_comps_link_appears_when_search_query_present(self):
        # Feature 2: the one-tap sold-comps link rides on the raw saved
        # search query, with exclusions stripped and terms URL-encoded.
        result = {
            "listing": {"title": "Canali Suit", "itemWebUrl": "https://x", "platform": None},
            "price": 100.0, "item_price": 90.0, "shipping_cost": 10.0,
            "search_query": "canali suit -navy",
        }
        message = self._send_and_capture(result)
        self.assertIn(
            "verify: https://www.ebay.com/sch/i.html?_nkw=canali+suit&LH_Sold=1&LH_Complete=1",
            message,
        )


class EbaySoldCompsUrl(unittest.TestCase):
    """Feature 2 helper: build eBay's public sold/completed-listings search
    URL so the AI's resale/retail estimates are independently checkable in
    one tap (per explicit user instruction) - a normal no-auth results page,
    not an API call."""

    def test_normal_query_strips_exclusions_and_url_encodes(self):
        url = m.ebay_sold_comps_url("zenith watch -tv -radio -canteen")
        self.assertEqual(
            url,
            "https://www.ebay.com/sch/i.html?_nkw=zenith+watch&LH_Sold=1&LH_Complete=1",
        )

    def test_quoted_phrase_is_percent_encoded(self):
        url = m.ebay_sold_comps_url('ralph lauren "purple label"')
        self.assertEqual(
            url,
            "https://www.ebay.com/sch/i.html?_nkw=ralph+lauren+%22purple+label%22&LH_Sold=1&LH_Complete=1",
        )

    def test_empty_or_falsy_query_returns_none(self):
        self.assertIsNone(m.ebay_sold_comps_url(""))
        self.assertIsNone(m.ebay_sold_comps_url(None))
        self.assertIsNone(m.ebay_sold_comps_url("   "))


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
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE marketplace_counts (platform TEXT, run_ts TEXT, count INTEGER)")
        conn.execute("CREATE TABLE marketplace_anomaly_notified (platform TEXT PRIMARY KEY, last_notified_ts TEXT)")
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
            result = m.prefetch_marketplaces(datetime.now(timezone.utc), conn)
        finally:
            conn.close()
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


class MarketplaceAnomalyDetection(unittest.TestCase):
    """The bot has gone completely silent before: a scraper's JSON shape
    drifted (a renamed field), it returned 0 listings for hours, and nobody
    noticed until a manual check. prefetch_marketplaces() records per-run
    counts per platform and must ntfy-alert on a real collapse - exactly
    once, only once there's history, only if it hasn't already notified
    recently, and never on a healthy run."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = sqlite3.connect(f"{self.tmpdir}/test.db")
        self.conn.execute("CREATE TABLE marketplace_counts (platform TEXT, run_ts TEXT, count INTEGER)")
        self.conn.execute("CREATE TABLE marketplace_anomaly_notified (platform TEXT PRIMARY KEY, last_notified_ts TEXT)")

    def tearDown(self):
        self.conn.close()

    def _seed_history(self, platform, counts):
        """Insert prior-run rows for a platform, newest last, all strictly
        before 'now' so they count as history for the current run."""
        base = datetime.now(timezone.utc) - timedelta(minutes=10)
        for i, c in enumerate(counts):
            ts = (base - timedelta(minutes=i * 5)).isoformat()
            self.conn.execute(
                "INSERT INTO marketplace_counts (platform, run_ts, count) VALUES (?, ?, ?)",
                (platform, ts, c),
            )
        self.conn.commit()

    def _run_prefetch(self, platform, listings_per_search):
        """Drive a real prefetch_marketplaces() for one platform that returns
        exactly `listings_per_search` listings per search; returns the mocked
        notify_bot_down for assertions."""
        now = datetime.now(timezone.utc)
        orig_adapters, orig_batch = dict(p.ADAPTERS), dict(p.BATCH_ADAPTERS)
        orig_searches, orig_enabled = m.SAVED_SEARCHES, m.MARKETPLACES_ENABLED
        try:
            p.ADAPTERS.clear()
            p.BATCH_ADAPTERS.clear()
            p.ADAPTERS[platform] = lambda s: (
                [{"itemId": f"{platform}:{i}", "title": "test item", "seller": {}} for i in range(listings_per_search)],
                listings_per_search,
            )
            m.MARKETPLACES_ENABLED = [platform]
            m.SAVED_SEARCHES = [
                {"query": "test watch", "enabled": True, "platforms": [platform]}
            ]
            with mock.patch.object(m, "notify_bot_down") as mock_notify:
                m.prefetch_marketplaces(now, self.conn)
            return mock_notify
        finally:
            p.ADAPTERS.clear()
            p.ADAPTERS.update(orig_adapters)
            p.BATCH_ADAPTERS.clear()
            p.BATCH_ADAPTERS.update(orig_batch)
            m.SAVED_SEARCHES = orig_searches
            m.MARKETPLACES_ENABLED = orig_enabled

    def test_zero_count_drop_with_history_notifies_once(self):
        self._seed_history("poshmark", [40, 45, 50, 45, 48, 42, 46, 44, 47, 43])
        mock_notify = self._run_prefetch("poshmark", 0)
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][0]
        self.assertIn("poshmark", msg)
        self.assertIn("0 listings", msg)
        self.assertIn("baseline ~45", msg)

    def test_not_enough_history_never_notifies_even_on_zero(self):
        self._seed_history("poshmark", [45, 48, 47])  # only 3 prior rows < 5
        mock_notify = self._run_prefetch("poshmark", 0)
        mock_notify.assert_not_called()

    def test_already_notified_within_6h_does_not_notify_again(self):
        self._seed_history("poshmark", [40, 45, 50, 45, 48, 42, 46, 44, 47, 43])
        self.conn.execute(
            "INSERT INTO marketplace_anomaly_notified (platform, last_notified_ts) VALUES (?, ?)",
            ("poshmark", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),
        )
        self.conn.commit()
        mock_notify = self._run_prefetch("poshmark", 0)
        mock_notify.assert_not_called()

    def test_normal_run_in_line_with_baseline_never_notifies(self):
        self._seed_history("poshmark", [40, 45, 50, 45, 48, 42, 46, 44, 47, 43])
        mock_notify = self._run_prefetch("poshmark", 45)
        mock_notify.assert_not_called()

    def test_batch_only_platform_zero_drop_notifies(self):
        """facebook lives only in BATCH_ADAPTERS, so `active` (filtered to
        ADAPTERS) used to exclude it from anomaly checks - a zero-count
        collapse went unnoticed even though counts[facebook] is populated.
        The check must cover every enabled platform, not just the
        per-search-queue ones."""
        self._seed_history("facebook", [40, 45, 50, 45, 48, 42, 46, 44, 47, 43])
        now = datetime.now(timezone.utc)
        orig_adapters, orig_batch = dict(p.ADAPTERS), dict(p.BATCH_ADAPTERS)
        orig_searches, orig_enabled = m.SAVED_SEARCHES, m.MARKETPLACES_ENABLED
        try:
            p.ADAPTERS.clear()
            p.BATCH_ADAPTERS.clear()
            p.ADAPTERS["poshmark"] = lambda s: (
                [{"itemId": "poshmark:1", "title": "test item", "seller": {}} for _ in range(45)],
                45,
            )
            p.BATCH_ADAPTERS["facebook"] = lambda searches: {}  # silent collapse
            m.MARKETPLACES_ENABLED = ["poshmark", "facebook"]
            m.SAVED_SEARCHES = [
                {"query": "test watch", "enabled": True, "platforms": ["poshmark", "facebook"]}
            ]
            with mock.patch.object(m, "notify_bot_down") as mock_notify:
                m.prefetch_marketplaces(now, self.conn)
            mock_notify.assert_called_once()
            self.assertIn("facebook", mock_notify.call_args[0][0])
            self.assertIn("0 listings", mock_notify.call_args[0][0])
        finally:
            p.ADAPTERS.clear()
            p.ADAPTERS.update(orig_adapters)
            p.BATCH_ADAPTERS.clear()
            p.BATCH_ADAPTERS.update(orig_batch)
            m.SAVED_SEARCHES = orig_searches
            m.MARKETPLACES_ENABLED = orig_enabled


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SaneAiPrice(unittest.TestCase):
    """The AI's JSON price fields were trusted as-is. Two real risks:
    a string ("$1,200") flows into clamp_watch_resale_estimate()'s numeric
    comparison and raises TypeError, killing the ENTIRE run since nothing
    catches it; and a negative value inverts compute_deal_rating()'s math
    - (-100 - 50) / -100 = +1.5 - fabricating a 150% "Steal" out of
    nonsense."""

    def test_parses_real_near_miss_formats(self):
        self.assertEqual(m._sane_ai_price(1200), 1200.0)
        self.assertEqual(m._sane_ai_price("$1,200"), 1200.0)
        self.assertEqual(m._sane_ai_price("1200 USD"), 1200.0)
        self.assertEqual(m._sane_ai_price("  950  "), 950.0)

    def test_rejects_values_that_are_not_a_usable_price(self):
        for bad in (None, True, -100, 0, "abc", "", "1.2.3", float("inf"), float("nan")):
            self.assertIsNone(m._sane_ai_price(bad), repr(bad))

    def test_negative_resale_can_no_longer_fabricate_a_steal(self):
        # The raw arithmetic really does produce a bogus Steal...
        self.assertEqual(m.compute_deal_rating(50, -100), ("Steal", 150))
        # ...so the sanitizer must stop it ever reaching that call.
        self.assertEqual(
            m.compute_deal_rating(50, m._sane_ai_price(-100)), (None, None)
        )

    def test_string_price_does_not_crash_the_watch_clamp(self):
        band = m.watch_price_band("Movado Museum Watch")
        self.assertIsNotNone(band)
        with self.assertRaises(TypeError):
            m.clamp_watch_resale_estimate("$1,200", band)
        self.assertEqual(
            m.clamp_watch_resale_estimate(m._sane_ai_price("$1,200"), band), band[2]
        )


class CircuitBreakerResilience(unittest.TestCase):
    """The breaker decides whether the bot talks to eBay at all for the
    next 30-120 minutes, and its state file is committed by the workflow
    on every run - so a partial write during a rebase/conflict is a real
    way to get a corrupt file. Two failure modes, both silent-total-outage
    shaped (the Aug 9 incident): a non-dict state crashed every caller via
    state.get(), and an absurd blocked_until_ts locked eBay out forever
    with no recovery path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = pathlib.Path(self.tmpdir) / "state.json"
        self._orig = m.EBAY_RATE_LIMIT_STATE_PATH
        m.EBAY_RATE_LIMIT_STATE_PATH = self.path

    def tearDown(self):
        m.EBAY_RATE_LIMIT_STATE_PATH = self._orig

    def test_corrupt_or_non_dict_state_never_crashes(self):
        for bad in ("[1,2]", '"hello"', "42", "null", "{bad json"):
            self.path.write_text(bad, encoding="utf-8")
            self.assertTrue(m.ebay_circuit_breaker_allows_calls("tok"), bad)

    def test_absurd_lockout_does_not_block(self):
        # This test previously asserted the OPPOSITE (that the first run
        # stays blocked) and so encoded the bug: the old clamp blocked for
        # a fresh full backoff on EVERY run without persisting anything,
        # which is a permanent lockout. An impossible timestamp is corrupt
        # data, not a real cooldown, so the correct behavior is to clear
        # it and allow calls. See CircuitBreakerCorruptTimestampSelfHeals
        # for the repeated-run coverage that would have caught it.
        far_future = time.time() + 10 * 365 * 24 * 3600
        self.path.write_text(
            json.dumps({"blocked_until_ts": far_future, "consecutive_429_streak": 1}),
            encoding="utf-8",
        )
        self.assertTrue(m.ebay_circuit_breaker_allows_calls("tok"))

    def test_normal_expired_lockout_allows_calls_again(self):
        self.path.write_text(
            json.dumps({"blocked_until_ts": time.time() - 60, "consecutive_429_streak": 2}),
            encoding="utf-8",
        )
        self.assertTrue(m.ebay_circuit_breaker_allows_calls("tok"))


class RunIntegration(unittest.TestCase):
    """run() is the orchestrator every other function in this file feeds
    into, and until now it had ZERO direct tests - which is exactly where
    the two worst production bugs of the Aug 2026 session lived, both
    shipping green:

    (a) the loro piana/cucinelli and knitwear gate bars returned a
        PERMANENT "below Steal" rejection for the pre-AI state
        (deal_rating None, meaning "not checked yet"), so PASS 3's
        pre-AI skip discarded and mark_seen'd every Loro Piana /
        Brunello Cucinelli / knitwear candidate before a single photo was
        ever looked at. Every unit test of the gate passed - none of them
        exercised the pre-AI state through the code path that acts on it.

    (b) review_candidates became a dict keyed by item_id (to dedupe an
        item arriving from both a brand search and the auction lane), but
        one line still iterated it as a list of dicts - an AttributeError
        that kills the entire run. Nothing caught it because run() was
        never called by anything but production.

    So these call the REAL run() with only its outer edges mocked: eBay,
    Gemini, ntfy, the gap report, and the three state files. Everything
    between - PASS 1's free filters, PASS 2's priority sort, PASS 3's
    budget/gate/alert logic - is the code under test."""

    # A real eBay Browse API item_summary, trimmed to the fields
    # search_ebay() actually parses (shape copied from a live response and
    # from the records in alerts_log.jsonl).
    def _ebay_item(self, item_id, title, price, *, seller="mensweardepot",
                   feedback_score=1204, feedback_pct="99.4", shipping=0.0):
        end_date = datetime.now(timezone.utc) + timedelta(hours=72)
        return m._attach_seller_feedback({
            "itemId": item_id,
            "title": title,
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "itemWebUrl": f"https://www.ebay.com/itm/{item_id.split('|')[1]}",
            "image": {"imageUrl": "https://i.ebayimg.com/images/g/9tkAAeSw/s-l1600.jpg"},
            "seller": {
                "username": seller,
                "feedbackScore": feedback_score,
                "feedbackPercentage": feedback_pct,
            },
            "buyingOptions": ["FIXED_PRICE"],
            "itemEndDate": end_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "shippingOptions": [
                {"shippingCost": {"value": f"{shipping:.2f}", "currency": "USD"}}
            ],
        })

    def _auction_item(self, item_id, title, price, minutes_remaining, bid_count=0):
        """A Browse API item_summary as search_ebay_ending_soon_auctions()
        returns it: a normal eBay item plus the closing-window tags run()
        reads to route it through the auction lane."""
        item = self._ebay_item(item_id, title, price)
        item["is_ending_soon_auction"] = True
        item["auction_minutes_remaining"] = minutes_remaining
        item["bid_count"] = bid_count
        return item

    # What check_photos_with_gemini() returns on a clean, well-priced item:
    # no damage, no third-party logo, and a resale estimate far above ask.
    AI_STEAL = {
        "damage_found": False,
        "weird_logo_found": False,
        "looks_good": True,
        "summary": "navy cashmere crewneck, no damage or third-party logos visible",
        "estimated_retail_price": 1495,
        "estimated_resale_value": 900,
        "price_confidence": "high",
        "fabric_from_tag": "100% cashmere",
        "fabric_confidence": "high",
        "liquidity": "fast",
        "peter_millar_back_crown_visible": None,
    }

    def _patch(self, name, value):
        patcher = mock.patch.object(m, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.tmpdir = pathlib.Path(tempfile.mkdtemp())
        self.alerts = []       # send_alert() calls, instead of a real push
        self.ai_calls = []     # listings check_photos_with_gemini() saw

        self._patch("DB_PATH", str(self.tmpdir / "seen.db"))
        self._patch("ALERTS_LOG_PATH", self.tmpdir / "alerts_log.jsonl")
        self._patch("EBAY_RATE_LIMIT_STATE_PATH", self.tmpdir / "rate_limit_state.json")
        # Fast and deterministic: no inter-call backoff, no config drift on
        # the AI budget, no auction lane unless a test asks for one.
        self._patch("GEMINI_INTER_CALL_SLEEP_SECONDS", 0)
        self._patch("GEMINI_CALL_LIMIT", 3)
        self._patch("EBAY_AUCTION_SEARCHES", [])

        # Every external edge. Anything left unpatched here is a real
        # network call, which is what makes this suite safe to run anywhere.
        self._patch("get_ebay_token", lambda: "fake-oauth-token")
        self._patch("get_ebay_rate_limit_remaining", lambda token: (5000, 5000))
        self._patch("fetch_gap_report", lambda: None)
        self._patch("fetch_ebay_item_description", lambda token, item_id: None)
        self._patch("fetch_vinted_item_description", lambda url: None)
        self._patch("prefetch_marketplaces", lambda now, conn: {})
        self._patch("search_ebay_ending_soon_auctions", lambda token, search: ([], None))
        self._patch("notify_bot_down", lambda message: None)
        self._patch("send_alert", self.alerts.append)
        self._patch("check_photos_with_gemini", self._fake_ai)
        # The new pre-alert DeepSeek sanity pass runs against every alerting
        # candidate. Patch its text-only call so no existing run test makes a
        # real network call; the default verdict is "complete item", so the
        # pass is a no-op unless an individual test overrides it.
        self._patch("_call_deepseek_text_json",
                    lambda prompt: {"is_complete_item": True,
                                    "is_part_or_accessory": False,
                                    "reason": "test default: complete item"})

        self.ai_result = dict(self.AI_STEAL)

    def _fake_ai(self, listing, category="other", current_month_name=None):
        self.ai_calls.append(listing.get("itemId"))
        return self.ai_result

    def _serve(self, saved_search, listings, total_listings=42):
        """Point the (single, so rotation is a no-op) saved search at a
        fixed set of listings. search_ebay() returns a (listings, total)
        tuple - the same shape run() unpacks."""
        self._patch("SAVED_SEARCHES", [saved_search])
        self._patch("search_ebay", lambda token, search: (list(listings), total_listings))

    def _alert_log_records(self):
        path = m.ALERTS_LOG_PATH
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _db(self):
        """Fresh connection to the run's DB file - run() closes its own, and
        anything not committed by then is genuinely gone (see
        test_mark_ai_pending_actually_commits)."""
        conn = sqlite3.connect(m.DB_PATH)
        self.addCleanup(conn.close)
        return conn

    def test_run_completes_without_error_on_realistic_input(self):
        # The smoke test that would have caught bug (b): run() is called
        # end-to-end with candidates that actually reach PASS 2's sort and
        # PASS 3's spend loop, which is where the dict-iterated-as-a-list
        # AttributeError lived. A crash there kills the whole run, so
        # "completed AND every listing reached a disposition" is the assertion.
        listings = [
            self._ebay_item("v1|364512889011|0",
                            "Loro Piana Cashmere Crewneck Sweater Mens Medium Navy", 180.0),
            self._ebay_item("v1|297183440152|0",
                            "Loro Piana Merino Sweater Mens Medium - moth holes on sleeve",
                            95.0, seller="atticfinds77"),
            self._ebay_item("v1|156004112938|0",
                            "Brunello Cucinelli Cashmere Sweater Mens Large Grey",
                            250.0, seller="milanoresale", shipping=8.0),
        ]
        self._serve({"query": "loro piana sweater", "max_price": 400,
                     "category_id": "11484", "enabled": True, "profile": "fast"},
                    listings)

        m.run()

        logged = {r["item_id"]: r for r in self._alert_log_records()}
        self.assertEqual(len(logged), 3, "every listing must reach a real disposition")
        # The moth-hole listing hard-fails in score_listing() before any AI
        # call; the other two are REVIEW candidates that reach PASS 3.
        self.assertEqual(logged["v1|297183440152|0"]["verdict"], "PASS")
        self.assertEqual(len(self.ai_calls), 2, "both REVIEW candidates get an AI check")
        self.assertFalse(m.is_new(self._db(), "v1|297183440152|0"),
                         "a hard-failed listing is a final disposition - mark it seen")

    def test_knitwear_candidate_needing_ai_is_also_not_discarded(self):
        # Sibling of the test below, deliberately routed through a DIFFERENT
        # gate bar. "loro piana sweater" hits the loro piana/cucinelli bar,
        # which is checked BEFORE the knitwear bar - so that test alone
        # leaves the knitwear path completely unguarded. Proven by mutation
        # testing: deleting the knitwear fix left the whole suite green,
        # while deleting the loro piana fix correctly failed it. Both bars
        # had the identical defect and both need their own regression.
        self._patch("GEMINI_CALL_LIMIT", 0)
        item_id = "v1|364512889022|0"
        self._serve({"query": "johnstons elgin cashmere", "max_price": 400,
                     "category_id": "11484", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Johnstons of Elgin Cashmere Sweater Mens Large Grey",
                                     120.0)])

        m.run()

        self.assertEqual(self.ai_calls, [], "budget was zero - no AI call should have happened")
        conn = self._db()
        self.assertTrue(
            m.is_new(conn, item_id),
            "a knitwear candidate that never got its AI check must NOT be marked "
            "seen - that discards it forever after zero evaluation",
        )
        self.assertIn(
            item_id, m.get_ai_pending_minutes(conn, [item_id]),
            "it must be parked in the ai_pending backlog to win an AI slot later",
        )

    def test_candidate_needing_ai_is_not_discarded_before_being_checked(self):
        # Regression for bug (a). With the AI budget at zero, a Loro Piana
        # knitwear candidate gets NO photo check this run - and must
        # therefore stay retryable, not be thrown away permanently. The bug
        # was the gate returning a permanent "deal_rating 'None' below
        # Steal" for the not-checked-yet state, which PASS 3's pre-AI skip
        # then acted on: verdict PASS, mark_seen, gone forever. Every
        # Loro Piana / Cucinelli / knitwear search could never alert at all.
        self._patch("GEMINI_CALL_LIMIT", 0)
        item_id = "v1|364512889011|0"
        self._serve({"query": "loro piana sweater", "max_price": 400,
                     "category_id": "11484", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Loro Piana Cashmere Crewneck Sweater Mens Medium Navy",
                                     180.0)])

        m.run()

        self.assertEqual(self.ai_calls, [], "budget was zero - no AI call should have happened")
        conn = self._db()
        self.assertTrue(
            m.is_new(conn, item_id),
            "a candidate that never got its AI check must NOT be marked seen - "
            "marking it seen discards it forever after zero evaluation",
        )
        self.assertIn(
            item_id, m.get_ai_pending_minutes(conn, [item_id]),
            "it must be parked in the ai_pending backlog so PASS 2 can age it "
            "up and win it an AI slot on a later run",
        )
        # And it left a diagnosable trace with the retry-eligible marker,
        # not a permanent rejection reason.
        (record,) = self._alert_log_records()
        self.assertIn("no AI price", record["reason"])

    def test_alert_requires_a_real_ai_check(self):
        # "every alert must be AI-vetted" - a failed/abstaining AI call
        # (None) is not a vetted check, so even a grab_on_sight-tier
        # candidate that would otherwise blind-trust straight through the
        # gate must not alert. Real report behind the rule: 10 of 15 suit
        # alerts in one flood had deal_rating None - zero price evidence,
        # alerted purely on brand tier.
        self.ai_result = None
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        self.assertEqual(len(self.ai_calls), 1, "the AI check must actually be attempted")
        self.assertEqual(self.alerts, [], "no AI verdict means no alert, brand tier notwithstanding")
        (record,) = self._alert_log_records()
        self.assertIn("no AI price", record["reason"])

    def test_alert_fires_when_ai_confirms_a_steal(self):
        # The other side of the same gate: a real AI check that confirms a
        # steal (900 resale vs a 212 landed cost, high confidence, no
        # damage) must actually reach send_alert - exactly once.
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        self.assertEqual(len(self.alerts), 1, "an AI-confirmed steal must alert exactly once")
        alerted = self.alerts[0]
        self.assertEqual(alerted["deal_rating"], "Steal")
        self.assertEqual(alerted["listing"]["itemId"], item_id)
        self.assertFalse(m.is_new(self._db(), item_id), "an alerted item is seen")

    def test_deepseek_sanity_suppresses_part_accessory_only(self):
        # The vision AI check passed (self.ai_result is AI_STEAL), but the
        # cheap text-only DeepSeek pass catches what the photos don't
        # disclose - here a watch strap sold as if it were the watch. The
        # alert must be suppressed, logged as PASS, and the item marked seen.
        sanity_calls = []
        self._patch("_call_deepseek_text_json",
                    lambda prompt: sanity_calls.append(prompt) or {
                        "is_complete_item": False,
                        "is_part_or_accessory": True,
                        "reason": "listing is a watch strap only, not the watch",
                    })
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Shoe Strap - Accessory Only",
                                     200.0)])

        m.run()

        self.assertEqual(self.alerts, [], "a part/accessory-only listing must not alert")
        self.assertEqual(len(sanity_calls), 1,
                         "the sanity check must run on a candidate that reached the alert point")
        (record,) = self._alert_log_records()
        self.assertEqual(record["verdict"], "PASS")
        self.assertIn("DeepSeek sanity check", record["reason"])
        self.assertFalse(m.is_new(self._db(), item_id), "a suppressed listing is marked seen")

    def test_deepseek_sanity_passes_complete_item_still_alerts(self):
        # A complete, wearable item clears the sanity pass and alerts
        # exactly as before - the pass is a filter, not a new gate.
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        self.assertEqual(len(self.alerts), 1, "a complete item still alerts normally")
        self.assertEqual(self.alerts[0]["listing"]["itemId"], item_id)

    def test_deepseek_sanity_exception_fails_open(self):
        # A DeepSeek API hiccup must never be the reason a real steal gets
        # suppressed - this is a bonus filter, not a required gate. The text
        # call raising (timeout, 5xx, bad JSON) still lets the alert fire.
        self._patch("_call_deepseek_text_json",
                    mock.Mock(side_effect=requests.exceptions.Timeout("deepseek down")))
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        self.assertEqual(len(self.alerts), 1, "a failed sanity check fails OPEN - the alert still fires")
        self.assertEqual(self.alerts[0]["listing"]["itemId"], item_id)

    def test_deepseek_second_opinion_lowers_medium_confidence_estimate(self):
        # The vision AI's medium-confidence resale guess is the weakest link
        # in the alert path, so a text-only DeepSeek second opinion re-
        # estimates from the same evidence. When DeepSeek's number is more
        # conservative, it must win and drive the deal rating.
        self.ai_result = dict(self.AI_STEAL)
        self.ai_result["price_confidence"] = "medium"
        self.ai_result["estimated_resale_value"] = 900
        self.ai_result["visible_brand_evidence"] = "Leather insole stamps 'Alden'"

        def _dispatch(prompt):
            if "estimated_resale_value" in prompt:
                return {"estimated_resale_value": 600, "reasoning": "text evidence suggests lower"}
            return {"is_complete_item": True, "is_part_or_accessory": False, "reason": "ok"}
        self._patch("_call_deepseek_text_json", _dispatch)
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0]["estimated_resale_value"], 600,
                         "the more conservative second-opinion estimate must win")
        self.assertEqual(self.alerts[0]["deal_rating"], "Great Deal",
                         "600 resale vs 200 ask is a 66% discount - Great Deal, not the old Steal")
        self.assertTrue(
            any("second opinion" in (f or "").lower() for f in self.alerts[0].get("flags", [])),
            "the adjustment must leave a trace in the alert flags",
        )

    def test_deepseek_second_opinion_skipped_for_high_confidence(self):
        # A high-confidence vision result is not "borderline" - the second
        # opinion must not run at all, and the estimate passes through
        # untouched.
        second_opinion = self._patch("_deepseek_second_opinion", mock.Mock())
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        second_opinion.assert_not_called()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0]["estimated_resale_value"], 900)
        self.assertEqual(self.alerts[0]["deal_rating"], "Steal")

    def test_deepseek_second_opinion_skipped_when_comp_overrides(self):
        # Real Grailed sold comps (median 700, n=6) already override a
        # medium-confidence AI guess before the rating is computed - a second
        # opinion on a number that's about to be replaced is wasted, so it
        # must not be called at all.
        self.ai_result = dict(self.AI_STEAL)
        self.ai_result["price_confidence"] = "medium"
        self.ai_result["estimated_resale_value"] = 900
        second_opinion = self._patch("_deepseek_second_opinion", mock.Mock())
        item_id = "v1|375288104417|0"
        item = self._ebay_item(item_id,
                               "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                               200.0)
        item["sold_comp_median"] = 700.0
        item["sold_comp_count"] = 6
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [item])

        m.run()

        second_opinion.assert_not_called()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0]["estimated_resale_value"], 700,
                         "the real sold-comp median must win over the AI guess")

    def test_deepseek_second_opinion_exception_fails_open(self):
        # A DeepSeek API hiccup (timeout/5xx/bad JSON) must never change the
        # estimate or block an alert - the second opinion fails OPEN, exactly
        # like the sanity pass.
        self.ai_result = dict(self.AI_STEAL)
        self.ai_result["price_confidence"] = "medium"
        self.ai_result["estimated_resale_value"] = 900
        self._patch("_call_deepseek_text_json",
                    mock.Mock(side_effect=requests.exceptions.Timeout("deepseek down")))
        item_id = "v1|375288104417|0"
        self._serve({"query": "alden shoes", "max_price": 400,
                     "category_id": "24087", "enabled": True, "profile": "fast"},
                    [self._ebay_item(item_id,
                                     "Alden Shell Cordovan Longwing Blucher 10 D Color 8",
                                     200.0)])

        m.run()

        self.assertEqual(len(self.alerts), 1, "a failed second opinion fails OPEN - the alert still fires")
        self.assertEqual(self.alerts[0]["estimated_resale_value"], 900,
                         "the original estimate must be untouched")
        self.assertEqual(self.alerts[0]["deal_rating"], "Steal")

    def test_mid_run_429_also_stops_the_auction_lane(self):
        # ebay_circuit_closed is computed ONCE before PASS 1. A real 429 on
        # a regular search mid-loop trips the breaker and clears
        # ebay_this_run (stopping further REGULAR searches), but the local
        # ebay_circuit_closed flag was never updated - so the always-on
        # auction branch, which checks THAT flag, kept firing up to 3 more
        # eBay calls into the lockout that had just been declared.
        auction_calls = []
        self._patch("search_ebay_ending_soon_auctions",
                    lambda token, search: auction_calls.append(search["query"]) or ([], None))
        self._patch("EBAY_AUCTION_SEARCHES",
                    [{"query": "canali suit", "category_id": "11484",
                      "max_price": 400, "enabled": True}])
        self._patch("SAVED_SEARCHES",
                    [{"query": "canali suit", "max_price": 400,
                      "category_id": "11484", "enabled": True, "profile": "fast"}])

        def _raise_429(token, search):
            resp = mock.Mock()
            resp.status_code = 429
            raise requests.exceptions.HTTPError(response=resp)

        self._patch("search_ebay", _raise_429)

        m.run()

        self.assertEqual(auction_calls, [],
                         "a mid-run 429 must also stop the auction lane, not just the regular rotation")

    def test_ending_soon_auction_gets_a_reserved_ai_slot(self):
        # Every alert needs a real AI check, and GEMINI_CALL_LIMIT paces
        # that to a handful per run. An auction closing in minutes that
        # loses that budget race is mark_ai_pending()'d to a "next run"
        # that, for something closing in under 5 minutes, never arrives -
        # the one thing the auction lane exists to catch, silently lost.
        # The reserved slot guarantees at least one AI call still reaches an
        # ending-soon auction even once the normal budget is spent.
        self._patch("GEMINI_CALL_LIMIT", 0)
        self._patch("SAVED_SEARCHES", [])
        self._patch("EBAY_AUCTION_SEARCHES",
                    [{"query": "canali suit", "category_id": "11484",
                      "max_price": 400, "enabled": True}])
        self._patch("search_ebay_ending_soon_auctions",
                    lambda token, search: (
                        [self._auction_item("v1|1|0", "Canali Suit 42R", 200.0, 3)], None
                    ))

        m.run()

        self.assertEqual(len(self.ai_calls), 1,
                         "a closing auction must get an AI check even when the normal budget is zero")
        self.assertEqual(len(self.alerts), 1, "an AI-confirmed closing auction must alert")

    def test_reserved_auction_slot_is_capped_and_lost_auctions_logged(self):
        # The reservation must NOT let auctions eat the whole AI budget when
        # several are closing at once - it's capped at AUCTION_AI_RESERVED_CALLS,
        # and the auctions that still get deferred past their closing time
        # are logged (a permanent miss, not a silent drop).
        self._patch("GEMINI_CALL_LIMIT", 0)
        self._patch("SAVED_SEARCHES", [])
        self._patch("EBAY_AUCTION_SEARCHES",
                    [{"query": "canali suit", "category_id": "11484",
                      "max_price": 400, "enabled": True}])
        self._patch("search_ebay_ending_soon_auctions",
                    lambda token, search: (
                        [
                            self._auction_item("v1|1|0", "Canali Suit 42R", 200.0, 1),
                            self._auction_item("v1|2|0", "Canali Suit 42R", 200.0, 2),
                            self._auction_item("v1|3|0", "Canali Suit 42R", 200.0, 3),
                        ], None
                    ))

        with self.assertLogs("ebay_deal_alert", level="WARNING") as cm:
            m.run()

        self.assertEqual(len(self.ai_calls), 1,
                         "the reserved slot is capped at one - many auctions must not eat the whole budget")
        self.assertEqual(self.ai_calls, ["v1|1|0"],
                         "only the soonest-closing auction gets the reserved check")
        self.assertTrue(
            any("Losing ending-soon auction" in line for line in cm.output),
            "an auction deferred past its closing time must be logged, not silently dropped",
        )


class WeeklyDigestCountsOnlyReviewAlerts(unittest.TestCase):
    """send_weekly_digest() counted EVERY record in alerts_log.jsonl as an
    "alert", but append_alert_log() writes both sent alerts (verdict REVIEW)
    and blocked/rejected candidates (verdict PASS). The weekly headline
    could therefore present a week heavy on blocked junk as if it were
    alerts the user actually received."""

    def _digest_message(self, records):
        captured = {}
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["message"] = data.decode("utf-8")
            return fake_resp

        with mock.patch.object(m, "_read_alert_log_records", return_value=records), \
             mock.patch("requests.post", side_effect=fake_post):
            m.send_weekly_digest()
        return captured["message"]

    def test_blocked_records_are_not_counted_as_alerts(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        records = [
            {"timestamp": ts, "verdict": "REVIEW", "deal_rating": "Steal", "query": "canali suit"},
            {"timestamp": ts, "verdict": "REVIEW", "deal_rating": "Great Deal", "query": "alden shoes"},
            {"timestamp": ts, "verdict": "PASS", "reason": "excluded gender keyword", "query": "canali suit"},
            {"timestamp": ts, "verdict": "PASS", "reason": "pet product, not menswear", "query": "barbour jacket"},
            {"timestamp": ts, "verdict": "PASS", "reason": "brand on pass list", "query": "canali suit"},
        ]
        message = self._digest_message(records)
        self.assertIn("2 alerts", message)
        self.assertNotIn("5 alerts", message)
        self.assertIn("3 blocked", message)

    def test_all_blocked_week_reports_zero_alerts(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = [
            {"timestamp": ts, "verdict": "PASS", "reason": "excluded gender keyword", "query": "canali suit"},
            {"timestamp": ts, "verdict": "PASS", "reason": "pet product, not menswear", "query": "barbour jacket"},
        ]
        message = self._digest_message(records)
        self.assertIn("0 alerts", message)
        self.assertIn("2 blocked", message)


class EbayTokenCacheWriteFailure(unittest.TestCase):
    """A token-cache WRITE failure used to propagate straight out of
    get_ebay_token() (read-only filesystem / full disk / permissions), and
    run() read it as a fatal token failure - "bot down" alert, early return
    - even though a perfectly valid token was already in hand. Caching is an
    optimization, never a hard requirement."""

    def test_write_failure_does_not_raise(self):
        # FileNotFoundError (a missing parent dir) is an OSError, the same
        # class as read-only/permission/disk-full on a real write.
        missing_parent = pathlib.Path(tempfile.mkdtemp()) / "does_not_exist" / "cache.json"
        with mock.patch.object(m, "TOKEN_CACHE_PATH", missing_parent):
            m._write_cached_ebay_token("fake-token", 7200)  # must not raise

    def test_token_fetch_returns_token_when_cache_write_fails(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = lambda: None
        fake_resp.json = lambda: {"access_token": "tok-123", "expires_in": 7200}
        missing_parent = pathlib.Path(tempfile.mkdtemp()) / "does_not_exist" / "cache.json"
        with mock.patch.object(m, "_read_cached_ebay_token", return_value=None), \
             mock.patch.object(m, "TOKEN_CACHE_PATH", missing_parent), \
             mock.patch("requests.post", return_value=fake_resp), \
             mock.patch.dict("os.environ", {"EBAY_CLIENT_ID": "client", "EBAY_CLIENT_SECRET": "secret"}):
            self.assertEqual(m.get_ebay_token(), "tok-123")


class AlertLogPriceSemantics(unittest.TestCase):
    """append_alert_log()'s `price` field meant two different things -
    landed cost (item + shipping + tax) on a REVIEW record, but the raw
    item price (no shipping/tax) on an early hard-fail PASS record that had
    no landed cost. Downstream readers (mobile app, weekly digest) couldn't
    tell which they were looking at."""

    def _write_and_read(self, result):
        tmpdir = pathlib.Path(tempfile.mkdtemp())
        orig = m.ALERTS_LOG_PATH
        m.ALERTS_LOG_PATH = tmpdir / "alerts_log.jsonl"
        try:
            m.append_alert_log(result)
            with m.ALERTS_LOG_PATH.open("r", encoding="utf-8") as f:
                return json.loads(f.read().strip())
        finally:
            m.ALERTS_LOG_PATH = orig

    def test_pass_record_price_is_none_item_price_is_raw(self):
        # Early hard-fail PASS: no landed cost computed. `price` must be
        # None (not silently the raw item price), and the raw item price
        # must be carried by item_price instead.
        result = {
            "listing": {"itemId": "v1|1|0", "title": "Women's Canali Sweater",
                        "price": {"value": 40.0, "currency": "USD"}},
            "verdict": "PASS",
            "reason": "excluded gender keyword in title/description",
        }
        record = self._write_and_read(result)
        self.assertIsNone(record["price"])
        self.assertEqual(record["item_price"], 40.0)

    def test_review_record_keeps_landed_price_and_raw_item_price(self):
        result = {
            "listing": {"itemId": "v1|2|0", "title": "Canali Suit",
                        "price": {"value": 80.0, "currency": "USD"}},
            "verdict": "REVIEW",
            "price": 100.0,      # landed cost
            "item_price": 80.0,  # raw item price
            "shipping_cost": 14.0,
        }
        record = self._write_and_read(result)
        self.assertEqual(record["price"], 100.0)
        self.assertEqual(record["item_price"], 80.0)


class CircuitBreakerCorruptTimestampSelfHeals(unittest.TestCase):
    """The clamp added to stop a permanent lockout CAUSED one. It clamped
    an impossible blocked_until_ts to now+max_backoff but never persisted
    it, so every later run re-read the same stale far-future value and
    re-clamped it to another full window - blocking eBay forever while
    logging a reassuring '~120 more min' each time. Worse than no clamp,
    which at least self-heals once real time passes.

    The original test only asserted the FIRST run was blocked, so it
    passed while the bug was live. This one runs the check repeatedly,
    which is the only thing that could have caught it."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = pathlib.Path(self.tmpdir) / "state.json"
        self._orig = m.EBAY_RATE_LIMIT_STATE_PATH
        m.EBAY_RATE_LIMIT_STATE_PATH = self.path

    def tearDown(self):
        m.EBAY_RATE_LIMIT_STATE_PATH = self._orig

    def test_corrupt_far_future_timestamp_does_not_block_forever(self):
        # Milliseconds written where seconds were expected - one of the
        # real triggers named in the code comment.
        self.path.write_text(
            json.dumps({"blocked_until_ts": 1750000000000, "consecutive_429_streak": 1}),
            encoding="utf-8",
        )
        # Must not block on ANY run, including repeated ones.
        for attempt in range(3):
            self.assertTrue(
                m.ebay_circuit_breaker_allows_calls("tok"),
                f"run {attempt + 1}: a corrupt timestamp must not lock eBay out",
            )

    def test_corrupt_state_is_cleared_so_it_cannot_recur(self):
        self.path.write_text(
            json.dumps({"blocked_until_ts": 1750000000000, "consecutive_429_streak": 4}),
            encoding="utf-8",
        )
        m.ebay_circuit_breaker_allows_calls("tok")
        state = m._read_ebay_rate_limit_state()
        self.assertEqual(state.get("blocked_until_ts"), 0)
        self.assertEqual(state.get("consecutive_429_streak"), 0)

    def test_a_genuine_backoff_is_still_respected(self):
        # A real, in-range cooldown must still block - the fix must not
        # have disabled the breaker outright.
        self.path.write_text(
            json.dumps({"blocked_until_ts": time.time() + 600, "consecutive_429_streak": 1}),
            encoding="utf-8",
        )
        self.assertFalse(m.ebay_circuit_breaker_allows_calls("tok"))


class SaneAiPriceNegativeStrings(unittest.TestCase):
    """_sane_ai_price stripped non-digits with re.sub(r"[^\d.]"), which
    ate the minus sign - so the STRING "-100" became 100.0, silently
    reintroducing the exact fabricated-"Steal" bug the function was
    written to prevent. Only the string path had the hole; the numeric
    branch was already correct, which is why the original tests (which
    only passed a numeric -100) missed it."""

    def test_negative_strings_are_rejected(self):
        for bad in ("-100", "-$1,200", "-0.5", "  -75 USD"):
            self.assertIsNone(m._sane_ai_price(bad), bad)

    def test_positive_strings_still_parse(self):
        self.assertEqual(m._sane_ai_price("$1,200"), 1200.0)
        self.assertEqual(m._sane_ai_price("1200 USD"), 1200.0)


class GluedSizeDefeatsJacketOnlyFilter(unittest.TestCase):
    """Real live miss, reported by the user: "Brioni Roma Wool Palatino
    Blazer42R Italy 3 Button Flaws" ALERTED as a Steal despite being a
    blazer with no pants - a standing no-standalone-jackets violation.

    Cause: SUIT_JACKET_ONLY_SIGNALS matches \bblazer\b, and the trailing
    word boundary cannot match when a digit is glued straight onto the
    word ("4" is a word character, so there is no boundary there).
    Sellers run the size onto the garment word constantly. This defeated
    EVERY jacket-only pattern at once, and is the same defect class as
    the old \b42\b-vs-"42R" size-matching bug."""

    def test_glued_size_no_longer_hides_a_jacket_only_listing(self):
        for title in (
            "Brioni Roma Wool Palatino Blazer42R Italy 3 Button Flaws",
            "Zegna Sport Coat42R Wool",
            "Kiton SuitJacket42R",
        ):
            self.assertTrue(
                m.is_jacket_only_suit_listing(title, "brioni suit"), title
            )

    def test_spaced_size_still_blocked(self):
        self.assertTrue(
            m.is_jacket_only_suit_listing("Canali Blazer 42R Navy", "canali suit")
        )

    def test_real_two_piece_suits_still_pass_with_glued_sizes(self):
        for title in (
            "Brioni Suit42R Wool Two Piece with Pants",
            "Loro Piana Suit42R pants included",
            "Kiton Suit 42R Jacket and Trousers",
        ):
            self.assertFalse(
                m.is_jacket_only_suit_listing(title, "kiton suit"), title
            )


class PhotoCheckProviderFallback(unittest.TestCase):
    def test_gemini_primary_falls_back_to_deepseek_on_failure(self):
        deepseek_result = {"damage_found": False, "looks_good": True}
        with mock.patch.object(m, "AI_PHOTO_PROVIDER", "gemini"), \
             mock.patch.object(m, "_call_gemini_json",
                               side_effect=requests.exceptions.RequestException("gemini down")) as gemini_mock, \
             mock.patch.object(m, "_call_deepseek_json", return_value=deepseek_result) as deepseek_mock:
            result = m._call_photo_check("prompt", [(b"img-bytes", "image/jpeg")], timeout=10)

        self.assertIs(result, deepseek_result)
        gemini_mock.assert_called_once()
        deepseek_mock.assert_called_once()
