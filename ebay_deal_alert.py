"""
CareerOS Deal Alert Engine — eBay module (Phase 1 starter)

Hand this to Claude Code along with BUILD_SPEC.md. This is a working skeleton,
not a finished tool: it needs eBay developer credentials, a real "seen items"
store, and tuning on the keyword lists before it's alert-ready.

Setup needed before this runs:
1. eBay developer account -> https://developer.ebay.com -> create an app ->
   get CLIENT_ID and CLIENT_SECRET (production keys, not sandbox).
2. Set env vars: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, NTFY_TOPIC
3. pip install requests
"""

import os
import json
import logging
import sqlite3
import sys
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG — saved searches ported from CareerOS project instructions
# ---------------------------------------------------------------------------

SAVED_SEARCHES = [
    {"query": "peter millar merino sweater", "size": ["L", "XL"], "max_price": 25},
    {"query": "peter millar quarter zip merino", "size": ["L", "XL"], "max_price": 25},
    {"query": "tom james merino", "size": None, "max_price": 30},
    {"query": "johnstons elgin cashmere", "size": None, "max_price": 40},
    {"query": "alan paine merino lambswool", "size": ["L"], "max_price": 25},
    {"query": "pringle scotland merino cashmere", "size": ["L"], "max_price": 25},
    {"query": "hickey freeman blazer", "size": ["40"], "max_price": 40},
    {"query": "brooks brothers gold label blazer", "size": ["40"], "max_price": 30},
    {"query": "canali blazer", "size": ["40"], "max_price": 50},
    {"query": "hart schaffner marx blazer", "size": ["40"], "max_price": 25},
    {"query": "allen edmonds", "size": ["13"], "max_price": 50},
    {"query": "alden shoes", "size": ["13"], "max_price": 80},
    {"query": "peter millar summer comfort polo", "size": ["L"], "max_price": 20},
]

# Brand tier lookup
GRAB_ON_SIGHT_BRANDS = [
    "tom james", "alan paine", "johnstons of elgin", "hickey freeman",
    "canali", "brioni", "isaia", "pringle of scotland", "hart schaffner marx",
]
STANDARD_BRANDS = ["peter millar", "brooks brothers", "ralph lauren", "allen edmonds", "alden"]
PASS_BRANDS = ["travismathew", "carnoustie"]

CORPORATE_LOGO_KEYWORDS = [
    "tournament", "championship", "club", "resort", "hotel", "bank", "capital",
    "financial", "wealth", "partners", "group", "associates", "invitational",
    "foundation", "corporate", "insurance", "open", "classic",
]

CONDITION_HARD_FAIL_KEYWORDS = ["moth", "moth hole", "moth holes"]
CONDITION_FLAG_KEYWORDS = ["hole", "stain", "pilling", "repair", "tear"]

FABRIC_GOOD_KEYWORDS = ["merino", "cashmere", "wool", "silk", "shell cordovan"]
FABRIC_POLY_KEYWORD = "poly"

PIT_TO_PIT_CAP_INCHES = 23.5

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "REPLACE_ME_careeros_deals")

DB_PATH = "seen_items.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EBAY AUTH + SEARCH
# ---------------------------------------------------------------------------

