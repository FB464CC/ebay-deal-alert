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
from urllib.parse import urlencode

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
    # Tightened again from 0.35, live-tested this time rather than
    # inherited from Grailed's number: 55 real back-to-back requests
    # against the actual endpoint, explicit sleep intervals stepped down
    # 0.35 -> 0.25 -> 0.15 -> 0.08 -> 0.03s, then a genuine ZERO-sleep
    # 15-request burst using real config.json brand queries (zegna,
    # canali, loro piana, alden, edward green...). Zero 429s, zero
    # CAPTCHA/challenge pages at any interval, despite Vinted's Cloudflare
    # __cf_bm bot-management cookie being present and active. The
    # zero-sleep burst landed at a natural ~0.42s/call floor - that's
    # real network+server latency, not throttling. Landed on 0.15s
    # (4x that natural floor) rather than the tested edge, same
    # trust-but-verify margin this file already uses elsewhere - not
    # maximum-observed-safe, real headroom below it.
    # ponytail: no adaptive backoff on repeated 429s yet for Vinted
    # specifically - get_json() already logs+returns None per failed
    # call, so a real throttling event degrades gracefully rather than
    # cascading, but if 429s start showing up in the logs for real, add
    # exponential backoff here rather than re-loosening this number.
    "vinted": 0.15,
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


def _dget(obj, key, default=None):
    """dict.get() that tolerates a non-dict receiver instead of raising.

    Several chained accesses here were written as ``(x or {}).get(k)``, which
    guards against absent/falsy values but NOT a truthy scalar - if an API
    ever returns a number or string where a dict is expected, ``or {}`` keeps
    the scalar and the ``.get`` raises AttributeError. _fetch_marketplace()
    swallows that, so the whole platform silently returns zero listings for
    the run. Route every chained-get through here so a scalar degrades to
    ``default`` exactly like a missing key."""
    if not isinstance(obj, dict):
        return default
    return obj.get(key, default)


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
    description=None,
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
    # Per explicit user instruction: "not all sizes etc are in the titles.
    # take the descriptions as well." Poshmark and ShopGoodwill both return
    # a full description in the SAME search response already being fetched
    # - zero extra cost, just previously dropped on the floor. Confirmed
    # live: a real Poshmark listing's description had "Size: 54R(EU)
    # 44R(US)" and "small hole on..." - a disclosed flaw invisible to
    # every title-only check in score_listing(). Vinted/Grailed don't
    # return description at all in their search responses (checked live);
    # eBay's item_summary/search never has, only its separate per-item
    # get_item call does - see fetch_ebay_item_description().
    if description:
        listing["description"] = description
    return listing


# ---------------------------------------------------------------------------
# ADAPTER REGISTRY
# ---------------------------------------------------------------------------

ADAPTERS = {}
# Platforms whose ADAPTERS entry handles ONE saved_search per call get
# dispatched by prefetch_marketplaces() through a per-(search, platform)
# task queue - fine when a search costs one HTTP call. Grailed cost TWO
# (a live-listing query and a sold-comps query) per search, so 86 enabled
# searches meant 172 sequential HTTP round trips eating most of the ~90s
# marketplace fetch budget. BATCH_ADAPTERS holds platforms that instead
# take the FULL LIST of enabled searches and return {query: [listings]}
# for all of them in one (or a few, chunk-limited) HTTP round trips -
# prefetch_marketplaces() calls these once, outside the normal task queue,
# and excludes them from it so there's no double-dispatch.
BATCH_ADAPTERS = {}


def adapter(name):
    def register(fn):
        ADAPTERS[name] = fn
        return fn
    return register


def batch_adapter(name):
    def register(fn):
        BATCH_ADAPTERS[name] = fn
        return fn
    return register


def available_platforms():
    return sorted(ADAPTERS)


