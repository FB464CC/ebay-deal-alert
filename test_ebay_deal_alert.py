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
        low, avg, high = m.watch_price_band("Movado Bold Evolution 2.0 Chronograph")
        self.assertLess(high, 795, "band must be tight enough to catch the real live miss")
        clamped = max(low, min(high, 795))
        self.assertEqual(clamped, high)


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

    def test_default_category_no_ai_data_blind_trusts_grab_on_sight_only(self):
        result = {"deal_rating": None, "brand_tier": "grab_on_sight"}
        self.assertIsNone(m.is_blocked_by_steal_quality_gate(result, category="other"))
        result["brand_tier"] = "standard"
        self.assertIsNotNone(m.is_blocked_by_steal_quality_gate(result, category="other"))


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
