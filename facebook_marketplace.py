"""Facebook Marketplace adapter.

Facebook has no public API and blocks plain `requests` scraping, but a real
headless Chromium (Playwright) routed through a residential proxy, LOGGED OUT
with no account at all, serves the full result set inside the initial page
HTML as `<script type="application/json">` blocks. Each listing is a dict
carrying `marketplace_listing_title` plus its price/photo/location siblings.

This module isolates that heavy Playwright dependency from platforms.py's
lightweight requests-only adapters. One function matters to the rest of the
bot: search_facebook_batch() - the BATCH_ADAPTERS entry for "facebook". It
fetches every facebook-enabled saved search in ONE browser launch, reusing
one context, and returns the same {query: [listings]} shape as
search_grailed_batch().

READ-ONLY. Logged out, no account, no login, no messaging, no purchase.
"""

import json
import logging
import os
import re
from urllib.parse import unquote, urlencode, urlsplit

from platforms import _dget, batch_adapter, make_listing, split_query_exclusions

logger = logging.getLogger("facebook_marketplace")

# The proxy is billed by DATA VOLUME, not request count, so abort every
# resource type that isn't the page HTML itself.
_BLOCKED_RESOURCE_TYPES = ("image", "media", "font", "stylesheet")

# Per explicit user instruction: results must be local, "roughly 20 miles
# from Columbia SC" - without this, search_facebook_batch() had no location
# parameter at all, so results were effectively nationwide (whatever the
# residential proxy's own IP geolocation happened to default to, unscoped
# and unpredictable run to run). latitude/longitude/radius are Facebook
# Marketplace's own documented search-URL parameters (radius in miles).
# NOT independently verified live from this environment - Facebook blocks
# unauthenticated fetches outside the real Playwright+proxy path this
# module already uses, and that path can't be exercised locally (Playwright
# browser install is CI/Linux-only, see poll.yml). Check the next few real
# Facebook alerts actually land local before trusting this fully.
FACEBOOK_SEARCH_LATITUDE = 34.0007
FACEBOOK_SEARCH_LONGITUDE = -81.0348
FACEBOOK_SEARCH_RADIUS_MILES = 20

# Listing data lives inside <script type="application/json"> tags. Facebook's
# tag carries other attributes around the type, so match the type anywhere in
# the tag. ponytail: regex, not an HTML parser - fine while the JSON blocks
# never contain a literal "</script>"; if a title ever embeds that, switch to
# html.parser for the extraction step.
_JSON_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\btype=["\']application/json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def _block_heavy_resources(route, request=None):
    # `request` was a second positional arg in older Playwright; 1.57 passes
    # only `route` and exposes it as route.request. Accept both so a version
    # skew can't turn every request into a crash mid-run.
    req = request if request is not None else route.request
    if req.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _find_listing_dicts(obj):
    """Recursively collect every dict carrying marketplace_listing_title."""
    found = []
    if isinstance(obj, dict):
        if "marketplace_listing_title" in obj:
            found.append(obj)
        for value in obj.values():
            found.extend(_find_listing_dicts(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_listing_dicts(item))
    return found


def _extract_listing_nodes(html):
    """Flat list of listing dicts, parsed from every application/json block."""
    nodes = []
    for raw in _JSON_SCRIPT_RE.findall(html):
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        nodes.extend(_find_listing_dicts(parsed))
    return nodes


def _node_to_listing(node):
    """One Facebook listing dict -> one make_listing() result (or None)."""
    node_id = node.get("id")
    title = node.get("marketplace_listing_title")
    price = _dget(_dget(node, "listing_price"), "amount")
    photo_uri = _dget(_dget(_dget(node, "primary_listing_photo"), "image"), "uri")
    geo = _dget(_dget(node, "location"), "reverse_geocode")
    city = _dget(geo, "city")
    state = _dget(geo, "state")
    description = f"Location: {city}, {state}" if city and state else None
    return make_listing(
        "facebook",
        node_id,
        title,
        price,
        f"https://www.facebook.com/marketplace/item/{node_id}/",
        image_url=photo_uri,
        description=description,
    )


@batch_adapter("facebook")
def search_facebook_batch(saved_searches):
    """Fetch every passed search from Facebook Marketplace in one browser.

    Returns {query: [listings]} covering every search passed in, even when a
    query produced zero listings or errored (an empty list for that key,
    never a missing key). Returns {} immediately if FACEBOOK_PROXY_URL is
    unset or playwright isn't installed - same skip contract as
    _call_deepseek_json's missing-key path, so a missing dependency can never
    crash the run.
    """
    proxy_url = os.environ.get("FACEBOOK_PROXY_URL")
    if not proxy_url:
        logger.warning("Skipping Facebook Marketplace: FACEBOOK_PROXY_URL is not configured")
        return {}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Skipping Facebook Marketplace: playwright is not installed")
        return {}

    # urlsplit's .username/.password are NOT percent-decoded (confirmed:
    # "p%40ss" stays literally "p%40ss", never becomes "p@ss") - a proxy
    # password containing @, :, %, /, or # (common in residential-proxy
    # session strings) got handed to Playwright as the literal encoded
    # text, causing silent 407s on every request (each page.goto burns its
    # full timeout, logged as an ordinary "search failed", indistinguishable
    # from zero real matches). .port is also None with no explicit port,
    # which previously produced the literal string "host:None" - default
    # to the scheme's standard port instead of crashing chromium.launch().
    parsed = urlsplit(proxy_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    proxy = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{port}",
        "username": unquote(parsed.username) if parsed.username else parsed.username,
        "password": unquote(parsed.password) if parsed.password else parsed.password,
    }

    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=proxy)
        try:
            context = browser.new_context()
            context.route("**/*", _block_heavy_resources)
            for saved_search in saved_searches:
                query = saved_search["query"]
                out.setdefault(query, [])
                clean_query, _ = split_query_exclusions(query)
                url = "https://www.facebook.com/marketplace/search/?" + urlencode({
                    "query": clean_query,
                    "latitude": FACEBOOK_SEARCH_LATITUDE,
                    "longitude": FACEBOOK_SEARCH_LONGITUDE,
                    "radius": FACEBOOK_SEARCH_RADIUS_MILES,
                })
                try:
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=15000, wait_until="domcontentloaded")
                        page.wait_for_timeout(1500)
                        html = page.content()
                    finally:
                        page.close()
                    for node in _extract_listing_nodes(html):
                        listing = _node_to_listing(node)
                        if listing:
                            out[query].append(listing)
                except Exception as exc:
                    logger.warning("facebook search failed for query %r: %s", query, exc)
        finally:
            browser.close()
    return out
