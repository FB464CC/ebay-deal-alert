"""Supplementary, quota-free eBay search lane via HTML scraping.

This is NOT a replacement for the official eBay Browse API (search_ebay()
in ebay_deal_alert.py). That API is capped at 5,000 calls/day and is the
real, reliable path the pipeline runs on. This module scrapes eBay's public
search results page (https://www.ebay.com/sch/i.html) instead, which costs
no API quota at all - useful as an extra, best-effort lane run alongside the
official one, not instead of it.

Uses the `scrapling` package's lightweight Fetcher (curl_cffi HTTP with a
browser network fingerprint, but no browser/JS rendering - confirmed live
that eBay's search results are server-rendered HTML, itm links and prices are
directly in the response body).

Historical live finding: a residential IP without a proxy got a 403 on about
1-in-10 calls, while GitHub Actions' shared runner range got 403 on every
call. EBAY_SCRAPE_PROXY_URL was introduced to route calls through a
residential proxy and did return real listing data. It remains optional, but
neither a residential route nor browser-like HTTP fingerprints guarantee
access: eBay/Akamai can still deny them. On any HTTP, transport, configuration,
or parse failure this returns an empty list and logs a safe diagnostic. It
never raises - a scrape failure must never take down the run.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import unquote, urlencode, urlsplit

from scrapling.fetchers import Fetcher

from platforms import make_listing

logger = logging.getLogger(__name__)

EBAY_SEARCH_URL = "https://www.ebay.com/sch/i.html"

# Fetcher already uses curl_cffi's Chrome TLS/HTTP impersonation. Keep the
# User-Agent and Sec-CH-UA values under curl_cffi's control so they match that
# fingerprint, but make the navigation headers explicit and internally
# consistent. Scrapling 0.4.15's defaults add a Google Referer after curl_cffi
# chooses `Sec-Fetch-Site: none`; a real cross-origin Google navigation sends
# `cross-site`, not `none`.
_EBAY_NAVIGATION_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_SUPPORTED_PROXY_SCHEMES = {
    "http",
    "https",
    "socks4",
    "socks4a",
    "socks5",
    "socks5h",
}
_RESPONSE_DIAGNOSTIC_HEADERS = (
    "server",
    "via",
    "proxy-status",
    "content-type",
    "x-ebay-c-request-id",
    "x-ebay-c-version",
)

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


def _build_request_headers():
    """Return a fresh browser-navigation header mapping for one request."""
    return dict(_EBAY_NAVIGATION_HEADERS)


def _proxy_config_error(proxy_url):
    """Return a safe validation error for a proxy URL, or None if usable."""
    if proxy_url != proxy_url.strip() or any(char.isspace() for char in proxy_url):
        return "contains whitespace"
    try:
        parsed = urlsplit(proxy_url)
        port = parsed.port
    except (TypeError, ValueError):
        return "could not be parsed"
    if parsed.scheme.lower() not in _SUPPORTED_PROXY_SCHEMES:
        return "has a missing or unsupported URL scheme"
    if not parsed.hostname:
        return "has no hostname"
    if port is not None and not 1 <= port <= 65535:
        return "has an invalid port"
    return None


def _sanitize_exception_message(exc, proxy_url=None):
    """Compact an exception message and redact configured proxy details."""
    message = re.sub(r"\s+", " ", str(exc)).strip() or "(no message)"
    if proxy_url:
        replacements = {proxy_url: "<configured-proxy>"}
        try:
            parsed = urlsplit(proxy_url)
            for value, placeholder in (
                (parsed.netloc, "<proxy-endpoint>"),
                (parsed.hostname, "<proxy-host>"),
                (parsed.username, "<proxy-username>"),
                (parsed.password, "<proxy-password>"),
            ):
                if value:
                    replacements[value] = placeholder
                    replacements[unquote(value)] = placeholder
        except (TypeError, ValueError):
            pass
        for value in sorted(replacements, key=len, reverse=True):
            if value:
                message = re.sub(
                    re.escape(value),
                    replacements[value],
                    message,
                    flags=re.IGNORECASE,
                )
    return message[:500]


def _log_value(value):
    """Make response metadata safe and compact enough for one log line."""
    if value is None:
        return "-"
    return re.sub(r"[\r\n\t]+", " ", str(value)).strip()[:160] or "-"


def _response_diagnostics(response):
    """Describe a response without logging its body, query URL, or secrets."""
    try:
        final_host = urlsplit(str(getattr(response, "url", ""))).hostname
    except (TypeError, ValueError):
        final_host = None

    try:
        raw_headers = getattr(response, "headers", {}) or {}
        headers = {
            str(key).lower(): value
            for key, value in raw_headers.items()
        }
    except (AttributeError, TypeError, ValueError):
        headers = {}

    try:
        body = getattr(response, "body", None)
    except Exception:
        body = None
    if body is None:
        body = getattr(response, "html_content", None)
    if isinstance(body, str):
        body_bytes = len(body.encode("utf-8", errors="replace"))
    else:
        try:
            body_bytes = len(body) if body is not None else None
        except TypeError:
            body_bytes = None

    fields = [f"final_host={_log_value(final_host)}"]
    fields.extend(
        f"{name.replace('-', '_')}={_log_value(headers.get(name))}"
        for name in _RESPONSE_DIAGNOSTIC_HEADERS
    )
    fields.append(f"body_bytes={_log_value(body_bytes)}")
    return ", ".join(fields)


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
        title = unescape(title_match.group(1)).strip()
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
            image_url=unescape(image_match.group(1)) if image_match else None,
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
                # This marker is auction-only. Tag the format before parsing
                # its countdown so an unfamiliar future time string fails
                # closed in classify_stray_auction_listing() instead of being
                # mistaken for a fixed-price listing at its current bid.
                bid_match = _BID_COUNT_RE.search(chunk)
                listing["buyingOptions"] = ["AUCTION"]
                listing["bidCount"] = int(bid_match.group(1)) if bid_match else 0
                minutes_remaining = _parse_time_left_minutes(time_left_match.group(1))
                if minutes_remaining is not None:
                    end_date = datetime.now(timezone.utc) + timedelta(minutes=minutes_remaining)
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
    if proxy_url and (proxy_error := _proxy_config_error(proxy_url)):
        logger.warning(
            "eBay scrape proxy configuration is invalid (%s); request not sent",
            proxy_error,
        )
        return []

    request_kwargs = {
        "timeout": 15,
        # This function is called repeatedly for different queries. Retrying a
        # dead static proxy three times inside every call only delays the next
        # observable attempt and makes Scrapling log the credential-bearing
        # proxy URL. One bounded attempt keeps failures safe and diagnosable.
        "retries": 1,
        "impersonate": "chrome",
        "headers": _build_request_headers(),
    }
    if proxy_url:
        request_kwargs["proxy"] = proxy_url

    try:
        response = Fetcher.get(url, **request_kwargs)
    except Exception as exc:
        logger.warning(
            "eBay scrape transport failure via %s for %r: %s: %s",
            "configured proxy" if proxy_url else "direct connection",
            query,
            type(exc).__name__,
            _sanitize_exception_message(exc, proxy_url),
        )
        return []

    if response.status != 200:
        if proxy_url and response.status == 407:
            logger.warning(
                "eBay scrape proxy authentication failed with HTTP 407 for %r (%s)",
                query,
                _response_diagnostics(response),
            )
        else:
            logger.warning(
                "eBay scrape received HTTP %s via %s for %r (%s)",
                response.status,
                "configured proxy" if proxy_url else "direct connection",
                query,
                _response_diagnostics(response),
            )
        return []

    try:
        return _parse_listings(response.html_content)
    except Exception as exc:
        logger.warning("eBay scrape parse failed for %r: %s", query, exc)
        return []