def get_ebay_token():
    """Client credentials OAuth flow — app-level token, no user login needed."""
    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_ebay(token, saved_search):
    """One call to the Browse API for a saved search config."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": saved_search["query"],
        "filter": "conditions:{USED|UNSPECIFIED}",  # pre-owned, adjust as needed
        "sort": "newlyListed",
        "limit": "50",
    }
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers=headers,
        params=params,
    )
    resp.raise_for_status()
    return resp.json().get("itemSummaries", [])


# ---------------------------------------------------------------------------
# SEEN-ITEM DEDUPE (SQLite — swap for a Wardrobe OS sheet tab if preferred)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (item_id TEXT PRIMARY KEY, seen_at TEXT)")
    conn.commit()
    return conn


def is_new(conn, item_id):
    cur = conn.execute("SELECT 1 FROM seen WHERE item_id = ?", (item_id,))
    return cur.fetchone() is None


def mark_seen(conn, item_id):
    conn.execute(
        "INSERT OR IGNORE INTO seen (item_id, seen_at) VALUES (?, ?)",
        (item_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# WARDROBE OS GAP CHECK
# ---------------------------------------------------------------------------

def fetch_gap_report():
    wardrobe_os_url = os.environ["WARDROBE_OS_URL"]
    wardrobe_os_secret = os.environ["WARDROBE_OS_SECRET"]
    resp = requests.get(
        wardrobe_os_url,
        params={"action": "gap_report", "secret": wardrobe_os_secret},
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# SIX-CHECK SCORING (text-based approximation — flags anything ambiguous
# for manual review rather than guessing)
# ---------------------------------------------------------------------------

def score_listing(listing, gap_report):
    title = listing.get("title", "").lower()
    price = float(listing.get("price", {}).get("value", 0))
    flags = []
    verdict = "REVIEW"  # default: don't auto-decide, surface it

    # 1. Brand
    brand_tier = None
    if any(b in title for b in PASS_BRANDS):
        return {"verdict": "PASS", "reason": "brand on pass list", "listing": listing}
    if any(b in title for b in GRAB_ON_SIGHT_BRANDS):
        brand_tier = "grab_on_sight"
    elif any(b in title for b in STANDARD_BRANDS):
        brand_tier = "standard"
    if any(kw in title for kw in CORPORATE_LOGO_KEYWORDS):
        return {"verdict": "PASS", "reason": "corporate logo keyword match", "listing": listing}
    if brand_tier is None:
        flags.append("brand not recognized — manual check needed")

    # 2. Fabric
    has_good_fabric = any(f in title for f in FABRIC_GOOD_KEYWORDS)
    has_poly = FABRIC_POLY_KEYWORD in title
    if has_poly and price > 15 and not has_good_fabric:
        return {"verdict": "PASS", "reason": "poly over $15, no premium fabric keyword", "listing": listing}
    if not has_good_fabric and not has_poly:
        flags.append("fabric not stated in title — check listing description/photos")

    # 3. Fit — can't reliably parse pit-to-pit from title alone
    flags.append("fit unconfirmed — pull listing description for pit-to-pit measurement")

    # 4. Condition
    if any(kw in title for kw in CONDITION_HARD_FAIL_KEYWORDS):
        return {"verdict": "PASS", "reason": "moth/hole keyword in title", "listing": listing}
    if any(kw in title for kw in CONDITION_FLAG_KEYWORDS):
        flags.append("condition keyword flagged — check description")

    # 5. Gap check (live)
    # NOTE: wire this to actual gap_report field names once confirmed against
    # a live pull — this is a placeholder structure.
    flags.append("gap check: cross-reference against live gap_report output")

    # 6. Flip potential — left for manual judgment, not automated here
    verdict = "REVIEW"

    return {
        "verdict": verdict,
        "brand_tier": brand_tier,
        "price": price,
        "flags": flags,
        "listing": listing,
    }


# ---------------------------------------------------------------------------
# ALERT DISPATCH
# ---------------------------------------------------------------------------

def send_alert(result):
    listing = result["listing"]
    title = listing.get("title", "")
    price = result.get("price")
    url = listing.get("itemWebUrl", "")
    flags = "; ".join(result.get("flags", []))

    message = f"${price} — {title}\n{url}\nFlags: {flags}"

    # ntfy.sh's public instance rate-limits by IP, and GitHub-hosted runners
    # share IPs with heavy traffic — 429s happen often enough in practice to
    # need a retry, not just a raise. 3 attempts, short backoff.
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": f"[{result['verdict']}] Deal alert"},
            )
            resp.raise_for_status()
            return
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s
    raise last_exc


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run():
    logger.info("Starting eBay deal alert run")
    conn = init_db()
    token = get_ebay_token()
    try:
        gap_report = fetch_gap_report()
        logger.info("Fetched Wardrobe OS gap report")
    except Exception:
        logger.exception("Failed to fetch Wardrobe OS gap report; proceeding without gap data")
        gap_report = None

    for saved_search in SAVED_SEARCHES:
        logger.info("Polling saved search: %s", saved_search["query"])
        try:
            listings = search_ebay(token, saved_search)
        except Exception:
            logger.exception("eBay search failed for query: %s", saved_search["query"])
            continue

        logger.info("Found %s listings for query: %s", len(listings), saved_search["query"])
        for listing in listings:
            item_id = listing.get("itemId")
            if not item_id:
                logger.info("Skipping listing without itemId: %s", listing.get("title", "untitled"))
                continue
            if not is_new(conn, item_id):
                continue
            mark_seen(conn, item_id)

            price = float(listing.get("price", {}).get("value", 999999))
            if price > saved_search["max_price"]:
                logger.info(
                    "Skipping %s over max price: $%s > $%s",
                    item_id,
                    price,
                    saved_search["max_price"],
                )
                continue

            result = score_listing(listing, gap_report)
            logger.info(
                "Scored %s as %s: %s",
                item_id,
                result["verdict"],
                result.get("reason") or "; ".join(result.get("flags", [])),
            )
            if result["verdict"] in ("REVIEW",):
                try:
                    send_alert(result)
                    logger.info("Sent alert for %s", item_id)
                except Exception:
                    logger.exception("Failed to send alert for %s", item_id)
            # PASS results are not sent — logged only if you add logging here

    logger.info("Finished eBay deal alert run")
    conn.close()


if __name__ == "__main__":
    run()
