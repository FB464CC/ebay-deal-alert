"""Non-eBay marketplace adapters.

Every adapter returns listings in the SAME SHAPE as eBay's Browse API
item_summary, so the entire downstream pipeline - score_listing(), the
Gemini photo check, fingerprint/relist dedupe, deal rating, ntfy alerts,
alerts_log.jsonl - works on them unchanged. Adding a marketplace means
adding one function here and nothing else.

Deliberately plain `requests` rather than a scraper framework: these are
JSON endpoints, the open-source wrappers around them are thin shims over
the same HTTP call that rot as soon as an endpoint moves, and the whole
GitHub Actions job has to finish inside the 60-second billing rounding
boundary (private repo = metered minutes, rounded UP per job).

READ-ONLY. These adapters only ever fetch public search results. Nothing
here logs in, creates accounts, bids, buys, offers, or messages.
"""

import json
import logging
import re
import threading
import time

import requests

logger = logging.getLogger("platforms")

# A normal browser UA. These endpoints serve the sites' own public web
# frontends; a blank/python UA gets 403'd by ordinary WAF defaults. This is
# not fingerprint spoofing - there is no rotation, no proxying, no cookie
# laundering. One honest client string, one IP, polite pacing.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 8

# Minimum seconds between requests to the same platform, enforced process-wide
# across the thread pool. Keeps us a polite guest instead of hammering.
_MIN_INTERVAL = {
    "grailed": 0.35,
    "depop": 0.35,
    "vinted": 0.60,
    "poshmark": 0.60,
    "mercari": 0.60,
    "vestiaire": 0.50,
    "shopgoodwill": 0.50,
}
_DEFAULT_MIN_INTERVAL = 0.5

_rate_lock = threading.Lock()
_last_call = {}


def _pace(platform):
    """Block until this platform's minimum inter-request interval has passed."""
    interval = _MIN_INTERVAL.get(platform, _DEFAULT_MIN_INTERVAL)
    while True:
        with _rate_lock:
            now = time.monotonic()
            previous = _last_call.get(platform, 0.0)
            wait = interval - (now - previous)
            if wait <= 0:
                _last_call[platform] = now
                return
        time.sleep(wait)


