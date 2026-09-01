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

OfferUp and Depop have no public JSON search API - their listing data is
embedded in the rendered search page (a __NEXT_DATA__ blob and a Next.js
RSC flight stream respectively), so those two adapters fetch the HTML via
scrapling's plain-HTTP Fetcher (TLS-fingerprint spoofing, no browser)
instead of `requests`. Same listing shape, same never-raise contract.

READ-ONLY. These adapters only ever fetch public search results. Nothing
here logs in, creates accounts, bids, buys, offers, or messages.
"""

import json
import logging
import math
import os
import re
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

logger = logging.getLogger("platforms")

# Batch HTML fetches run in a private daemon thread so a wedged scraper cannot
# hold the caller past its deadline.  A caller's threading.local health context
# cannot cross that thread boundary, so stamp the platform directly onto every
# LogRecord emitted by the batch thread.  Health handlers can then attribute
# transport and parser warnings without importing this module's caller.
_marketplace_log_context = threading.local()


class _MarketplaceLogContextFilter(logging.Filter):
    def filter(self, record):
        platform = getattr(_marketplace_log_context, "platform", None)
        if platform and not hasattr(record, "marketplace_platform"):
            record.marketplace_platform = platform
        return True


logger.addFilter(_MarketplaceLogContextFilter())

# A normal browser UA. These endpoints serve the sites' own public web
# frontends; a blank/python UA gets 403'd by ordinary WAF defaults. This is
# not fingerprint spoofing - there is no rotation, no proxying, no cookie
# laundering. One honest client string, one IP, polite pacing.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 8


def _configured_marketplace_fetch_budget():
    """Mirror the caller's marketplace deadline without importing it.

    ``ebay_deal_alert`` imports this module, so importing the caller here would
    create a cycle.  Reading the same config value lets long-running batch
    adapters stop before the caller's daemon-thread hard stop instead of
    returning later and mutating results that have already been consumed.
    """
    try:
        with Path(__file__).resolve().with_name("config.json").open("r", encoding="utf-8") as config_file:
            value = float(json.load(config_file).get("MARKETPLACE_FETCH_BUDGET_SECONDS", 90))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 90.0
    return max(0.0, value)


MARKETPLACE_BATCH_DEADLINE_SECONDS = _configured_marketplace_fetch_budget()

# Minimum seconds between requests to the same platform, enforced process-wide
# across the thread pool. Keeps us a polite guest instead of hammering.
_MIN_INTERVAL = {
    "grailed": 0.35,
    "depop": 0.35,
    "offerup": 0.50,
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
    # UPDATE: 429s did start showing up in the logs for real (43-62/run
    # measured live) - exponential backoff added (_register_rate_limit,
    # _backoff_multiplier below), this base number is untouched.
    "vinted": 0.15,
    "poshmark": 0.60,
    "mercari": 0.60,
    "vestiaire": 0.50,
    "shopgoodwill": 0.50,
}
_DEFAULT_MIN_INTERVAL = 0.5

_rate_lock = threading.Lock()
_last_call = {}
# Real live pattern this exists for: Vinted alone measured 43-62 429s per
# run out of ~86 calls (checked against 4 real GitHub Actions runs) at the
# fixed 0.15s pace tuned for a clean, uncontested single-threaded burst test
# - real production load is dozens of searches firing concurrently across
# threads, a condition that tuning number was never tested against. Was a
# known, explicitly named ceiling (see the "ponytail: no adaptive backoff
# on repeated 429s yet" comment on _MIN_INTERVAL above) until the 429s
# actually showed up in the logs for real. Doubling the effective pace on
# every 429 (capped at _MAX_BACKOFF_MULTIPLIER) means a run backs off for
# real instead of continuing to hammer the same wall at the same rate for
# every remaining call to that platform. Resets naturally every run - this
# module-level dict doesn't survive past one process, and each GH Actions
# run is a fresh process, so there's no cross-run decay logic needed.
_backoff_multiplier = {}
_MAX_BACKOFF_MULTIPLIER = 32


def _register_rate_limit(platform):
    with _rate_lock:
        current = _backoff_multiplier.get(platform, 1)
        _backoff_multiplier[platform] = min(current * 2, _MAX_BACKOFF_MULTIPLIER)
        return _backoff_multiplier[platform]


def _pace(platform):
    """Block until this platform's minimum inter-request interval - scaled
    up by any live backoff multiplier from a recent 429 - has passed."""
    interval = _MIN_INTERVAL.get(platform, _DEFAULT_MIN_INTERVAL)
    while True:
        with _rate_lock:
            effective_interval = interval * _backoff_multiplier.get(platform, 1)
            now = time.monotonic()
            previous = _last_call.get(platform, 0.0)
            wait = effective_interval - (now - previous)
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
        multiplier = _register_rate_limit(platform)
        logger.warning(
            "%s rate limited (429) - backing off, next calls to this platform "
            "paced %sx slower for the rest of this run",
            platform, multiplier,
        )
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
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raw = str(value).strip()
        negative = raw.startswith("-")
        cleaned = re.sub(r"[^\d.]", "", raw)
        if negative:
            cleaned = "-" + cleaned if cleaned else ""
        if not cleaned or cleaned.count(".") > 1:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    return number if number > 0 and math.isfinite(number) else None


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
    if not item_id or not title or price is None or price <= 0 or not url:
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
    if shipping_value is not None and shipping_value > 0:
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
    """Return every registered fetch lane, regardless of dispatch strategy."""
    return sorted(ADAPTERS.keys() | BATCH_ADAPTERS.keys())


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
_QUERY_EXCLUSION_RE = re.compile(
    r'(?:^|\s)-(?:"([^"]+)"|([A-Za-z0-9][\w\'-]*))'
)


def split_query_exclusions(query):
    """Split an eBay-style query into (clean_query, [excluded_terms]).

    Returns the query with all "-term" tokens removed (so marketplaces get
    a clean set of words to actually match on) plus the lowercased terms
    themselves, so the caller can enforce them on the results instead."""
    query = query or ""
    terms = [
        (match.group(1) or match.group(2)).lower()
        for match in _QUERY_EXCLUSION_RE.finditer(query)
    ]
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
SHOPGOODWILL_RATE_LIMIT_STATE_PATH = Path(__file__).resolve().with_name("shopgoodwill_rate_limit_state.json")
SHOPGOODWILL_BACKOFF_INITIAL_MINUTES = 30
SHOPGOODWILL_BACKOFF_MAX_MINUTES = 120
SHOPGOODWILL_URL = "https://buyerapi.shopgoodwill.com/api/Search/ItemListing"
SHOPGOODWILL_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://shopgoodwill.com",
    "Referer": "https://shopgoodwill.com/",
}

_shopgoodwill_state_lock = threading.Lock()


def _read_shopgoodwill_rate_limit_state():
    with _shopgoodwill_state_lock:
        if not SHOPGOODWILL_RATE_LIMIT_STATE_PATH.exists():
            return {}
        try:
            with SHOPGOODWILL_RATE_LIMIT_STATE_PATH.open("r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid ShopGoodwill rate-limit state: %s", exc)
            return {}
    if not isinstance(state, dict):
        logger.warning("ShopGoodwill rate-limit state is %s, not an object - ignoring", type(state).__name__)
        return {}
    return state


def _write_shopgoodwill_rate_limit_state(state):
    with _shopgoodwill_state_lock:
        try:
            with SHOPGOODWILL_RATE_LIMIT_STATE_PATH.open("w", encoding="utf-8") as f:
                json.dump(state, f)
                f.write("\n")
        except OSError as exc:
            logger.warning("Failed to write ShopGoodwill rate-limit state: %s", exc)


def shopgoodwill_circuit_breaker_allows_calls():
    state = _read_shopgoodwill_rate_limit_state()
    now_ts = time.time()
    blocked_until = state.get("blocked_until_ts", 0)
    if not isinstance(blocked_until, (int, float)) or blocked_until != blocked_until:
        blocked_until = 0
    if blocked_until > now_ts + SHOPGOODWILL_BACKOFF_MAX_MINUTES * 60:
        logger.warning("ShopGoodwill circuit breaker has an impossible future timestamp - clearing it")
        _write_shopgoodwill_rate_limit_state({"blocked_until_ts": 0, "consecutive_block_streak": 0})
        return True
    if now_ts < blocked_until:
        logger.info(
            "ShopGoodwill circuit breaker: cooldown active for ~%s more min (streak %s); blocked, not zero results",
            round((blocked_until - now_ts) / 60), state.get("consecutive_block_streak", 0),
        )
        return False
    return True


def _trip_shopgoodwill_circuit_breaker(status):
    state = _read_shopgoodwill_rate_limit_state()
    streak = state.get("consecutive_block_streak", 0)
    if not isinstance(streak, int) or streak < 0:
        streak = 0
    streak += 1
    minutes = min(SHOPGOODWILL_BACKOFF_INITIAL_MINUTES * (2 ** (streak - 1)), SHOPGOODWILL_BACKOFF_MAX_MINUTES)
    _write_shopgoodwill_rate_limit_state({
        "blocked_until_ts": time.time() + minutes * 60,
        "consecutive_block_streak": streak,
        "last_status": status,
    })
    logger.warning(
        "ShopGoodwill circuit breaker: persistent HTTP %s after retry (streak %s), backing off %s min",
        status, streak, minutes,
    )


def _clear_shopgoodwill_circuit_breaker_if_tripped():
    state = _read_shopgoodwill_rate_limit_state()
    if state.get("consecutive_block_streak"):
        logger.info("ShopGoodwill circuit breaker: request succeeded, clearing prior block streak")
        _write_shopgoodwill_rate_limit_state({"blocked_until_ts": 0, "consecutive_block_streak": 0})


def _retry_after_seconds(headers):
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if not value:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, int(parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return None


def _parse_shopgoodwill_remaining(remaining_str):
    """Parse ShopGoodwill's human-readable "2d 19h" / "45m" / "46s" countdown
    into total minutes (rounded down - a sub-minute remainder still counts
    as "0 minutes left", the most urgent case, not "unparseable"). Returns
    None if unparseable - callers should treat that as "unknown, don't
    trust it" rather than assuming it's safe.

    Real live bug: confirmed live values include bare "46s" with no d/h/m
    unit at all - with no seconds capture, that returned None and got
    thrown out as unparseable, silently dropping every auction in its
    final minute. That's exactly the down-to-the-wire case the closing-
    soon filter exists to catch; "1m46s" survived only because the minutes
    regex still found an "m" to match."""
    if not remaining_str:
        return None
    days_match = re.search(r"(\d+)\s*d", remaining_str)
    hours_match = re.search(r"(\d+)\s*h", remaining_str)
    minutes_match = re.search(r"(\d+)\s*m", remaining_str)
    seconds_match = re.search(r"(\d+)\s*s", remaining_str)
    if not (days_match or hours_match or minutes_match or seconds_match):
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
    if not shopgoodwill_circuit_breaker_allows_calls():
        return [], None
    try:
        from scrapling.fetchers import Fetcher
    except ImportError:
        logger.warning("shopgoodwill skipped: scrapling is not installed")
        return [], None
    proxy_url = os.environ.get("SHOPGOODWILL_PROXY_URL")
    resp = None
    for attempt in range(2):
        _pace("shopgoodwill")
        # Another worker may have opened the process-wide breaker while this
        # one waited for pacing. Recheck immediately before touching the site.
        if not shopgoodwill_circuit_breaker_allows_calls():
            return [], None
        try:
            resp = Fetcher.post(
                SHOPGOODWILL_URL,
                json=payload,
                headers=SHOPGOODWILL_HEADERS,
                timeout=HTTP_TIMEOUT,
                proxy=proxy_url,
            )
        except Exception as exc:
            logger.warning("shopgoodwill request failed: %s", exc)
            return [], None
        status = resp.status
        if status not in (403, 429):
            break
        multiplier = _register_rate_limit("shopgoodwill") if status == 429 else None
        if attempt == 0:
            delay = _retry_after_seconds(getattr(resp, "headers", None))
            delay = 2 if delay is None else min(delay, SHOPGOODWILL_BACKOFF_MAX_MINUTES * 60)
            logger.warning(
                "shopgoodwill transient HTTP %s block; retrying once after %ss%s",
                status, delay, f" and pacing {multiplier}x slower" if multiplier else "",
            )
            time.sleep(delay)
            continue
        _trip_shopgoodwill_circuit_breaker(status)
        return [], None
    if resp.status != 200:
        logger.warning("shopgoodwill request failed with HTTP %s; blocked/failed, not zero results", resp.status)
        return [], None
    _clear_shopgoodwill_circuit_breaker_if_tripped()
    try:
        body = resp.json()
    except Exception:
        logger.warning("shopgoodwill returned HTTP 200 with a non-JSON body; failed, not zero results")
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
        # _to_float, not a raw `or 0` - every other numeric field from this
        # response is routed through it before comparison (this request's
        # own payload is all-strings, e.g. "pageSize": "40", so a stringly-
        # typed "numBids" in the response is plausible). A raw string
        # compared with `> 0` raises TypeError, which propagates out of
        # this function uncaught and gets caught by _fetch_marketplace's
        # blanket except - silently discarding EVERY ShopGoodwill result
        # for the whole search, not just this one item.
        num_bids = _to_float(item.get("numBids")) or 0
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
    item_count = _dget(_dget(body, "searchResults"), "itemCount")
    if item_count == 0:
        logger.info("shopgoodwill: genuine zero-result response for query %r", saved_search["query"])
    else:
        logger.info("shopgoodwill: HTTP 200 returned %s total result(s), %s on this page", item_count, len(results))
    return [x for x in listings if x], item_count


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
VINTED_RATE_LIMIT_STATE_PATH = Path(__file__).resolve().with_name("vinted_rate_limit_state.json")
VINTED_BACKOFF_INITIAL_MINUTES = 30
VINTED_BACKOFF_MAX_MINUTES = 120
VINTED_CATALOG_URL = "https://www.vinted.com/api/v2/catalog/items"
# Both are issued by a healthy anonymous homepage bootstrap and are required
# for catalog calls.  A Cloudflare cookie alone can also be present on a
# challenge page, so it is deliberately not enough to accept a session.
VINTED_REQUIRED_ANONYMOUS_COOKIES = frozenset({"anon_id", "access_token_web"})

_vinted_thread_state = threading.local()
_vinted_bootstrap_lock = threading.Lock()
_vinted_state_lock = threading.Lock()


def _read_vinted_rate_limit_state():
    with _vinted_state_lock:
        if not VINTED_RATE_LIMIT_STATE_PATH.exists():
            return {}
        try:
            with VINTED_RATE_LIMIT_STATE_PATH.open("r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid Vinted rate-limit state: %s", exc)
            return {}
    if not isinstance(state, dict):
        logger.warning("Vinted rate-limit state is %s, not an object - ignoring", type(state).__name__)
        return {}
    return state


def _write_vinted_rate_limit_state(state):
    with _vinted_state_lock:
        try:
            with VINTED_RATE_LIMIT_STATE_PATH.open("w", encoding="utf-8") as f:
                json.dump(state, f)
                f.write("\n")
        except OSError as exc:
            logger.warning("Failed to write Vinted rate-limit state: %s", exc)


def vinted_circuit_breaker_allows_calls():
    state = _read_vinted_rate_limit_state()
    now_ts = time.time()
    blocked_until = state.get("blocked_until_ts", 0)
    if not isinstance(blocked_until, (int, float)) or blocked_until != blocked_until:
        blocked_until = 0
    if blocked_until > now_ts + VINTED_BACKOFF_MAX_MINUTES * 60:
        logger.warning("Vinted circuit breaker has an impossible future timestamp - clearing it")
        _write_vinted_rate_limit_state({"blocked_until_ts": 0, "consecutive_block_streak": 0})
        return True
    if now_ts < blocked_until:
        logger.info(
            "Vinted circuit breaker: cooldown active for ~%s more min (streak %s); blocked, not zero results",
            round((blocked_until - now_ts) / 60), state.get("consecutive_block_streak", 0),
        )
        return False
    return True


# A single query failing its own built-in one-retry (two 429s back to back)
# is normal load-shedding noise - every "healthy" run already sees 5-12 429s
# across ~90+ searches on 8 concurrent workers. Tripping the whole-lane
# breaker on that one query blanked the entire run (confirmed live: one
# streak-1 trip produced six straight 0-listing runs). Require several
# DISTINCT queries to fail in a row before treating it as a real block.
VINTED_TRIP_THRESHOLD = 5
_vinted_failure_lock = threading.Lock()
_vinted_consecutive_query_failures = 0


def _note_vinted_query_success():
    global _vinted_consecutive_query_failures
    with _vinted_failure_lock:
        _vinted_consecutive_query_failures = 0
    _clear_vinted_circuit_breaker_if_tripped()


def _note_vinted_query_failure(status):
    global _vinted_consecutive_query_failures
    with _vinted_failure_lock:
        _vinted_consecutive_query_failures += 1
        count = _vinted_consecutive_query_failures
    if count >= VINTED_TRIP_THRESHOLD:
        _trip_vinted_circuit_breaker(status)
    else:
        logger.info(
            "vinted query failed (HTTP %s) after retry - %s/%s consecutive before tripping breaker",
            status, count, VINTED_TRIP_THRESHOLD,
        )


def _trip_vinted_circuit_breaker(status):
    state = _read_vinted_rate_limit_state()
    streak = state.get("consecutive_block_streak", 0)
    if not isinstance(streak, int) or streak < 0:
        streak = 0
    streak += 1
    minutes = min(VINTED_BACKOFF_INITIAL_MINUTES * (2 ** (streak - 1)), VINTED_BACKOFF_MAX_MINUTES)
    _write_vinted_rate_limit_state({
        "blocked_until_ts": time.time() + minutes * 60,
        "consecutive_block_streak": streak,
        "last_status": status,
    })
    logger.warning(
        "Vinted circuit breaker: persistent HTTP %s after retry (streak %s), backing off %s min",
        status, streak, minutes,
    )


def _clear_vinted_circuit_breaker_if_tripped():
    state = _read_vinted_rate_limit_state()
    if state.get("consecutive_block_streak"):
        logger.info("Vinted circuit breaker: request succeeded, clearing prior block streak")
        _write_vinted_rate_limit_state({"blocked_until_ts": 0, "consecutive_block_streak": 0})


def _discard_vinted_session(session):
    if getattr(_vinted_thread_state, "session", None) is session:
        _vinted_thread_state.session = None
        manager = getattr(_vinted_thread_state, "session_manager", None)
        _vinted_thread_state.session_manager = None
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


def _get_vinted_session():
    # FetcherSession uses curl_cffi's browser TLS fingerprint and retains the
    # homepage cookies for catalog calls.  The caller runs eight marketplace
    # workers concurrently, so each worker owns and reuses only its own
    # session; the lock merely avoids an eight-request homepage bootstrap
    # burst at process start.
    session = getattr(_vinted_thread_state, "session", None)
    if session is not None:
        return session
    with _vinted_bootstrap_lock:
        session = getattr(_vinted_thread_state, "session", None)
        if session is not None:
            return session
        try:
            from scrapling.fetchers import FetcherSession
        except ImportError:
            logger.warning("vinted skipped: scrapling is not installed")
            return None
        manager = FetcherSession(timeout=HTTP_TIMEOUT, retries=1)
        session = None
        try:
            session = manager.__enter__()
            _vinted_thread_state.session = session
            _vinted_thread_state.session_manager = manager
            resp = session.get("https://www.vinted.com/", timeout=HTTP_TIMEOUT)
        except Exception as exc:
            logger.warning("vinted session bootstrap failed: %s", exc)
            if session is not None:
                _discard_vinted_session(session)
            return None
        if not 200 <= resp.status < 300:
            logger.warning("vinted session bootstrap failed with HTTP %s", resp.status)
            _discard_vinted_session(session)
            return None
        cookie_names = set(getattr(resp, "cookies", {}) or {})
        missing = VINTED_REQUIRED_ANONYMOUS_COOKIES - cookie_names
        if missing:
            logger.warning(
                "vinted session bootstrap returned no usable anonymous session cookies (missing %s)",
                ", ".join(sorted(missing)),
            )
            _discard_vinted_session(session)
            return None
        return session


def _get_vinted_catalog_page(session, params):
    """Fetch one catalog page, returning ``(body, live_session)``.

    A 429 is retried once after Vinted's Retry-After (or a short fallback),
    then persisted into the cross-run 30/60/120-minute circuit breaker.  A
    challenged/403 session is discarded and bootstrapped once from scratch.
    """
    for attempt in range(2):
        if not vinted_circuit_breaker_allows_calls():
            return None, session
        _pace("vinted")
        # Another worker may have tripped the process-wide breaker while this
        # one was sleeping in _pace().
        if not vinted_circuit_breaker_allows_calls():
            return None, session
        try:
            resp = session.get(
                VINTED_CATALOG_URL,
                params=params,
                headers={"Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("vinted request failed: %s", exc)
            return None, session
        status = resp.status
        if status not in (403, 429):
            if not 200 <= status < 300:
                logger.warning("vinted returned HTTP %s", status)
                return None, session
            try:
                body = resp.json()
            except ValueError:
                logger.warning("vinted returned non-JSON body")
                return None, session
            _note_vinted_query_success()
            return body, session

        multiplier = _register_rate_limit("vinted") if status == 429 else None
        if status == 403:
            _discard_vinted_session(session)
        if attempt == 0:
            delay = _retry_after_seconds(getattr(resp, "headers", None))
            delay = 2 if delay is None else min(delay, VINTED_BACKOFF_MAX_MINUTES * 60)
            logger.warning(
                "vinted transient HTTP %s block; retrying once after %ss%s",
                status, delay, f" and pacing {multiplier}x slower" if multiplier else "",
            )
            time.sleep(delay)
            if status == 403:
                session = _get_vinted_session()
                if session is None:
                    return None, None
            continue
        _note_vinted_query_failure(status)
        return None, session
    return None, session


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
    if not vinted_circuit_breaker_allows_calls():
        return [], None
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
        body, session = _get_vinted_catalog_page(
            session,
            {
                "search_text": query_text,
                "order": "newest_first",
                "per_page": 96,
                "page": page,
            },
        )
        if body is None:
            # Failed is not the same as an empty/short page.  Keep any page
            # already completed, but do not use the failure as pagination
            # evidence or report a genuine zero.
            return [x for x in listings if x], None
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            logger.warning("vinted catalog response missing a valid items collection; failed, not zero results")
            return [x for x in listings if x], None
        listings.extend(_vinted_item_to_listing(item) for item in items)
        if len(items) < 96:
            # A short page means there's nothing left - don't waste a call
            # confirming an empty page 2 exists.
            break
    return [x for x in listings if x], None


# ---------------------------------------------------------------------------
# OfferUp / Depop - HTML-page adapters via scrapling's Fetcher.
#
# OfferUp and Depop have no public JSON search API - their listing data is
# embedded in the rendered search page: OfferUp in a <script id="__NEXT_DATA__">
# JSON blob, Depop in Next.js App Router's RSC flight stream (repeated
# self.__next_f.push([1,"<escaped string>"]) calls carrying a dehydrated React
# Query cache). Both are fetched with scrapling's plain-HTTP Fetcher
# (TLS-fingerprint spoofing, no browser), then parsed here. Same listing
# shape, same never-raise contract as every other adapter.
# ---------------------------------------------------------------------------

_OFFERUP_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_FLIGHT_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[1,"((?:\\.|[^"\\])*)"\]\)', re.DOTALL
)


def _fetch_page(platform, url, timeout=15):
    """Fetch an HTML page via scrapling's TLS-spoofing Fetcher, or None."""
    try:
        from scrapling.fetchers import Fetcher
    except ImportError:
        logger.warning("%s skipped: scrapling is not installed", platform)
        return None
    _pace(platform)
    try:
        resp = Fetcher.get(url, timeout=timeout)
    except Exception as exc:
        logger.warning("%s request failed: %s", platform, exc)
        return None
    if resp.status == 429:
        multiplier = _register_rate_limit(platform)
        logger.warning(
            "%s rate limited (429) - backing off, next calls to this platform "
            "paced %sx slower for the rest of this run",
            platform, multiplier,
        )
        return None
    if resp.status != 200:
        logger.warning("%s returned HTTP %s", platform, resp.status)
        return None
    body = resp.body
    if isinstance(body, bytes):
        return body.decode(resp.encoding or "utf-8", errors="replace")
    return body


