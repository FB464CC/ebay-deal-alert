"""Regression tests for the Facebook Marketplace adapter.

Facebook has no public API and blocks plain `requests` scraping; the adapter
drives a real headless Chromium (Playwright) through a residential proxy,
LOGGED OUT with no account. These tests mock playwright entirely (patched into
sys.modules) so a test run never launches a browser and never needs the proxy.
The four things locked in here:

  1. A real listing node parses into the correct make_listing shape.
  2. FACEBOOK_PROXY_URL unset -> empty dict, no browser launch attempted.
  3. One query's page.goto raising still lets the other queries return.
  4. A page with no marketplace_listing_title anywhere -> empty list, no crash.

Pure stdlib unittest, mirroring test_ebay_deal_alert.py / test_photo_provider.py.
"""

import json
import os
import sys
import types
import unittest
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import facebook_marketplace


PLACEHOLDER_URL = "https://example.com/listing-photo.jpg"

# One real listing node as the live response builds it. Ordinary Python dict,
# not a JSON string - the exact shape search_facebook_batch walks for.
LISTING_NODE = {
    "__typename": "GroupCommerceProductItem",
    "id": "1749404192962999",
    "primary_listing_photo": {
        "image": {"uri": PLACEHOLDER_URL},
    },
    "listing_price": {
        "formatted_amount": "$60,000",
        "amount": "60000.00",
    },
    "location": {
        "reverse_geocode": {"city": "San Francisco", "state": "CA"},
    },
    "marketplace_listing_title": "Presidential Rolex",
}

HTML_WITH_LISTING = (
    '<html><body><script type="application/json">'
    + json.dumps({"marketplace_search": {"results": [LISTING_NODE]}})
    + "</script></body></html>"
)

HTML_WITHOUT_LISTING = (
    '<html><body><script type="application/json">{"foo": [1, 2, 3]}</script>'
    "</body></html>"
)


class _FakePlaywrightCtx:
    """What sync_playwright() returns - a context manager yielding `p`."""

    def __init__(self, p):
        self._p = p

    def __enter__(self):
        return self._p

    def __exit__(self, exc_type, exc, tb):
        return False


def _install_fake_playwright(page_behaviors):
    """Patch a fake playwright into sys.modules.

    `page_behaviors` is one dict per expected context.new_page() call; each
    may set 'html' (page.content() return value) and 'goto_raises' (exception
    for page.goto()). Returns (patcher, sync_playwright_mock, browser_mock,
    pages) - `pages` is the list of page mocks themselves, in call order,
    for tests that need to inspect what a specific page.goto() was called
    with (context.new_page.side_effect gets consumed into an iterator once
    the real code calls it, so it can't be read back after the fact)."""
    pages = []
    for behavior in page_behaviors:
        page = mock.MagicMock()
        page.content.return_value = behavior.get("html", "")
        if behavior.get("goto_raises"):
            page.goto.side_effect = behavior["goto_raises"]
        pages.append(page)

    context = mock.MagicMock()
    context.new_page.side_effect = pages
    browser = mock.MagicMock()
    browser.new_context.return_value = context
    p = mock.MagicMock()
    p.chromium.launch.return_value = browser
    sync_playwright = mock.MagicMock(return_value=_FakePlaywrightCtx(p))

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = sync_playwright
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api

    patcher = mock.patch.dict(sys.modules, {
        "playwright": fake_playwright,
        "playwright.sync_api": fake_sync_api,
    })
    return patcher, sync_playwright, browser, pages


def _proxy_env():
    # A clearly-fake placeholder, never the real proxy URL/credentials.
    return {"FACEBOOK_PROXY_URL": "http://user:pass@proxy.example.com:8080"}