def get_json(platform, url, params=None, headers=None, session=None, timeout=HTTP_TIMEOUT):
    """Paced GET returning parsed JSON, or None on any failure.

    Adapters must never raise into the main loop - one dead marketplace can
    not be allowed to abort a polling run for the other five.
    """
    _pace(platform)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(url, params=params, headers=request_headers, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("%s request failed: %s", platform, exc)
        return None
    if resp.status_code == 429:
        logger.warning("%s rate limited (429), backing off for this run", platform)
        return None
    if not resp.ok:
        logger.warning("%s returned HTTP %s", platform, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("%s returned non-JSON body", platform)
        return None


def _dig(obj, path):
    """Walk a dotted path through nested dicts/lists, returning None if absent."""
    current = obj
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _to_float(value):
    """Coerce the many price shapes these APIs use ('$42.00', 4200, '42.5')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.]", "", str(value))
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def make_listing(
    platform,
    item_id,
    title,
    price,
    url,
    image_url=None,
    shipping=0.0,
    seller=None,
    extra_images=(),
    size=None,
    condition=None,
    currency="USD",
):
    """Normalize one marketplace listing into eBay Browse API item shape.

    Returns None if the listing is missing anything the pipeline requires -
    a half-populated listing is worse than no listing, since it would be
    scored and alerted on incomplete data.
    """
    price = _to_float(price)
    if not item_id or not title or price is None or not url:
        return None

    # NOTE: itemId is namespaced "platform:id" so seen_items.db cannot collide
    # across marketplaces. eBay listings deliberately keep BARE ids (see
    # ebay_deal_alert.run) - namespacing those would make every
    # previously-seen eBay item look new and re-alert the entire backlog.
    listing = {
        "itemId": f"{platform}:{item_id}",
        "title": title,
        "price": {"value": price, "currency": currency},
        "itemWebUrl": url,
        "platform": platform,
    }
    if image_url:
        listing["image"] = {"imageUrl": image_url}
    additional = [{"imageUrl": u} for u in extra_images if u]
    if additional:
        listing["additionalImages"] = additional
    shipping_value = _to_float(shipping)
    if shipping_value:
        listing["shippingOptions"] = [{"shippingCost": {"value": shipping_value}}]
    if seller:
        listing["seller"] = {"username": f"{platform}:{seller}"}
    if size:
        listing["size"] = size
    if condition:
        listing["condition"] = condition
    return listing


# ---------------------------------------------------------------------------
# ADAPTER REGISTRY
# ---------------------------------------------------------------------------

ADAPTERS = {}


def adapter(name):
    def register(fn):
        ADAPTERS[name] = fn
        return fn
    return register


def available_platforms():
    return sorted(ADAPTERS)


def _slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", text)).strip("-")


# ---------------------------------------------------------------------------
# GRAILED - public Algolia search key, served to every anonymous visitor in
# window.PUBLIC_CONFIG on grailed.com/shop/men. Best single fit for this
# bot's taste profile (Ivy-trad/tailored menswear).
# ---------------------------------------------------------------------------

GRAILED_APP_ID = "MNRWEFSS2Q"
GRAILED_SEARCH_KEY = "c89dbaddf15fe70e1941a109bf7c2a3d"
# created_at desc replica - the bot wants newest-first, not relevance.
GRAILED_INDEX = "Listing_by_date_added_production"


@adapter("grailed")
def search_grailed(saved_search):
    body = get_json(
        "grailed",
        f"https://mnrwefss2q-dsn.algolia.net/1/indexes/{GRAILED_INDEX}",
        params={
            "query": saved_search["query"],
            "hitsPerPage": 30,
            # Europe+Asia outnumber US ~2:1 on Grailed; without this the feed
            # is mostly items that ship internationally with import charges.
            "filters": 'location:"United States"',
        },
        headers={
            "X-Algolia-Application-Id": GRAILED_APP_ID,
            "X-Algolia-API-Key": GRAILED_SEARCH_KEY,
        },
    )
    if not body:
        return [], None
    listings = []
    for hit in body.get("hits", []):
        object_id = hit.get("objectID")
        cover = (hit.get("cover_photo") or {}).get("url")
        if cover:
            # Full-res covers are ~2.2MB; the resized form is ~160KB and
            # plenty for vision. Matters because every byte is downloaded
            # inside the job's 60-second billing window.
            cover += ("&" if "?" in cover else "?") + "w=800&fit=clip"
        listings.append(
            make_listing(
                "grailed",
                object_id,
                hit.get("title"),
                hit.get("price"),
                f"https://www.grailed.com/listings/{object_id}",
                image_url=cover,
                size=hit.get("size"),
                seller=(hit.get("user") or {}).get("username"),
            )
        )
    return [x for x in listings if x], body.get("nbHits")


# ---------------------------------------------------------------------------
# POSHMARK - the site's own frontend JSON endpoint, no auth of any kind.
# ---------------------------------------------------------------------------

@adapter("poshmark")
def search_poshmark(saved_search):
    request_filter = json.dumps({
        "filters": {"department": "Men", "inventory_status": ["available"]},
        "query_and_facet_filters": {},
        "query": saved_search["query"],
        "experience": "all",
        "sizeSystem": "us",
        "sort_by": "added_desc",
    })
    body = get_json(
        "poshmark",
        "https://poshmark.com/vm-rest/posts",
        params={
            "request": request_filter,
            "summarize": "true",
            "suggested_filters_count": "0",
        },
    )
    if not body:
        return [], None
    listings = []
    for post in body.get("data", []):
        post_id = post.get("id")
        title = post.get("title") or ""
        if not post_id or not title:
            continue
        price = (post.get("price_amount") or {}).get("val", post.get("price"))
        # Field name bug, confirmed live: the real key is "cover_shot"
        # (underscore) - "covershot" doesn't exist on the actual response,
        # so image_url has been None for EVERY Poshmark listing until now.
        # check_photos_with_gemini() returns None outright with zero
        # images (never a hallucinated fake result), but it means the AI
        # damage/logo/price check has never actually run on a single
        # Poshmark listing - a real, structural blind spot, not just a
        # missing-extra-angle gap. Also picking up "pictures", a full
        # gallery array sitting right there in the same response
        # (confirmed live: 6 real photos on a sample item) that was never
        # read at all.
        cover = post.get("cover_shot") or {}
        picture = cover.get("url") or cover.get("url_small") or post.get("picture_url")
        # Confirmed live: entries are dicts with a "url" key, not plain
        # strings - an isinstance(p, str) filter here silently drops every
        # single one, which is exactly what shipped first and was caught
        # by testing the actual return value instead of trusting the diff.
        all_pictures = post.get("pictures") or []
        extra_images = [
            (p.get("url") if isinstance(p, dict) else p)
            for p in all_pictures
        ]
        extra_images = [u for u in extra_images if u and u != picture]
        size = (post.get("inventory") or {}).get("size_obj", {}).get("display")
        listings.append(
            make_listing(
                "poshmark",
                post_id,
                title,
                price,
                # No url field exists; Poshmark's own anchors are slug-id.
                f"https://poshmark.com/listing/{_slugify(title)}-{post_id}",
                image_url=picture,
                extra_images=extra_images,
                size=size or post.get("size"),
                seller=(post.get("creator_id") or post.get("creator_username")),
            )
        )
    return [x for x in listings if x], None


# ---------------------------------------------------------------------------
# SHOPGOODWILL - Goodwill's national auction site. Genuine thrift-price trad
# menswear; auction format means prices start low. Public POST search API.
# ---------------------------------------------------------------------------

# Only surface an auction once it's this close to ending - per explicit user
# instruction, currentPrice on a live auction isn't a real number until the
# bidding is basically over.
SHOPGOODWILL_CLOSING_SOON_MINUTES = 60
# Flat assumed shipping+handling - per explicit user instruction, the API's
# own shippingPrice/handlingPrice fields don't reflect real cost. Midpoint
# of the $12-15 range given.
SHOPGOODWILL_ASSUMED_SHIPPING = 13.50


def _parse_shopgoodwill_remaining(remaining_str):
    """Parse ShopGoodwill's human-readable "2d 19h" / "45m" countdown into
    total minutes. Returns None if unparseable - callers should treat that
    as "unknown, don't trust it" rather than assuming it's safe."""
    if not remaining_str:
        return None
    days_match = re.search(r"(\d+)\s*d", remaining_str)
    hours_match = re.search(r"(\d+)\s*h", remaining_str)
    minutes_match = re.search(r"(\d+)\s*m", remaining_str)
    if not (days_match or hours_match or minutes_match):
        return None
    days = int(days_match.group(1)) if days_match else 0
    hours = int(hours_match.group(1)) if hours_match else 0
    minutes = int(minutes_match.group(1)) if minutes_match else 0
    return days * 24 * 60 + hours * 60 + minutes


@adapter("shopgoodwill")
def search_shopgoodwill(saved_search):
    payload = {
        "isSize": False, "isWeddingCatagory": "false", "isMultipleCategoryIds": False,
        "isFromHeaderMenuTab": False, "layout": "grid", "isFromHomePage": False,
        "searchText": saved_search["query"], "selectedGroup": "Keyword",
        "selectedCategoryIds": "", "selectedSellerIds": "",
        "lowPrice": "0", "highPrice": "999999",
        "searchBuyNowOnly": "", "searchPickupOnly": "false", "searchNoPickupOnly": "false",
        "searchOneCentShippingOnly": "false", "searchDescriptions": "false",
        "searchClosedAuctions": "false", "closedAuctionEndingDate": "1/1/2000",
        "closedAuctionDaysBack": "0", "searchCanadaShipping": "false",
        "searchInternationalShippingOnly": "false",
        "sortColumn": "1", "page": "1", "pageSize": "40", "sortDescending": "true",
        "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "true",
        "categoryLevelNo": "1", "categoryLevel": 1, "categoryId": 0, "partNumber": "",
    }
    _pace("shopgoodwill")
    try:
        resp = requests.post(
            "https://buyerapi.shopgoodwill.com/api/Search/ItemListing",
            json=payload,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("shopgoodwill request failed: %s", exc)
        return [], None
    if not resp.ok:
        logger.warning("shopgoodwill returned HTTP %s", resp.status_code)
        return [], None
    try:
        body = resp.json()
    except ValueError:
        return [], None
    results = (body.get("searchResults") or {}).get("items") or []
    listings = []
    skipped_too_early = 0
    for item in results:
        item_id = item.get("itemId")
        # currentPrice is a LIVE AUCTION BID, not a buyable price - it climbs
        # until close, so alerting on it early is alerting on a number that
        # won't hold. Live user report: got an alert for a ShopGoodwill item
        # that was still bidding, nowhere near a price they could actually
        # buy at. Only surface items close enough to closing that the
        # current bid is close to being the FINAL price - same logic a
        # human sniper uses (watch it, don't bid until the last hour).
        remaining_minutes = _parse_shopgoodwill_remaining(item.get("remainingTime"))
        if remaining_minutes is None or remaining_minutes > SHOPGOODWILL_CLOSING_SOON_MINUTES:
            skipped_too_early += 1
            continue
        listings.append(
            make_listing(
                "shopgoodwill",
                item_id,
                item.get("title"),
                item.get("currentPrice"),
                f"https://shopgoodwill.com/item/{item_id}",
                image_url=item.get("imageURL") or item.get("imageUrlString"),
                # ShopGoodwill's own shippingPrice/handlingPrice fields are
                # frequently $0 or near-$0 placeholders that don't reflect
                # real cost - confirmed unreliable by direct user
                # instruction. Always assume a flat real-world estimate
                # instead of trusting what the API reports.
                shipping=SHOPGOODWILL_ASSUMED_SHIPPING,
                seller=item.get("sellerName") or item.get("sellerId"),
            )
        )
    if skipped_too_early:
        logger.info(
            "shopgoodwill: skipped %s auction(s) more than %s min from closing",
            skipped_too_early, SHOPGOODWILL_CLOSING_SOON_MINUTES,
        )
    return [x for x in listings if x], (body.get("searchResults") or {}).get("itemCount")


# ---------------------------------------------------------------------------
# VINTED - needs an anonymous session cookie (no account) obtained by first
# requesting the homepage; the API 401s without it. That is ordinary session
# handling, not an auth bypass.
# ---------------------------------------------------------------------------

_vinted_session = None
_vinted_lock = threading.Lock()


def _get_vinted_session():
    global _vinted_session
    with _vinted_lock:
        if _vinted_session is not None:
            return _vinted_session
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        try:
            session.get("https://www.vinted.com/", timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("vinted session bootstrap failed: %s", exc)
            return None
        _vinted_session = session
        return session


@adapter("vinted")
def search_vinted(saved_search):
    session = _get_vinted_session()
    if session is None:
        return [], None
    body = get_json(
        "vinted",
        "https://www.vinted.com/api/v2/catalog/items",
        params={
            "search_text": saved_search["query"],
            "order": "newest_first",
            "per_page": 20,
            "page": 1,
        },
        session=session,
    )
    if not body:
        return [], None
    listings = []
    for item in body.get("items", []):
        # The search response carries a full "photos" gallery array, not
        # just the single cover "photo" - confirmed live (4 real photos on
        # a sample item). Missed entirely until now: the AI damage check
        # was only ever seeing one angle per Vinted listing, which is
        # exactly how a real hole/damage on a different angle got past it.
        all_photos = item.get("photos") or []
        extra_images = [p.get("url") for p in all_photos if not p.get("is_main") and p.get("url")]
        listings.append(
            make_listing(
                "vinted",
                item.get("id"),
                item.get("title"),
                (item.get("price") or {}).get("amount"),
                item.get("url"),
                image_url=(item.get("photo") or {}).get("url"),
                extra_images=extra_images,
                size=item.get("size_title"),
                seller=(item.get("user") or {}).get("login"),
            )
        )
    return [x for x in listings if x], None