# --- OfferUp ----------------------------------------------------------------

def _offerup_listings(html):
    """OfferUp listing dicts from the page's __NEXT_DATA__ blob. [] on failure."""
    m = _OFFERUP_NEXT_DATA_RE.search(html)
    if not m:
        logger.warning("offerup: __NEXT_DATA__ tag not found")
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        logger.warning("offerup: __NEXT_DATA__ JSON parse failed")
        return []
    tiles = _dig(data, "props.pageProps.searchFeedResponse.looseTiles")
    if tiles is None:
        logger.warning("offerup: searchFeedResponse.looseTiles missing")
        return []
    return [
        tile["listing"] for tile in tiles
        if isinstance(tile, dict)
        and tile.get("__typename") == "ModularFeedTileListing"
        and isinstance(tile.get("listing"), dict)
    ]


def _offerup_to_listing(listing):
    image = listing.get("image")
    image_url = image.get("url") if isinstance(image, dict) else None
    return make_listing(
        "offerup",
        listing.get("listingId"),
        (listing.get("title") or "").strip(),
        listing.get("price"),
        f"https://offerup.com/item/detail/{listing.get('listingId')}",
        image_url=image_url,
    )


def _batch_request_timeout(deadline):
    """Return a request timeout that cannot extend past a batch deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(15, max(0.001, remaining))


def _run_html_batch(platform, saved_searches, build_url, extract_objects, normalize_object):
    """Run a sequential HTML batch behind a hard, snapshotting deadline.

    Scrapling normally obeys its request timeout, but the outer marketplace
    caller intentionally uses daemon threads because a socket can still wedge.
    Keep any potentially late work in a private dict and return a copy at the
    deadline.  If the inner daemon eventually wakes up, it can no longer mutate
    the object the caller already merged and treated as final.
    """
    deadline = time.monotonic() + MARKETPLACE_BATCH_DEADLINE_SECONDS
    working = {}
    working_lock = threading.Lock()

    def run():
        _marketplace_log_context.platform = platform
        try:
            for saved_search in saved_searches:
                timeout = _batch_request_timeout(deadline)
                if timeout is None:
                    return
                query = split_query_exclusions(saved_search["query"])[0]
                html = _fetch_page(platform, build_url(query), timeout=timeout)
                if html is None or time.monotonic() > deadline:
                    if time.monotonic() > deadline:
                        logger.warning(
                            "%s batch request exceeded the marketplace deadline; discarding its late result",
                            platform,
                        )
                        return
                    continue
                listings = [x for x in (normalize_object(obj) for obj in extract_objects(html)) if x]
                if listings:
                    with working_lock:
                        if time.monotonic() <= deadline:
                            working[saved_search["query"]] = listings
        except Exception:
            logger.exception("%s batch fetch failed", platform)
        finally:
            _marketplace_log_context.platform = None

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        logger.warning("%s batch hit the marketplace deadline; returning completed searches only", platform)
    with working_lock:
        return {query: list(listings) for query, listings in working.items()}


@batch_adapter("offerup")
def search_offerup(saved_searches):
    """Real bug caught in review before this ever shipped: this was built
    single-search (saved_search -> [listings]), but prefetch_marketplaces()'s
    batch_worker calls every BATCH_ADAPTERS entry with the FULL list of
    matching searches and expects {query: [listings]} back (see
    search_grailed_batch above, the pattern this must match) - confirmed
    live, calling this the way batch_worker actually does raised
    "TypeError: list indices must be integers or slices, not str" every
    time. Dormant only because no saved search currently lists "offerup" in
    its own platforms array (MARKETPLACES_ENABLED alone isn't enough to
    reach it - see batch_worker's `relevant` filter), so nothing had
    exercised the real call path yet. One HTTP call per query (no combined
    multi-query API like Grailed's Algolia endpoint exists for OfferUp)."""
    return _run_html_batch(
        "offerup",
        saved_searches,
        lambda query: "https://offerup.com/search?" + urlencode({"q": query}),
        _offerup_listings,
        _offerup_to_listing,
    )


# --- Depop ------------------------------------------------------------------

def _flight_payload_strings(html):
    """Decode each self.__next_f.push([1,"..."]) payload string, in order."""
    for m in _FLIGHT_PUSH_RE.finditer(html):
        try:
            yield json.loads('"' + m.group(1) + '"')
        except ValueError:
            continue


def _depop_rows(html):
    """Parse every flight row ("<id>:<json>", newline-joined) into {id: json}.

    The pushes are chunks of one stream, so they are concatenated before
    splitting - a single row can span multiple pushes, and the client itself
    joins them before decoding.
    """
    full = "".join(_flight_payload_strings(html))
    rows = {}
    for line in full.split("\n"):
        if not line or ":" not in line:
            continue
        rid, _, body = line.partition(":")
        try:
            rows[rid] = json.loads(body)
        except ValueError:
            continue
    return rows


def _product_search_entry(obj):
    """Recursively find the React Query cache entry named 'product_search'."""
    if isinstance(obj, dict):
        qk = obj.get("queryKey")
        if (isinstance(qk, list) and qk and qk[0] == "product_search") or (
            isinstance(qk, str) and "product_search" in qk
        ):
            return obj
        for value in obj.values():
            found = _product_search_entry(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _product_search_entry(item)
            if found is not None:
                return found
    return None


def _resolve_flight_ref(ref, rows):
    """Best-effort resolution of a React Flight reference ("$40:props:...").

    A component row is ["$", id, props, children]; the pointed-to payload
    lives in element [3] and reference paths start from it with a "props:"
    prefix. Only reached when a query's state.data is stored by reference
    instead of inline - the live page had it inline, this is belt-and-
    suspenders. ponytail: not a full Flight decoder; if Depop moves to deeply
    nested cross-row references this returns None and the adapter degrades to
    an empty result rather than crash.
    """
    if not isinstance(ref, str) or not ref.startswith("$"):
        return None
    rid, _, path = ref[1:].partition(":")
    row = rows.get(rid)
    if row is None:
        return None
    obj = row
    for seg in path.split(":"):
        if obj is None:
            return None
        if isinstance(obj, list):
            if seg == "props" and len(obj) >= 4:
                obj = obj[3]
            else:
                try:
                    obj = obj[int(seg)]
                except (ValueError, IndexError):
                    return None
        elif isinstance(obj, dict):
            if ":" in seg:
                key, _, idx = seg.partition(":")
                obj = obj.get(key)
                if not isinstance(obj, list):
                    return None
                try:
                    obj = obj[int(idx)]
                except (ValueError, IndexError):
                    return None
            else:
                obj = obj.get(seg)
        else:
            return None
    return obj


def _depop_objects(html):
    """Depop search-result listing dicts from the RSC flight stream. [] on failure."""
    rows = _depop_rows(html)
    if not rows:
        logger.warning("depop: no flight rows parsed from page HTML")
        return []
    objects = []
    found_entry = False
    for row in rows.values():
        entry = _product_search_entry(row)
        if entry is None:
            continue
        found_entry = True
        state = entry.get("state") or {}
        data = state.get("data")
        if isinstance(data, str):
            data = _resolve_flight_ref(data, rows)
        if not isinstance(data, dict):
            continue
        for page in data.get("pages") or []:
            page_data = page.get("data") if isinstance(page, dict) else None
            page_objects = page_data.get("objects") if isinstance(page_data, dict) else None
            if page_objects:
                objects.extend(o for o in page_objects if isinstance(o, dict))
    if not found_entry:
        logger.warning("depop: product_search query cache not found in flight stream")
    return objects


def _depop_title(description, max_length=90):
    """Derive stable title-like text from Depop's description-only payload."""
    if not isinstance(description, str):
        return None
    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
    # Reach hashtags belong in the searchable description, not in the title
    # used by relevance, exclusion, and fingerprint gates.
    title = re.sub(r"(?:^|\s)#[^\s#]+", " ", first_line)
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None
    if len(title) > max_length:
        title = title[: max_length - 3].rstrip() + "..."
    return title


def _depop_to_listing(obj):
    description = obj.get("description")
    pictures = obj.get("pictures") or []
    image_url = None
    if pictures and isinstance(pictures[0], dict):
        formats = pictures[0].get("formats")
        p0 = formats.get("P0") if isinstance(formats, dict) else None
        image_url = p0.get("url") if isinstance(p0, dict) else None
    pricing = obj.get("pricing")
    current = pricing.get("current_price") if isinstance(pricing, dict) else None
    price = current.get("total_price") if isinstance(current, dict) else None
    slug = obj.get("slug")
    item_id = obj.get("id")
    return make_listing(
        "depop",
        str(item_id) if item_id is not None else None,
        _depop_title(description),
        price,
        f"https://www.depop.com/products/{slug}/" if slug else None,
        image_url=image_url,
        description=description,
    )


@batch_adapter("depop")
def search_depop(saved_searches):
    """Same real contract fix as search_offerup above - was single-search,
    prefetch_marketplaces()'s batch_worker needs (list-in) -> {query:
    [listings]}. One HTTP call per query (no combined multi-query API)."""
    return _run_html_batch(
        "depop",
        saved_searches,
        lambda query: "https://www.depop.com/search/?" + urlencode({"q": query}),
        _depop_objects,
        _depop_to_listing,
    )
