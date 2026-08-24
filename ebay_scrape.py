"""Supplementary, quota-free eBay search lane via HTML scraping.

This is NOT a replacement for the official eBay Browse API (search_ebay()
in ebay_deal_alert.py). That API is capped at 5,000 calls/day and is the
real, reliable path the pipeline runs on. This module scrapes eBay's public
search results page (https://www.ebay.com/sch/i.html) instead, which costs
no API quota at all - useful as an extra, best-effort lane run alongside the
official one, not instead of it.

Uses the `scrapling` package's lightweight Fetcher (plain HTTP, no browser/
JS rendering - confirmed live that eBay's search results are server-rendered
HTML, itm links and prices are directly in the response body).

Confirmed live: this intermittently gets a 403 (~1-in-10 from a single IP
with no proxy). This module does NOT do any proxy rotation itself - see
FACEBOOK_PROXY_URL in facebook_marketplace.py for the established pattern
this project uses elsewhere if that's ever wanted here. On a 403, or any
parse failure, this returns an empty list and logs a warning. It never
raises - a scrape failure must never take down the run.
"""

import logging
import re
from urllib.parse import urlencode

from scrapling.fetchers import Fetcher

from platforms import make_listing

logger = logging.getLogger(__name__)

EBAY_SEARCH_URL = "https://www.ebay.com/sch/i.html"

# Each result card starts with this marker; splitting on it turns the page
# into one chunk per listing. Regex over raw HTML, not html.parser - eBay's
# search page is ~2MB of markup and every field needed (id/title/price/
# image) sits in a small, stable set of classes/attributes.
_CARD_SPLIT_RE = re.compile(r'<li class="s-card')
_LISTING_ID_RE = re.compile(r'data-listingid="(\d+)"')
_TITLE_RE = re.compile(r'class="s-card__title">\s*<span[^>]*>([^<]+)</span>')
_PRICE_RE = re.compile(r'class="[^"]*\bs-card__price\b[^"]*">\$([\d,]+\.\d{2})')
_IMAGE_RE = re.compile(r'<img\b[^>]*?class="s-card__image"[^>]*?\bsrc="([^"]+)"')


def _parse_listings(html):
    """Extract real listings from an eBay search results page.

    Skips eBay's "Shop on eBay" ad placeholder cards (fake /itm/123456
    link, confirmed live) and anything missing an id/title/price.
    """
    listings = []
    if not html:
        return listings
    for chunk in _CARD_SPLIT_RE.split(html)[1:]:
        id_match = _LISTING_ID_RE.search(chunk)
        title_match = _TITLE_RE.search(chunk)
        price_match = _PRICE_RE.search(chunk)
        if not id_match or not title_match or not price_match:
            continue
        title = title_match.group(1).strip()
        if title == "Shop on eBay":
            continue
        item_id = id_match.group(1)
        image_match = _IMAGE_RE.search(chunk)
        listing = make_listing(
            "ebay_scraped",
            item_id,
            title,
            price_match.group(1),
            f"https://www.ebay.com/itm/{item_id}",
            image_url=image_match.group(1) if image_match else None,
        )
        if listing:
            # DEDUP FIX: make_listing() namespaces every itemId as
            # "platform:id" (see its own docstring) so seen_items.db can't
            # collide across marketplaces - but the official eBay Browse
            # API (search_ebay() in ebay_deal_alert.py) does NOT go through
            # make_listing() at all and keeps its itemId BARE, in eBay's own
            # "v1|<numeric id>|0" format (confirmed live against real
            # alerts_log.jsonl entries). Without this override, the exact
            # same physical eBay listing reached via this scraped lane vs
            # the official API would look like two different items to the
            # dedup table and could alert twice. Reformatting to eBay's own
            # bare id convention here makes both lanes collide correctly on
            # the same real listing.
            listing["itemId"] = f"v1|{item_id}|0"
            listings.append(listing)
    return listings


def search_ebay_scraped(query, max_price=None, category_id=None):
    """Scrape eBay's public search results page for `query`.

    Supplementary/quota-free alongside search_ebay() - see module docstring.
    Returns a list of listings in the same shape as platforms.make_listing()
    (which is itself the eBay Browse API item shape). Returns [] and logs a
    warning on a non-200 response or any parse failure; never raises.
    """
    params = {"_nkw": query}
    if max_price is not None:
        params["_udhi"] = max_price
    if category_id is not None:
        params["_sacat"] = category_id
    url = f"{EBAY_SEARCH_URL}?{urlencode(params)}"

    try:
        response = Fetcher.get(url, timeout=15)
    except Exception as exc:
        logger.warning("eBay scrape request failed for %r: %s", query, exc)
        return []

    if response.status != 200:
        logger.warning(
            "eBay scrape got non-200 status %s for %r (likely a 403 - no proxy configured)",
            response.status,
            query,
        )
        return []

    try:
        return _parse_listings(response.html_content)
    except Exception as exc:
        logger.warning("eBay scrape parse failed for %r: %s", query, exc)
        return []
