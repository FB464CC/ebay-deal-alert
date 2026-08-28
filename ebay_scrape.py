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

Confirmed live: a single residential IP with no proxy gets a 403 on ~1-in-10
calls from a home connection - and, measured directly against a real GitHub
Actions run, 100% of calls from GH Actions' shared runner IP range (eBay
blocks that range far harder than a residential IP). EBAY_SCRAPE_PROXY_URL
routes every call through a real residential proxy instead - confirmed live
to return real listing data - and is optional: unset, this just calls eBay
directly (same behavior as before, still works fine from a non-CI IP). On a
403/other failure, or any parse failure, this returns an empty list and logs a
warning. It never raises - a scrape failure must never take down the run.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
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

# A live AUCTION card - and ONLY an auction card, confirmed live: a plain
# fixed-price search never carries this span at all - shows a real
# server-rendered countdown here, e.g. "2d 23h", "2h 9m", "35s". Without
# this, the price scraped above is the CURRENT BID for these cards, not a
# real purchasable price (same trap the official Browse API lane had -
# see classify_stray_auction_listing() in ebay_deal_alert.py). This HTML
# lane can't produce a real end-DATE the way the JSON API does, so it
# synthesizes one (now + parsed minutes) in the exact field shape
# classify_stray_auction_listing() already expects (buyingOptions/
# itemEndDate/bidCount) - that shared function then applies the identical
# closing-window gate to these listings too, no duplicated threshold
# logic needed here.
_TIME_LEFT_RE = re.compile(r'class="s-card__time-left">([^<]+)</span>')
_BID_COUNT_RE = re.compile(r'(\d+)\s*bids?\b', re.I)
_TIME_COMPONENT_RE = re.compile(r'(\d+)\s*(d|h|m|s)\b')
_TIME_UNIT_MINUTES = {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}


def _parse_time_left_minutes(text):
    """"2d 23h" / "2h 9m" / "35s" -> total minutes remaining, or None."""
    if not text:
        return None
    components = _TIME_COMPONENT_RE.findall(text)
    if not components:
        return None
    return sum(int(value) * _TIME_UNIT_MINUTES[unit] for value, unit in components)


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
            time_left_match = _TIME_LEFT_RE.search(chunk)
            if time_left_match:
                minutes_remaining = _parse_time_left_minutes(time_left_match.group(1))
                if minutes_remaining is not None:
                    bid_match = _BID_COUNT_RE.search(chunk)
                    end_date = datetime.now(timezone.utc) + timedelta(minutes=minutes_remaining)
                    listing["buyingOptions"] = ["AUCTION"]
                    listing["bidCount"] = int(bid_match.group(1)) if bid_match else 0
                    listing["itemEndDate"] = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
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

    proxy_url = os.environ.get("EBAY_SCRAPE_PROXY_URL")
    try:
        response = Fetcher.get(url, timeout=15, proxy=proxy_url) if proxy_url else Fetcher.get(url, timeout=15)
    except Exception as exc:
        logger.warning("eBay scrape request failed for %r: %s", query, exc)
        return []

    if response.status != 200:
        logger.warning(
            "eBay scrape got non-200 status %s for %r%s",
            response.status,
            query,
            "" if proxy_url else " (no EBAY_SCRAPE_PROXY_URL configured - likely a 403 from this IP)",
        )
        return []

    try:
        return _parse_listings(response.html_content)
    except Exception as exc:
        logger.warning("eBay scrape parse failed for %r: %s", query, exc)
        return []