class NodeParsing(unittest.TestCase):
    def test_listing_node_parses_into_make_listing_shape(self):
        listing = facebook_marketplace._node_to_listing(LISTING_NODE)
        self.assertEqual(listing["platform"], "facebook")
        self.assertEqual(listing["itemId"], "facebook:1749404192962999")
        self.assertEqual(listing["title"], "Presidential Rolex")
        self.assertEqual(listing["price"]["value"], 60000.0)
        self.assertEqual(
            listing["itemWebUrl"],
            "https://www.facebook.com/marketplace/item/1749404192962999/",
        )
        self.assertEqual(listing["image"]["imageUrl"], PLACEHOLDER_URL)
        self.assertEqual(listing["description"], "Location: San Francisco, CA")


class NoProxySkip(unittest.TestCase):
    def test_unset_proxy_returns_empty_dict_without_launching(self):
        patcher, sync_playwright, _browser, _pages = _install_fake_playwright([])
        with mock.patch.dict("os.environ", {}, clear=True), patcher:
            result = facebook_marketplace.search_facebook_batch([{"query": "rolex watch"}])
        self.assertEqual(result, {})
        sync_playwright.assert_not_called()


class PerQueryIsolation(unittest.TestCase):
    def test_one_query_error_does_not_lose_others(self):
        patcher, _sync, _browser, _pages = _install_fake_playwright([
            {"goto_raises": Exception("navigation timeout")},
            {"html": HTML_WITH_LISTING},
        ])
        searches = [
            {"query": "rolex watch -strap", "platforms": ["facebook"]},
            {"query": "peter millar gamecocks", "platforms": ["facebook"]},
        ]
        with mock.patch.dict(os.environ, _proxy_env()), patcher:
            result = facebook_marketplace.search_facebook_batch(searches)

        self.assertEqual(result["rolex watch -strap"], [])
        gamecocks = result["peter millar gamecocks"]
        self.assertEqual(len(gamecocks), 1)
        self.assertEqual(gamecocks[0]["title"], "Presidential Rolex")


class EmptyResult(unittest.TestCase):
    def test_no_listing_nodes_returns_empty_list_not_crash(self):
        patcher, _sync, _browser, _pages = _install_fake_playwright([
            {"html": HTML_WITHOUT_LISTING},
        ])
        with mock.patch.dict(os.environ, _proxy_env()), patcher:
            result = facebook_marketplace.search_facebook_batch([{"query": "cartier watch"}])
        self.assertEqual(result, {"cartier watch": []})


class LocationScoping(unittest.TestCase):
    # Real live miss: a first attempt added latitude/longitude/radius query
    # params to the search URL, confirmed WRONG by a real golf-club alert
    # from California - logged-out Marketplace doesn't honor those params,
    # it scopes by the PROXY's own IP location instead (FACEBOOK_PROXY_URL
    # now points at a Columbia-SC-targeted residential proxy). This test
    # locks in the real verification mechanism instead: every listing's
    # location is logged so a mis-located result surfaces in the logs
    # immediately rather than only when a bad alert reaches the user.
    def test_listing_locations_are_logged_for_verification(self):
        patcher, _sync, _browser, _pages = _install_fake_playwright([
            {"html": HTML_WITH_LISTING},
        ])
        with mock.patch.dict(os.environ, _proxy_env()), patcher:
            with self.assertLogs("facebook_marketplace", level="INFO") as cm:
                facebook_marketplace.search_facebook_batch([{"query": "cartier watch"}])
        joined = " ".join(cm.output)
        self.assertIn("facebook locations seen", joined)
        self.assertIn("San Francisco, CA", joined)

    def test_search_url_no_longer_carries_unverified_location_params(self):
        # Regression guard: don't silently reintroduce the wrong fix.
        patcher, _sync, _browser, _pages = _install_fake_playwright([
            {"html": HTML_WITHOUT_LISTING},
        ])
        with mock.patch.dict(os.environ, _proxy_env()), patcher:
            facebook_marketplace.search_facebook_batch([{"query": "cartier watch"}])
        called_url = _pages[0].goto.call_args[0][0]
        params = dict(parse_qsl(urlsplit(called_url).query))
        self.assertNotIn("latitude", params)
        self.assertNotIn("longitude", params)
        self.assertNotIn("radius", params)


if __name__ == "__main__":
    unittest.main()