# "-term" in a saved search is eBay Browse search syntax meaning "exclude
# listings matching this". NO other marketplace understands it, and every
# adapter here was passing the raw query straight through - so on Poshmark,
# Grailed, Vinted and ShopGoodwill the exclusions did nothing at all, and
# the literal "-radio -canteen -mug" text was also being fed into their
# relevance matching as if it were something to search FOR.
#
# Confirmed against the real alert history: of 21 watch alerts ever sent,
# 15 were junk, and 12 of those came from Vinted - including "Zenith radio"
# ( -radio ), "Tudor watch brand canteen" ( -canteen ), "Hamilton Pullover
# Sweatshirt" ( -sweatshirt ), "Rolex Oyster Perpetual Bone China Coffee
# Mug" ( -mug ) and a Heath/Zenith doorbell. Every one of those was named
# explicitly in its own search's exclusion list and got through anyway.
_QUERY_EXCLUSION_RE = re.compile(r"(?:^|\s)-([A-Za-z0-9][\w'-]*)")


def split_query_exclusions(query):
    """Split an eBay-style query into (clean_query, [excluded_terms]).

    Returns the query with all "-term" tokens removed (so marketplaces get
    a clean set of words to actually match on) plus the lowercased terms
    themselves, so the caller can enforce them on the results instead."""
    query = query or ""
    terms = [m.group(1).lower() for m in _QUERY_EXCLUSION_RE.finditer(query)]
    clean = re.sub(r"\s+", " ", _QUERY_EXCLUSION_RE.sub(" ", query)).strip()
    return clean, terms


def title_matches_exclusion(title, terms):
    """True if the listing title contains any excluded term as a whole word.

    Deliberately TITLE-only, matching how the eBay-side gender/logo filters
    work. eBay's own "-term" operator matches full listing text
    (title+description+aspects), which was previously confirmed to collapse
    real inventory by 90%+ on some searches when a description merely
    mentioned the word. Whole-word so "-hat" can't kill "Thatcher"."""
    if not terms:
        return False
    haystack = (title or "").lower()
    return any(
        re.search(rf"\b{re.escape(term)}\b", haystack) for term in terms
    )


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


# Relevance-sorted sold index. Was "Listing_sold_by_high_price_production",
# which is sorted by PRICE DESCENDING - so pulling hitsPerPage=10 off it took
# the ten most expensive sales ever recorded for the query and called their
# median a typical resale value. Measured live, the overstatement was severe:
#   "alden shell cordovan"  $875 -> $270 real 180-day median  (3.2x too high)
#   "hermes tie"            $283 ->  $45 real 180-day median  (6.3x too high)
# That number feeds estimated_resale_value -> discount_pct -> the steal gate,
# so it was a systematic false-positive generator: it made ordinary listings
# look like steals against a fantasy resale price.
GRAILED_SOLD_INDEX = "Listing_sold_production"

# Only count sales from the last ~6 months. The old index mixed 2022 sales in
# with current ones; menswear resale prices move enough that a 4-year-old sale
# is not a comp for today.
SOLD_COMP_WINDOW_DAYS = 180
SOLD_COMP_HITS = 50


