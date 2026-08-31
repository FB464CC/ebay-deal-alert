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
import json
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms as p


class _FakeResp:
    """Minimal stand-in for requests.Response - just what the code reads."""

    def __init__(self, status_code, body=None, text="", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class _FakeScraplingResp:
    """Minimal stand-in for a scrapling Fetcher response - .status not
    .status_code, and no .ok property, unlike requests.Response."""

    def __init__(self, status, body=None, headers=None, cookies=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.cookies = cookies or {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class ListingNumberValidation(unittest.TestCase):
    def test_to_float_rejects_leading_minus_strings(self):
        self.assertIsNone(p._to_float("-10"))
        self.assertIsNone(p._to_float("-$12.50"))
        self.assertEqual(p._to_float("$12.50"), 12.5)

    def test_non_finite_boolean_and_non_positive_prices_are_rejected(self):
        for price in (True, float("nan"), float("inf"), float("-inf"), 0, -1):
            with self.subTest(price=price):
                self.assertIsNone(p.make_listing("test", "1", "Title", price, "https://example.test/1"))

    def test_negative_or_non_finite_shipping_cannot_reduce_landed_price(self):
        for shipping in (-10, float("nan"), float("inf"), True):
            with self.subTest(shipping=shipping):
                listing = p.make_listing(
                    "test", "1", "Title", 25, "https://example.test/1", shipping=shipping
                )
                self.assertNotIn("shippingOptions", listing)


class QueryExclusions(unittest.TestCase):
    def test_word_and_quoted_phrase_exclusions_are_removed_and_preserved(self):
        clean, excluded = p.split_query_exclusions(
            'golf club set -junior -"left hand" -lefty -"left handed"'
        )
        self.assertEqual(clean, "golf club set")
        self.assertEqual(excluded, ["junior", "left hand", "lefty", "left handed"])
        self.assertTrue(p.title_matches_exclusion("Mens left handed golf clubs", excluded))
        self.assertFalse(p.title_matches_exclusion("Mens right handed golf clubs", excluded))


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
        session = object()
        with mock.patch.object(p, "_get_vinted_session", return_value=session), \
                mock.patch.object(p, "_get_vinted_catalog_page", return_value=(body, session)), \
                mock.patch.object(p, "vinted_circuit_breaker_allows_calls", return_value=True):
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


class VintedAdaptiveBackoff(unittest.TestCase):
    """Real live pattern: measured against 4 actual GitHub Actions runs,
    Vinted was hitting 43-62 429s out of ~86 calls PER RUN - the fixed
    0.15s pace (tuned from a clean, uncontested single-threaded burst test)
    keeps hammering at the same rate for the rest of the run even after
    Vinted starts rejecting calls. This was a known, explicitly named
    ceiling (a `ponytail:` comment on _MIN_INTERVAL) until the 429s
    actually showed up in the logs for real."""

    def setUp(self):
        # Module-level dicts persist across tests in the same process -
        # isolate every test from whatever an earlier one left behind.
        p._backoff_multiplier.clear()
        p._last_call.clear()

    def test_register_rate_limit_doubles_and_caps(self):
        self.assertEqual(p._register_rate_limit("vinted"), 2)
        self.assertEqual(p._register_rate_limit("vinted"), 4)
        self.assertEqual(p._register_rate_limit("vinted"), 8)
        for _ in range(10):
            p._register_rate_limit("vinted")
        self.assertEqual(p._backoff_multiplier["vinted"], p._MAX_BACKOFF_MULTIPLIER)

    def test_backoff_is_per_platform_not_global(self):
        p._register_rate_limit("vinted")
        p._register_rate_limit("vinted")
        self.assertEqual(p._backoff_multiplier.get("poshmark", 1), 1)

    def test_pace_actually_scales_with_backoff_multiplier(self):
        # Mutation-catchable: proves _pace() reads the multiplier, not just
        # that _register_rate_limit tracks a number nobody consults. First
        # monotonic() call computes the wait (triggers one sleep); every
        # call after that returns a value far enough past the scaled
        # interval to let the loop exit on its next check, whatever the
        # exact call count turns out to be.
        p._last_call["vinted"] = 100.0  # "just called"
        p._backoff_multiplier["vinted"] = 4
        with mock.patch("platforms.time.monotonic", side_effect=[100.0] + [1000.0] * 10), \
             mock.patch("platforms.time.sleep") as sleep_mock:
            p._pace("vinted")
        sleep_mock.assert_called_once()
        waited = sleep_mock.call_args[0][0]
        self.assertAlmostEqual(waited, 0.15 * 4, places=3)

    def test_get_json_429_registers_backoff(self):
        with mock.patch("platforms.requests.get", return_value=_FakeResp(429)), \
             mock.patch.object(p, "_pace"):
            with self.assertLogs("platforms", level="WARNING") as cm:
                result = p.get_json("vinted", "https://vinted.example/api")
        self.assertIsNone(result)
        self.assertEqual(p._backoff_multiplier.get("vinted"), 2)
        self.assertIn("backing off", " ".join(cm.output))

    def test_fetch_page_429_registers_backoff(self):
        fake_fetcher = mock.MagicMock()
        fake_fetcher.get.return_value = mock.MagicMock(status=429)
        with mock.patch.dict("sys.modules", {"scrapling.fetchers": mock.MagicMock(Fetcher=fake_fetcher)}), \
             mock.patch.object(p, "_pace"):
            with self.assertLogs("platforms", level="WARNING") as cm:
                result = p._fetch_page("offerup", "https://offerup.com/search?q=x")
        self.assertIsNone(result)
        self.assertEqual(p._backoff_multiplier.get("offerup"), 2)
        self.assertIn("backing off", " ".join(cm.output))


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
        fake_fetcher = mock.MagicMock()
        fake_fetcher.post.return_value = _FakeScraplingResp(200, body)
        with mock.patch.dict("sys.modules", {"scrapling.fetchers": mock.MagicMock(Fetcher=fake_fetcher)}):
            listings, _ = p.search_shopgoodwill({"query": "test"})
        self.assertEqual(len(listings), 1)


class ShopGoodwillProxyScoping(unittest.TestCase):
    # Real live bug (two-part): ShopGoodwill's WAF blocks plain `requests`
    # regardless of source IP - a residential proxy alone only got ~10%
    # of calls through (confirmed live: 23/29 still 403'd even proxied).
    # It's a TLS/HTTP client fingerprint check, same class of WAF that made
    # OfferUp/Depop need scrapling instead of plain requests - confirmed
    # live that scrapling's Fetcher (curl_cffi impersonation) through the
    # SAME proxy gets a clean 200 every time. Fix routes search_shopgoodwill
    # through scrapling's Fetcher.post with SHOPGOODWILL_PROXY_URL - scoped
    # to ONLY this platform, since every other adapter (get_json-based or
    # scrapling-based) must keep hitting its own origin directly, unaffected.
    def _run(self, env):
        body = {"searchResults": {"items": [], "itemCount": 0}}
        fake_fetcher = mock.MagicMock()
        fake_fetcher.post.return_value = _FakeScraplingResp(200, body)
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.dict("sys.modules", {"scrapling.fetchers": mock.MagicMock(Fetcher=fake_fetcher)}):
            p.search_shopgoodwill({"query": "test"})
        return fake_fetcher.post

    def test_proxy_url_set_is_passed_to_fetcher_post(self):
        post_mock = self._run({"SHOPGOODWILL_PROXY_URL": "http://user:pass@proxy.example.com:8888"})
        _, kwargs = post_mock.call_args
        self.assertEqual(kwargs["proxy"], "http://user:pass@proxy.example.com:8888")
        self.assertEqual(kwargs["headers"], p.SHOPGOODWILL_HEADERS)

    def test_proxy_url_unset_passes_none(self):
        post_mock = self._run({})
        _, kwargs = post_mock.call_args
        self.assertIsNone(kwargs["proxy"])

    def test_proxy_scoping_does_not_leak_into_get_json(self):
        # Regression guard: the proxy must be read directly inside
        # search_shopgoodwill, never threaded into get_json() - any other
        # platform calling get_json() must never pick up this proxy.
        with mock.patch.dict("os.environ", {"SHOPGOODWILL_PROXY_URL": "http://proxy.example.com:8888"}, clear=True), \
                mock.patch("platforms.requests.get") as get_mock:
            get_mock.return_value = _FakeResp(200, {"ok": True})
            p.get_json("poshmark", "https://example.com")
        _, kwargs = get_mock.call_args
        self.assertNotIn("proxies", kwargs)
        self.assertNotIn("proxy", kwargs)

    def test_scrapling_not_installed_returns_empty_not_a_crash(self):
        with mock.patch.dict("sys.modules", {"scrapling.fetchers": None}):
            with self.assertLogs("platforms", level="WARNING") as cm:
                listings, count = p.search_shopgoodwill({"query": "test"})
        self.assertEqual(listings, [])
        self.assertIsNone(count)
        self.assertIn("scrapling is not installed", " ".join(cm.output))


class ShopGoodwillCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = pathlib.Path(self.tmpdir) / "state.json"
        self.original = p.SHOPGOODWILL_RATE_LIMIT_STATE_PATH
        p.SHOPGOODWILL_RATE_LIMIT_STATE_PATH = self.path

    def tearDown(self):
        p.SHOPGOODWILL_RATE_LIMIT_STATE_PATH = self.original
        shutil.rmtree(self.tmpdir)

    def test_persistent_blocks_escalate_30_60_120_minutes(self):
        with mock.patch.object(p.time, "time", return_value=1000):
            for expected_streak, expected_minutes in ((1, 30), (2, 60), (3, 120), (4, 120)):
                p._trip_shopgoodwill_circuit_breaker(403)
                state = p._read_shopgoodwill_rate_limit_state()
                self.assertEqual(state["consecutive_block_streak"], expected_streak)
                self.assertEqual(state["blocked_until_ts"], 1000 + expected_minutes * 60)

    def test_active_cooldown_skips_without_request(self):
        self.path.write_text(json.dumps({"blocked_until_ts": time.time() + 600, "consecutive_block_streak": 1}))
        fake_fetcher = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"scrapling.fetchers": mock.MagicMock(Fetcher=fake_fetcher)}):
            self.assertEqual(p.search_shopgoodwill({"query": "test"}), ([], None))
        fake_fetcher.post.assert_not_called()

    def test_first_403_retries_then_success_clears_old_streak(self):
        self.path.write_text(json.dumps({"blocked_until_ts": 0, "consecutive_block_streak": 2}))
        fake_fetcher = mock.MagicMock()
        fake_fetcher.post.side_effect = [
            _FakeScraplingResp(403, headers={"retry-after": "0"}),
            _FakeScraplingResp(200, {"searchResults": {"items": [], "itemCount": 0}}),
        ]
        with mock.patch.dict("sys.modules", {"scrapling.fetchers": mock.MagicMock(Fetcher=fake_fetcher)}):
            self.assertEqual(p.search_shopgoodwill({"query": "test"}), ([], 0))
        self.assertEqual(p._read_shopgoodwill_rate_limit_state()["consecutive_block_streak"], 0)

    def test_two_429s_trip_breaker_and_honor_retry_after(self):
        blocked = _FakeScraplingResp(429, headers={"Retry-After": "7"})
        fake_fetcher = mock.MagicMock()
        fake_fetcher.post.side_effect = [blocked, blocked]
        with mock.patch.object(p.time, "sleep") as sleep_mock, \
                mock.patch.dict("sys.modules", {"scrapling.fetchers": mock.MagicMock(Fetcher=fake_fetcher)}):
            self.assertEqual(p.search_shopgoodwill({"query": "test"}), ([], None))
        sleep_mock.assert_any_call(7)
        self.assertEqual(p._read_shopgoodwill_rate_limit_state()["last_status"], 429)


class VintedCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = pathlib.Path(self.tmpdir) / "state.json"
        self.original = p.VINTED_RATE_LIMIT_STATE_PATH
        p.VINTED_RATE_LIMIT_STATE_PATH = self.path
        p._vinted_thread_state = threading.local()
        p._backoff_multiplier.clear()
        p._last_call.clear()
        p._vinted_consecutive_query_failures = 0

    def tearDown(self):
        p.VINTED_RATE_LIMIT_STATE_PATH = self.original
        p._vinted_thread_state = threading.local()
        p._vinted_consecutive_query_failures = 0
        shutil.rmtree(self.tmpdir)

    def test_persistent_blocks_escalate_30_60_120_minutes(self):
        with mock.patch.object(p.time, "time", return_value=1000):
            for expected_streak, expected_minutes in ((1, 30), (2, 60), (3, 120), (4, 120)):
                p._trip_vinted_circuit_breaker(429)
                state = p._read_vinted_rate_limit_state()
                self.assertEqual(state["consecutive_block_streak"], expected_streak)
                self.assertEqual(state["blocked_until_ts"], 1000 + expected_minutes * 60)

    def test_active_cooldown_skips_without_bootstrap_or_catalog_request(self):
        self.path.write_text(json.dumps({
            "blocked_until_ts": time.time() + 600,
            "consecutive_block_streak": 1,
        }))
        with mock.patch.object(p, "_get_vinted_session") as bootstrap:
            self.assertEqual(p.search_vinted({"query": "test"}), ([], None))
        bootstrap.assert_not_called()

    def test_two_429s_on_one_query_honor_retry_after_but_dont_trip_breaker(self):
        # A single query failing its own built-in retry is normal
        # load-shedding noise (every healthy run sees several) - it must
        # NOT blank the whole lane by itself. Regression test for the
        # real outage this caused: one streak-1 trip produced six
        # straight 0-listing runs.
        blocked = _FakeScraplingResp(429, headers={"Retry-After": "7"})
        session = mock.MagicMock()
        session.get.side_effect = [blocked, blocked]
        with mock.patch.object(p, "_pace"), mock.patch.object(p.time, "sleep") as sleep_mock:
            body, returned_session = p._get_vinted_catalog_page(session, {"page": 1})
        self.assertIsNone(body)
        self.assertIs(returned_session, session)
        sleep_mock.assert_called_once_with(7)
        state = p._read_vinted_rate_limit_state()
        self.assertEqual(state, {})

    def test_breaker_trips_only_after_threshold_distinct_query_failures(self):
        blocked = _FakeScraplingResp(429, headers={"Retry-After": "0"})
        session = mock.MagicMock()
        with mock.patch.object(p, "_pace"), mock.patch.object(p.time, "sleep"):
            for i in range(p.VINTED_TRIP_THRESHOLD):
                session.get.side_effect = [blocked, blocked]
                body, _ = p._get_vinted_catalog_page(session, {"page": i})
                self.assertIsNone(body)
        state = p._read_vinted_rate_limit_state()
        self.assertEqual(state["consecutive_block_streak"], 1)

    def test_success_between_failures_resets_the_failure_count(self):
        blocked = _FakeScraplingResp(429, headers={"Retry-After": "0"})
        ok = _FakeScraplingResp(200, {"items": []})
        session = mock.MagicMock()
        with mock.patch.object(p, "_pace"), mock.patch.object(p.time, "sleep"):
            for _ in range(p.VINTED_TRIP_THRESHOLD - 1):
                session.get.side_effect = [blocked, blocked]
                p._get_vinted_catalog_page(session, {"page": 1})
            session.get.side_effect = [ok]
            p._get_vinted_catalog_page(session, {"page": 2})
        self.assertEqual(p._vinted_consecutive_query_failures, 0)
        self.assertEqual(p._read_vinted_rate_limit_state(), {})

    def test_success_after_expired_cooldown_clears_streak(self):
        self.path.write_text(json.dumps({"blocked_until_ts": 0, "consecutive_block_streak": 2}))
        session = mock.MagicMock()
        session.get.return_value = _FakeScraplingResp(200, {"items": []})
        with mock.patch.object(p, "_pace"):
            body, _ = p._get_vinted_catalog_page(session, {"page": 1})
        self.assertEqual(body, {"items": []})
        self.assertEqual(p._read_vinted_rate_limit_state()["consecutive_block_streak"], 0)

    def test_catalog_uses_same_fingerprinted_session_without_static_user_agent(self):
        session = mock.MagicMock()
        session.get.return_value = _FakeScraplingResp(200, {"items": []})
        params = {"page": 1}
        with mock.patch.object(p, "_pace"):
            body, returned_session = p._get_vinted_catalog_page(session, params)
        self.assertEqual(body, {"items": []})
        self.assertIs(returned_session, session)
        session.get.assert_called_once_with(
            p.VINTED_CATALOG_URL,
            params=params,
            headers={"Accept": "application/json"},
            timeout=p.HTTP_TIMEOUT,
        )

    def test_bootstrap_rejects_missing_anonymous_cookies(self):
        session = mock.MagicMock()
        session.get.return_value = _FakeScraplingResp(200)
        manager = mock.MagicMock()
        manager.__enter__.return_value = session
        fetcher_session = mock.MagicMock(return_value=manager)
        with mock.patch.dict(
            "sys.modules", {"scrapling.fetchers": mock.MagicMock(FetcherSession=fetcher_session)}
        ):
            self.assertIsNone(p._get_vinted_session())
        manager.__exit__.assert_called_once_with(None, None, None)

    def test_bootstrap_rejects_http_challenge_instead_of_caching_it(self):
        session = mock.MagicMock()
        session.get.return_value = _FakeScraplingResp(403)
        manager = mock.MagicMock()
        manager.__enter__.return_value = session
        fetcher_session = mock.MagicMock(return_value=manager)
        with mock.patch.dict(
            "sys.modules", {"scrapling.fetchers": mock.MagicMock(FetcherSession=fetcher_session)}
        ):
            self.assertIsNone(p._get_vinted_session())
        manager.__exit__.assert_called_once_with(None, None, None)
        self.assertIsNone(getattr(p._vinted_thread_state, "session", None))

    def test_bootstrap_uses_browser_fingerprinted_fetcher_session(self):
        session = mock.MagicMock()
        session.get.return_value = _FakeScraplingResp(
            200,
            cookies={"anon_id": "anon", "access_token_web": "token"},
        )
        manager = mock.MagicMock()
        manager.__enter__.return_value = session
        fetcher_session = mock.MagicMock(return_value=manager)
        with mock.patch.dict(
            "sys.modules", {"scrapling.fetchers": mock.MagicMock(FetcherSession=fetcher_session)}
        ), mock.patch("platforms.requests.Session") as requests_session:
            self.assertIs(p._get_vinted_session(), session)
            self.assertIs(p._get_vinted_session(), session)
        fetcher_session.assert_called_once_with(timeout=p.HTTP_TIMEOUT, retries=1)
        session.get.assert_called_once_with("https://www.vinted.com/", timeout=p.HTTP_TIMEOUT)
        requests_session.assert_not_called()

    def test_bootstrap_without_scrapling_fails_closed(self):
        with mock.patch.dict("sys.modules", {"scrapling.fetchers": None}):
            with self.assertLogs("platforms", level="WARNING") as cm:
                self.assertIsNone(p._get_vinted_session())
        self.assertIn("scrapling is not installed", " ".join(cm.output))

    def test_each_worker_gets_a_dedicated_session(self):
        def healthy_manager():
            session = mock.MagicMock()
            session.get.return_value = _FakeScraplingResp(
                200,
                cookies={"anon_id": "anon", "access_token_web": "token"},
            )
            manager = mock.MagicMock()
            manager.__enter__.return_value = session
            return manager

        managers = [healthy_manager(), healthy_manager()]
        fetcher_session = mock.MagicMock(side_effect=managers)
        returned = []
        with mock.patch.dict(
            "sys.modules", {"scrapling.fetchers": mock.MagicMock(FetcherSession=fetcher_session)}
        ):
            threads = [threading.Thread(target=lambda: returned.append(p._get_vinted_session())) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(returned), 2)
        self.assertIsNot(returned[0], returned[1])


_OFFERUP_HTML = (
    '<html><body><script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"searchFeedResponse":{"looseTiles":['
    '{"__typename":"ModularFeedTileGoogleDisplayAd"},'
    '{"__typename":"ModularFeedTileListing","listing":{'
    '"listingId":"97415bd5-6ba9-3c3c-b357-b06f9301dd75",'
    '"title":"Rolex Watches ","price":"2000",'
    '"image":{"__typename":"ModularFeedImage",'
    '"url":"https://images.offerup.com/abc.jpg","width":250,"height":250},'
    '"locationName":"Columbia, SC","conditionText":null}}'
    ']}}}}'
    '</script></body></html>'
)


def _depop_html(payload):
    """Build a realistic Depop RSC flight page from a query-cache payload.

    The real page is self.__next_f.push([1, "<escaped JS string>"]) where the
    string is JSON text prefixed with a row id ("12:..."). json.dumps of the
    string literal reproduces the exact escaping the wire format uses.
    """
    row = json.dumps(["$", "$L48", None, payload])
    return "<html><body><script>self.__next_f.push([1," + json.dumps("12:" + row) + "]);</script></body></html>"


_DEPOP_PAYLOAD = {
    "state": {
        "mutations": [],
        "queries": [{
            "queryKey": ["product_search", {"params": {"q": "rolex watch"}}],
            "state": {
                "data": {
                    "pages": [{
                        "data": {
                            "objects": [{
                                "id": 879421273,
                                "description": "Moissanite Rose Gold Luxury Watch",
                                "pictures": [{"formats": {"P0": {"url": "https://media-photos.depop.com/P0.jpg"}}}],
                                "pricing": {"current_price": {"total_price": "105.00"}},
                                "location": "Garden Grove, United States",
                                "attributes": {"brand": "unbranded", "condition": "brand_new"},
                                "slug": "jayusfinds-moissanite-rose-gold-luxury-watch-d1ca",
                            }]
                        }
                    }]
                }
            }
        }]
    }
}


class OfferUpAdapter(unittest.TestCase):
    # search_offerup/search_depop are @batch_adapter entries - real bug
    # caught in review before this ever shipped: they were first built
    # single-search (saved_search -> [listings]), but
    # prefetch_marketplaces()'s batch_worker calls every BATCH_ADAPTERS
    # entry with a LIST of matching searches and expects {query:
    # [listings]} back (see search_grailed_batch's own contract/tests) -
    # confirmed live, the single-search version raised TypeError the
    # moment it was called the way batch_worker actually calls it. Every
    # test below exercises the REAL list-in/dict-out contract, not the
    # function in isolation with an untested calling convention.
    def test_extracts_real_listing_from_fixture(self):
        with mock.patch.object(p, "_fetch_page", return_value=_OFFERUP_HTML):
            result = p.search_offerup([{"query": "rolex watch"}])

        self.assertIn("rolex watch", result)
        listings = result["rolex watch"]
        self.assertEqual(len(listings), 1)
        l = listings[0]
        self.assertEqual(l["itemId"], "offerup:97415bd5-6ba9-3c3c-b357-b06f9301dd75")
        self.assertEqual(l["title"], "Rolex Watches")
        self.assertEqual(l["price"], {"value": 2000.0, "currency": "USD"})
        self.assertEqual(l["itemWebUrl"], "https://offerup.com/item/detail/97415bd5-6ba9-3c3c-b357-b06f9301dd75")
        self.assertEqual(l["image"]["imageUrl"], "https://images.offerup.com/abc.jpg")

    def test_missing_next_data_returns_empty_dict(self):
        with mock.patch.object(p, "_fetch_page", return_value="<html><body>no data</body></html>"):
            with self.assertLogs("platforms", level="WARNING") as cm:
                result = p.search_offerup([{"query": "rolex watch"}])

        self.assertEqual(result, {})
        self.assertIn("__NEXT_DATA__", " ".join(cm.output))

    def test_query_exclusions_stripped_before_building_url(self):
        with mock.patch.object(p, "_fetch_page", return_value="<html></html>") as fetch:
            p.search_offerup([{"query": "rolex watch -canteen -mug"}])
        self.assertEqual(fetch.call_args[0][1], "https://offerup.com/search?q=rolex+watch")

    def test_multiple_searches_each_get_their_own_key(self):
        # The real bug class this bare-list contract exists to prevent:
        # every enabled search sharing this platform must get its OWN
        # results under its OWN (raw, unstripped) query key, not silently
        # merged or only the last one surviving.
        with mock.patch.object(p, "_fetch_page", return_value=_OFFERUP_HTML):
            result = p.search_offerup([{"query": "rolex watch"}, {"query": "omega watch -parts"}])
        self.assertEqual(set(result.keys()), {"rolex watch", "omega watch -parts"})

    def test_deadline_snapshots_completed_work_and_late_worker_cannot_mutate_it(self):
        searches = [{"query": "rolex watch"}, {"query": "omega watch"}]
        release_late_request = threading.Event()
        late_request_started = threading.Event()

        def fetch_page(*_args, **_kwargs):
            if not late_request_started.is_set() and fetch_page.calls == 0:
                fetch_page.calls += 1
                return _OFFERUP_HTML
            late_request_started.set()
            release_late_request.wait(1)
            return _OFFERUP_HTML

        fetch_page.calls = 0
        with mock.patch.object(p, "MARKETPLACE_BATCH_DEADLINE_SECONDS", 0.05), \
                mock.patch.object(p, "_fetch_page", side_effect=fetch_page) as fetch:
            result = p.search_offerup(searches)
        self.assertTrue(late_request_started.is_set())
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(set(result), {"rolex watch"})
        release_late_request.set()
        time.sleep(0.02)
        self.assertEqual(set(result), {"rolex watch"})


class DepopAdapter(unittest.TestCase):
    def test_extracts_real_listing_from_fixture(self):
        with mock.patch.object(p, "_fetch_page", return_value=_depop_html(_DEPOP_PAYLOAD)):
            result = p.search_depop([{"query": "rolex watch"}])

        self.assertIn("rolex watch", result)
        listings = result["rolex watch"]
        self.assertEqual(len(listings), 1)
        l = listings[0]
        self.assertEqual(l["itemId"], "depop:879421273")
        self.assertEqual(l["title"], "Moissanite Rose Gold Luxury Watch")
        self.assertEqual(l["price"], {"value": 105.0, "currency": "USD"})
        self.assertEqual(l["itemWebUrl"], "https://www.depop.com/products/jayusfinds-moissanite-rose-gold-luxury-watch-d1ca/")
        self.assertEqual(l["image"]["imageUrl"], "https://media-photos.depop.com/P0.jpg")

    def test_missing_product_search_cache_returns_empty_dict(self):
        payload = {"state": {"queries": [{"queryKey": ["some_other_query", {}], "state": {"data": {"pages": []}}}]}}
        with mock.patch.object(p, "_fetch_page", return_value=_depop_html(payload)):
            with self.assertLogs("platforms", level="WARNING") as cm:
                result = p.search_depop([{"query": "rolex watch"}])

        self.assertEqual(result, {})
        self.assertIn("product_search", " ".join(cm.output))

    def test_query_exclusions_stripped_before_building_url(self):
        with mock.patch.object(p, "_fetch_page", return_value="<html></html>") as fetch:
            p.search_depop([{"query": "rolex watch -canteen"}])
        self.assertEqual(fetch.call_args[0][1], "https://www.depop.com/search/?q=rolex+watch")

    def test_real_batch_worker_call_shape_does_not_raise(self):
        # Mutation-catchable regression test for the exact bug found in
        # review: simulates prefetch_marketplaces().batch_worker's real
        # call convention end to end (list-in) instead of testing the
        # function in isolation with an assumed signature.
        relevant = [{"query": "rolex watch", "max_price": 500, "category_id": "281", "enabled": True}]
        with mock.patch.object(p, "_fetch_page", return_value=_depop_html(_DEPOP_PAYLOAD)):
            result = p.BATCH_ADAPTERS["depop"](relevant)
        self.assertIsInstance(result, dict)
        self.assertIn("rolex watch", result)

    def test_deadline_snapshots_completed_work_and_late_worker_cannot_mutate_it(self):
        searches = [{"query": "rolex watch"}, {"query": "omega watch"}]
        release_late_request = threading.Event()
        late_request_started = threading.Event()

        def fetch_page(*_args, **_kwargs):
            if not late_request_started.is_set() and fetch_page.calls == 0:
                fetch_page.calls += 1
                return _depop_html(_DEPOP_PAYLOAD)
            late_request_started.set()
            release_late_request.wait(1)
            return _depop_html(_DEPOP_PAYLOAD)

        fetch_page.calls = 0
        with mock.patch.object(p, "MARKETPLACE_BATCH_DEADLINE_SECONDS", 0.05), \
                mock.patch.object(p, "_fetch_page", side_effect=fetch_page) as fetch:
            result = p.search_depop(searches)
        self.assertTrue(late_request_started.is_set())
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(set(result), {"rolex watch"})
        release_late_request.set()
        time.sleep(0.02)
        self.assertEqual(set(result), {"rolex watch"})


if __name__ == "__main__":
    unittest.main()
