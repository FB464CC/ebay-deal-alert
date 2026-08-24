"""Regression tests for the three previously-audited platforms.py defects.

Each guards a specific real failure mode:
  1. Vinted landed cost was understated by ~$5-10 (only the buyer-protection
     fee was counted as shipping), so Vinted listings won deal comparisons
     against Poshmark/ShopGoodwill's honest flat shipping.
  2. Grailed's Algolia batch POST bypassed get_json()'s 429 handling, so a
     single 429 blanked up to 50 sub-queries with no retry and no 429-specific
     log line.
  3. Chained ``(x or {}).get(k)`` parses raised AttributeError on a scalar
     value, which _fetch_marketplace() swallowed so an entire platform
     silently returned zero listings for that run.

Pure stdlib unittest + mock, matching the repo's "no scraper framework"
philosophy. Run with:
    python -m unittest test_platforms
"""
import unittest
from unittest import mock

import platforms as p


class _FakeResp:
    """Minimal stand-in for requests.Response - just what the code reads."""

    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class VintedLandedCost(unittest.TestCase):
    def test_assumed_shipping_constant_is_positive(self):
        self.assertGreater(p.VINTED_ASSUMED_SHIPPING, 0)

    def test_shipping_is_service_fee_plus_flat_assumption(self):
        # Regression: shipping used to be the buyer-protection fee ALONE, so a
        # Vinted item landed ~$5-10 cheaper than the same item elsewhere and
        # won comparisons it should have lost.
        body = {
            "items": [{
                "id": "1",
                "title": "Vintage Blazer",
                "price": {"amount": "100.00"},
                "total_item_price": {"amount": "106.70"},
                "url": "https://www.vinted.com/item/1",
                "photo": {"url": "https://img.example/x.jpg"},
                "photos": [],
                "size_title": "M",
                "user": {"login": "seller1"},
            }],
        }
        with mock.patch.object(p, "_get_vinted_session", return_value=object()), \
                mock.patch.object(p, "get_json", return_value=body):
            listings, _ = p.search_vinted({"query": "alden shell cordovan"})

        self.assertEqual(len(listings), 1)
        shipping = listings[0]["shippingOptions"][0]["shippingCost"]["value"]
        service_fee = 106.70 - 100.00
        self.assertAlmostEqual(shipping, service_fee + p.VINTED_ASSUMED_SHIPPING, places=6)


class GrailedBatch429(unittest.TestCase):
    def test_retries_once_on_429_then_uses_success_body(self):
        # Regression: a single 429 used to blank the whole chunk instantly.
        post = mock.patch("platforms.requests.post")
        sleep = mock.patch("platforms.time.sleep")
        pace = mock.patch.object(p, "_pace")
        with post as post_mock, sleep as sleep_mock, pace:
            post_mock.side_effect = [
                _FakeResp(429, text="rate limited"),
                _FakeResp(200, {"results": [{"hits": [{"objectID": "x"}]}]}),
            ]
            results = p._algolia_multi_query([{"indexName": "I", "params": "query=foo"}])

        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2)
        self.assertEqual(results[0]["hits"][0]["objectID"], "x")

    def test_persistent_429_logs_distinctly_and_drops_chunk(self):
        # Regression: no 429-specific signal existed, so a throttled run was
        # indistinguishable in the logs from any other failed batch call.
        post = mock.patch("platforms.requests.post", return_value=_FakeResp(429, text="rate limited"))
        sleep = mock.patch("platforms.time.sleep")
        pace = mock.patch.object(p, "_pace")
        with post, sleep, pace:
            with self.assertLogs("platforms", level="WARNING") as cm:
                results = p._algolia_multi_query([{"indexName": "I", "params": "query=foo"}])

        self.assertEqual(results, [None])
        joined = " ".join(cm.output)
        self.assertIn("429", joined)
        self.assertIn("rate limited", joined)


class DefensiveParsing(unittest.TestCase):
    def test_dget_scalar_receiver_returns_default(self):
        # Regression: (x or {}).get(k) raised AttributeError on a truthy
        # scalar; _dget must degrade to the default exactly like a missing key.
        self.assertIsNone(p._dget("not-a-dict", "url"))
        self.assertIsNone(p._dget(12345, "val"))
        self.assertIsNone(p._dget(None, "val"))
        self.assertEqual(p._dget("not-a-dict", "url", "fallback"), "fallback")

    def test_dget_dict_receiver_still_reads(self):
        self.assertEqual(p._dget({"a": 1}, "a"), 1)
        self.assertIsNone(p._dget({"a": 1}, "missing"))

    def test_grailed_hit_tolerates_scalar_nested_fields(self):
        # Regression: scalar cover_photo/user/shipping used to raise
        # AttributeError inside the adapter, silently zeroing Grailed's run.
        hit = {
            "objectID": "abc",
            "title": "Alden Shell Cordovan",
            "price": 500,
            "cover_photo": "scalar-not-a-dict",
            "user": "scalar-not-a-dict",
            "shipping": "scalar-not-a-dict",
        }
        listing = p._grailed_hit_to_listing(hit, None, 0)

        self.assertIsNotNone(listing)
        self.assertNotIn("image", listing)
        self.assertNotIn("shippingOptions", listing)
        self.assertIsNone(listing.get("seller"))
        self.assertFalse(listing["seller_trusted"])


class ShopGoodwillClosingSoon(unittest.TestCase):
    def test_seconds_only_remaining_is_not_unparseable(self):
        # Real live bug: confirmed live remainingTime values include bare
        # "46s" - with no seconds capture this had no d/h/m to match,
        # returned None, and got thrown out as "unparseable, don't trust
        # it" - silently dropping every auction in its final minute, the
        # exact down-to-the-wire case the closing-soon filter exists for.
        self.assertEqual(p._parse_shopgoodwill_remaining("46s"), 0)
        self.assertEqual(p._parse_shopgoodwill_remaining("1m46s"), 1)
        self.assertIsNone(p._parse_shopgoodwill_remaining(""))
        self.assertIsNone(p._parse_shopgoodwill_remaining(None))

    def test_string_numbids_does_not_crash_the_whole_search(self):
        # Real live bug: numBids compared with `> 0` while every other
        # numeric field in this response goes through _to_float first.
        # ShopGoodwill's own request payload is all-strings ("pageSize":
        # "40" etc.), so a stringly-typed numBids in the response is
        # plausible - `"3" > 0` raises TypeError, uncaught here, which
        # _fetch_marketplace's blanket except turns into a silently empty
        # result for the ENTIRE search, not just this one item.
        body = {"searchResults": {"items": [{
            "itemId": "1", "title": "Test Item", "currentPrice": "10.00",
            "remainingTime": "5m", "numBids": "3", "assetsUrl": "http://x",
        }]}}
        with mock.patch.object(p, "get_json", return_value=None), \
                mock.patch("platforms.requests.post") as post_mock:
            post_mock.return_value = _FakeResp(200, body)
            listings, _ = p.search_shopgoodwill({"query": "test"})
        self.assertEqual(len(listings), 1)


if __name__ == "__main__":
    unittest.main()