def fetch_grailed_sold_comps(query):
    """Real recent sold prices for this query, straight from Grailed's own
    sold-comps index - genuine historical sale prices (sold_price field),
    not an estimate. Returns (median, count) or (None, count) if fewer than
    3 comps exist (too few to trust a median on). One extra call per search,
    same pacing/cost as the main search call."""
    cutoff = int(time.time()) - SOLD_COMP_WINDOW_DAYS * 86400
    body = get_json(
        "grailed",
        f"https://mnrwefss2q-dsn.algolia.net/1/indexes/{GRAILED_SOLD_INDEX}",
        params={
            "query": query,
            "hitsPerPage": SOLD_COMP_HITS,
            "numericFilters": f"sold_at_i>{cutoff}",
            # Algolia fuzzy-matches by default, which is fine for browsing
            # and actively dangerous for price evidence. Confirmed live:
            #   "oxxford suit" -> Taylor Stitch OXFORD shirts, median $28
            #   "vass shoes"   -> 49 of 50 hits were VANS sneakers
            #   "maison margiela hat" -> Supreme x MM6 collab caps
            # Those medians were then used as "genuine sold-price data" for
            # a completely different brand - so an Oxxford suit got priced
            # against $28 oxford shirts and was blocked forever, while
            # searches whose fuzzy match landed on pricier junk rated
            # anything in budget as a Steal.
            #
            # With both off, a brand Grailed barely carries returns too few
            # real comps and the >=3 floor makes the function correctly
            # ABSTAIN instead of inventing a number. Abstaining is the right
            # failure mode for price evidence.
            "typoTolerance": "false",
            "removeWordsIfNoResults": "none",
        },
        headers={
            "X-Algolia-Application-Id": GRAILED_APP_ID,
            "X-Algolia-API-Key": GRAILED_SEARCH_KEY,
        },
    )
    if not body:
        return None, 0
    prices = [h.get("sold_price") for h in body.get("hits", []) if h.get("sold_price")]
    if len(prices) < 3:
        return None, len(prices)
    prices.sort()
    return prices[len(prices) // 2], len(prices)


def _grailed_hit_to_listing(hit, sold_median, sold_count):
    """One Algolia hit -> one normalized listing. Shared by both the
    single-search and batched adapters below so there's exactly one place
    that knows Grailed's hit shape, not two copies that can drift."""
    object_id = hit.get("objectID")
    cover = _dget(_dget(hit, "cover_photo"), "url")
    if cover:
        # Full-res covers are ~2.2MB; the resized form is ~160KB and
        # plenty for vision. Matters because every byte is downloaded
        # inside the job's 60-second billing window.
        cover += ("&" if "?" in cover else "?") + "w=800&fit=clip"
    user = _dget(hit, "user") or {}
    # Confirmed live: each hit carries a real per-listing "shipping.us"
    # block (amount + enabled) - a $50 item with $7.99 US shipping
    # enabled was showing as a flat $50 item with make_listing's 0.0
    # shipping default. enabled:false means the seller's price already
    # includes shipping (free-shipping listing), so 0.0 is correct there.
    us_shipping = _dget(_dget(hit, "shipping"), "us") or {}
    shipping_cost = (_dget(us_shipping, "amount") or 0.0) if _dget(us_shipping, "enabled") else 0.0
    listing = make_listing(
        "grailed",
        object_id,
        hit.get("title"),
        hit.get("price"),
        f"https://www.grailed.com/listings/{object_id}",
        image_url=cover,
        size=hit.get("size"),
        seller=_dget(user, "username"),
        shipping=shipping_cost,
    )
    if listing:
        # Real sold-comp data and seller trust signals, both sitting in
        # fields Grailed already returns - just never extracted before.
        if sold_median is not None:
            listing["sold_comp_median"] = sold_median
            listing["sold_comp_count"] = sold_count
        seller_score = _dget(user, "seller_score") or {}
        listing["seller_trusted"] = bool(_dget(user, "trusted_seller"))
        listing["seller_rating"] = _dget(seller_score, "rating_average")
        listing["seller_total_sales"] = _dget(user, "total_bought_and_sold")
    return listing


@adapter("grailed")
def search_grailed(saved_search):
    """Single-search fallback - kept working (and still directly testable
    in isolation) even though prefetch_marketplaces() routes real runs
    through search_grailed_batch() below via BATCH_ADAPTERS instead."""
    query, _excluded = split_query_exclusions(saved_search["query"])
    body = get_json(
        "grailed",
        f"https://mnrwefss2q-dsn.algolia.net/1/indexes/{GRAILED_INDEX}",
        params={
            "query": query,
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
    sold_median, sold_count = fetch_grailed_sold_comps(query)
    listings = [_grailed_hit_to_listing(hit, sold_median, sold_count) for hit in body.get("hits", [])]
    return [x for x in listings if x], body.get("nbHits")


# Confirmed live: a multi-query request with 60 sub-queries returns HTTP 400
# ("Too many queries in multi query request"); 50 is the real ceiling.
ALGOLIA_MAX_BATCH = 50
ALGOLIA_MULTI_QUERY_URL = f"https://{GRAILED_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"


def _algolia_multi_query(sub_requests):
    """POST up to ALGOLIA_MAX_BATCH sub-queries per HTTP call, chunking as
    needed, and return one result dict per sub-request in the SAME ORDER
    they were submitted (None for any that failed). Never raises - same
    "adapters must never break the run" contract as get_json()."""
    results = []
    for i in range(0, len(sub_requests), ALGOLIA_MAX_BATCH):
        chunk = sub_requests[i:i + ALGOLIA_MAX_BATCH]
        _pace("grailed")
        # One short retry on a 429, mirroring search_ebay(): a single retry
        # after a brief sleep costs little and can save up to ALGOLIA_MAX_BATCH
        # sub-queries at once if a transient burst trips Algolia's limit.
        for attempt in range(2):
            try:
                resp = requests.post(
                    ALGOLIA_MULTI_QUERY_URL,
                    json={"requests": chunk},
                    headers={
                        "User-Agent": USER_AGENT, "Accept": "application/json",
                        "X-Algolia-Application-Id": GRAILED_APP_ID,
                        "X-Algolia-API-Key": GRAILED_SEARCH_KEY,
                    },
                    timeout=HTTP_TIMEOUT,
                )
            except requests.RequestException as exc:
                logger.warning("grailed batch request failed: %s", exc)
                resp = None
                break
            if resp.status_code == 429 and attempt == 0:
                time.sleep(2)
                continue
            break
        if resp is None:
            results.extend([None] * len(chunk))
            continue
        if resp.status_code == 429:
            logger.warning(
                "grailed batch rate limited (429) after retry, dropping %s sub-queries: %s",
                len(chunk), resp.text[:300],
            )
            results.extend([None] * len(chunk))
            continue
        if not resp.ok:
            logger.warning("grailed batch returned HTTP %s: %s", resp.status_code, resp.text[:300])
            results.extend([None] * len(chunk))
            continue
        try:
            body = resp.json()
        except ValueError:
            logger.warning("grailed batch returned non-JSON body")
            results.extend([None] * len(chunk))
            continue
        chunk_results = list(body.get("results") or [])
        # Defensive: if Algolia ever returns a different count than
        # requested, pad/truncate rather than let a later zip() misalign
        # and silently attribute one search's results to another.
        if len(chunk_results) != len(chunk):
            logger.warning(
                "grailed batch result count mismatch: sent %s sub-queries, got %s results back",
                len(chunk), len(chunk_results),
            )
            chunk_results += [None] * (len(chunk) - len(chunk_results))
            chunk_results = chunk_results[:len(chunk)]
        results.extend(chunk_results)
    return results


@batch_adapter("grailed")
def search_grailed_batch(saved_searches):
    """Same live-search + sold-comps pair per query as search_grailed(),
    but for EVERY grailed-enabled search in 1-4 HTTP round trips via
    Algolia's multi-query endpoint instead of one round trip PER search.

    Measured live: 86 enabled searches x 2 queries each (live + sold-comps)
    = 172 sequential calls, ~60s of the ~90s marketplace fetch budget -
    Grailed alone was eating two-thirds of it. That's why
    prefetch_marketplaces() needed a rotating start-offset in the first
    place: the budget cutoff was truncating the search list every run, and
    the offset only spread WHICH searches got cut, it never stopped it
    from happening. Batching frees that budget for every other platform
    and search instead of just moving the truncation point around.

    Returns {query: [listings]} covering every search passed in."""
    cutoff = int(time.time()) - SOLD_COMP_WINDOW_DAYS * 86400
    clean_queries = [split_query_exclusions(s["query"])[0] for s in saved_searches]

    sub_requests = []
    for query in clean_queries:
        sub_requests.append({
            "indexName": GRAILED_INDEX,
            "params": urlencode({
                "query": query,
                "hitsPerPage": 30,
                "filters": 'location:"United States"',
            }),
        })
        sub_requests.append({
            "indexName": GRAILED_SOLD_INDEX,
            "params": urlencode({
                "query": query,
                "hitsPerPage": SOLD_COMP_HITS,
                "numericFilters": f"sold_at_i>{cutoff}",
                "typoTolerance": "false",
                "removeWordsIfNoResults": "none",
            }),
        })

    raw_results = _algolia_multi_query(sub_requests)

    out = {}
    for i, saved_search in enumerate(saved_searches):
        query = saved_search["query"]  # the ORIGINAL (unstripped) query - the
        # dict key prefetch_marketplaces() looks results up by everywhere else.
        live_result = raw_results[2 * i]
        sold_result = raw_results[2 * i + 1]

        sold_median = sold_count = None
        if sold_result:
            prices = sorted(
                h.get("sold_price") for h in sold_result.get("hits", []) if h.get("sold_price")
            )
            if len(prices) >= 3:
                sold_median, sold_count = prices[len(prices) // 2], len(prices)

        if not live_result:
            out.setdefault(query, [])
            continue
        listings = [
            _grailed_hit_to_listing(hit, sold_median, sold_count)
            for hit in live_result.get("hits", [])
        ]
        out.setdefault(query, []).extend(x for x in listings if x)

    return out


# ---------------------------------------------------------------------------
# POSHMARK - the site's own frontend JSON endpoint, no auth of any kind.
# ---------------------------------------------------------------------------

# Poshmark charges every buyer this same flat rate regardless of item -
# there's no per-listing shipping field in the API to read instead (checked
# live). Confirmed against a real alert: a $22 item logged with $0.0
# shipping actually sold for a reported $30 total - $22 + $7.97 = $29.97.
POSHMARK_ASSUMED_SHIPPING = 7.97


@adapter("poshmark")
def search_poshmark(saved_search):
    request_filter = json.dumps({
        "filters": {"department": "Men", "inventory_status": ["available"]},
        "query_and_facet_filters": {},
        "query": split_query_exclusions(saved_search["query"])[0],
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
        price = _dget(_dget(post, "price_amount"), "val", _dget(post, "price"))
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
        cover = _dget(post, "cover_shot")
        picture = _dget(cover, "url") or _dget(cover, "url_small") or _dget(post, "picture_url")
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
        size = _dget(_dget(_dget(post, "inventory"), "size_obj"), "display")
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
                shipping=POSHMARK_ASSUMED_SHIPPING,
                description=post.get("description"),
            )
        )
    return [x for x in listings if x], None


# ---------------------------------------------------------------------------
# SHOPGOODWILL - Goodwill's national auction site. Genuine thrift-price trad
# menswear; auction format means prices start low. Public POST search API.
# ---------------------------------------------------------------------------

# Only surface an auction once it's this close to ending - per explicit user
# instruction, currentPrice on a live auction isn't a real number until the
# bidding is basically over. Tightened 60 -> 30: even at 60 min out, live
# user report was still landing alerts on prices that had already moved on
# by the time they looked. Contested auctions (numBids > 0) get an even
# tighter window - a bid war is the strongest signal the current price is
# NOT close to final, so those only surface once truly down to the wire.
SHOPGOODWILL_CLOSING_SOON_MINUTES = 30
SHOPGOODWILL_CONTESTED_CLOSING_SOON_MINUTES = 15
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
        "searchText": split_query_exclusions(saved_search["query"])[0],
        "selectedGroup": "Keyword",
        "selectedCategoryIds": "", "selectedSellerIds": "",
        "lowPrice": "0", "highPrice": "999999",
        "searchBuyNowOnly": "", "searchPickupOnly": "false", "searchNoPickupOnly": "false",
        "searchOneCentShippingOnly": "false", "searchDescriptions": "false",
        "searchClosedAuctions": "false", "closedAuctionEndingDate": "1/1/2000",
        "closedAuctionDaysBack": "0", "searchCanadaShipping": "false",
        "searchInternationalShippingOnly": "false",
        # sortDescending was "true" - confirmed live this sorts by
        # remainingTime DESCENDING (ending latest first: 6-7 days out),
        # which is the exact opposite of useful against a 60-minute
        # closing-soon filter. "false" sorts ending-SOONEST first
        # (confirmed live: 46s, 1m46s, 2m46s remaining) - what
        # SHOPGOODWILL_CLOSING_SOON_MINUTES actually needs to find
        # anything. This is almost certainly why ShopGoodwill has been
        # pulling ~4 listings/run against thousands from every other
        # platform - it was showing this filter its least relevant page,
        # every single call.
        "sortColumn": "1", "page": "1", "pageSize": "40", "sortDescending": "false",
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
    results = _dget(_dget(body, "searchResults"), "items") or []
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
        num_bids = item.get("numBids") or 0
        threshold = SHOPGOODWILL_CONTESTED_CLOSING_SOON_MINUTES if num_bids > 0 else SHOPGOODWILL_CLOSING_SOON_MINUTES
        if remaining_minutes is None or remaining_minutes > threshold:
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
                description=item.get("description"),
            )
        )
    if skipped_too_early:
        logger.info(
            "shopgoodwill: skipped %s auction(s) too far from closing "
            "(>%s min uncontested, >%s min with bids already on it)",
            skipped_too_early, SHOPGOODWILL_CLOSING_SOON_MINUTES, SHOPGOODWILL_CONTESTED_CLOSING_SOON_MINUTES,
        )
    return [x for x in listings if x], _dget(_dget(body, "searchResults"), "itemCount")


# ---------------------------------------------------------------------------
# VINTED - needs an anonymous session cookie (no account) obtained by first
# requesting the homepage; the API 401s without it. That is ordinary session
# handling, not an auth bypass.
# ---------------------------------------------------------------------------

# Vinted resolves real shipping only at checkout against the buyer's address,
# so no catalog field exists to read (checked live) - same situation that made
# SHOPGOODWILL_ASSUMED_SHIPPING necessary. Vinted US buyer-paid shipping tiers
# run ~$3.99 (small) to ~$7.99 (large); the trad-menswear this bot hunts
# (blazers, knitwear, shoes) lands mostly in the medium tier, so $5.99 is the
# defensible midpoint. Assumed ON TOP of the buyer-protection fee below.
VINTED_ASSUMED_SHIPPING = 5.99

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


def _vinted_item_to_listing(item):
    # The search response carries a full "photos" gallery array, not
    # just the single cover "photo" - confirmed live (4 real photos on
    # a sample item). Missed entirely until now: the AI damage check
    # was only ever seeing one angle per Vinted listing, which is
    # exactly how a real hole/damage on a different angle got past it.
    all_photos = item.get("photos") or []
    extra_images = [p.get("url") for p in all_photos if not p.get("is_main") and p.get("url")]
    # Real actual shipping cost isn't in the catalog response at all -
    # Vinted only resolves that at checkout against the buyer's address.
    # But "total_item_price" IS present and real (confirmed live): it's
    # price + Vinted's mandatory buyer-protection service_fee, an extra
    # cost every buyer definitely pays on top of the listed price. That
    # delta alone still understates true landed cost by whatever shipping
    # ends up being (~$5-10 real), which made Vinted listings win deal
    # comparisons against Poshmark/ShopGoodwill's honest flat shipping.
    # So add VINTED_ASSUMED_SHIPPING on top, mirroring the other two.
    item_price = _to_float(_dget(_dget(item, "price"), "amount")) or 0.0
    total_price = _to_float(_dget(_dget(item, "total_item_price"), "amount"))
    service_fee = (total_price - item_price) if total_price is not None else 0.0
    return make_listing(
        "vinted",
        item.get("id"),
        item.get("title"),
        _dget(_dget(item, "price"), "amount"),
        item.get("url"),
        image_url=_dget(_dget(item, "photo"), "url"),
        extra_images=extra_images,
        size=item.get("size_title"),
        seller=_dget(_dget(item, "user"), "login"),
        shipping=service_fee + VINTED_ASSUMED_SHIPPING,
    )


@adapter("vinted")
def search_vinted(saved_search):
    session = _get_vinted_session()
    if session is None:
        return [], None
    query_text = split_query_exclusions(saved_search["query"])[0]
    listings = []
    # Confirmed live: Vinted's real per-page cap is 96 (100 gets silently
    # clamped down to 96). Two pages = ~192 results per search for one
    # extra HTTP call. User observation: Vinted sellers are systematically
    # underinformed/underpriced compared to the other platforms, worth
    # hounding harder specifically here - this is that "harder".
    for page in (1, 2):
        body = get_json(
            "vinted",
            "https://www.vinted.com/api/v2/catalog/items",
            params={
                "search_text": query_text,
                "order": "newest_first",
                "per_page": 96,
                "page": page,
            },
            session=session,
        )
        items = (body or {}).get("items", [])
        listings.extend(_vinted_item_to_listing(item) for item in items)
        if len(items) < 96:
            # A short page means there's nothing left - don't waste a call
            # confirming an empty page 2 exists.
            break
    return [x for x in listings if x], None


# facebook_marketplace lives in its own module (Playwright is a heavy dep this
# requests-only file must not import at module level). Imported here at the
# bottom so the @batch_adapter("facebook") decorator inside it registers
# against a fully-initialized platforms module - avoids a circular import.
import facebook_marketplace  # noqa: E402,F401
