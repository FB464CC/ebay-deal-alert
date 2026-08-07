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
import base64
import json
import logging
import mimetypes
import re
import sqlite3
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG — saved searches ported from CareerOS project instructions
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


_CONFIG = load_config()
SAVED_SEARCHES = _CONFIG["SAVED_SEARCHES"]
GRAB_ON_SIGHT_BRANDS = _CONFIG["GRAB_ON_SIGHT_BRANDS"]
STANDARD_BRANDS = _CONFIG["STANDARD_BRANDS"]
PASS_BRANDS = _CONFIG["PASS_BRANDS"]
CORPORATE_LOGO_KEYWORDS = _CONFIG["CORPORATE_LOGO_KEYWORDS"]
CONDITION_HARD_FAIL_KEYWORDS = _CONFIG["CONDITION_HARD_FAIL_KEYWORDS"]
CONDITION_FLAG_KEYWORDS = _CONFIG["CONDITION_FLAG_KEYWORDS"]
FABRIC_GOOD_KEYWORDS = _CONFIG["FABRIC_GOOD_KEYWORDS"]
FABRIC_POLY_KEYWORD = _CONFIG["FABRIC_POLY_KEYWORD"]
PIT_TO_PIT_CAP_INCHES = _CONFIG["PIT_TO_PIT_CAP_INCHES"]

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "REPLACE_ME_careeros_deals")

DB_PATH = "seen_items.db"
TOKEN_CACHE_PATH = Path(__file__).resolve().with_name("ebay_token_cache.json")
ALERTS_LOG_PATH = Path(__file__).resolve().with_name("alerts_log.jsonl")
GEMINI_CALL_LIMIT = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EBAY AUTH + SEARCH
# ---------------------------------------------------------------------------

def _get_ebay_token_uncached():
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


def _read_cached_ebay_token():
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as cache_file:
            cached = json.load(cache_file)
        if cached.get("access_token") and float(cached.get("expires_at", 0)) > time.time():
            logger.info("Using cached eBay OAuth token")
            return cached["access_token"]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring invalid eBay token cache: %s", exc)
    return None


def _write_cached_ebay_token(access_token, expires_in):
    expires_at = time.time() + int(expires_in) - 300
    with TOKEN_CACHE_PATH.open("w", encoding="utf-8") as cache_file:
        json.dump({"access_token": access_token, "expires_at": expires_at}, cache_file)
        cache_file.write("\n")


def get_ebay_token():
    """Client credentials OAuth flow for an app-level token."""
    cached_token = _read_cached_ebay_token()
    if cached_token:
        return cached_token

    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    last_exc = None
    for attempt in range(3):
        try:
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
            token_body = resp.json()
            access_token = token_body["access_token"]
            _write_cached_ebay_token(access_token, token_body.get("expires_in", 7200))
            return access_token
        except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise last_exc


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
    price_value = (listing.get("price") or {}).get("value", 0)
    price = float(0 if price_value is None else price_value)
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
# GEMINI PHOTO CHECK
# ---------------------------------------------------------------------------

def _collect_listing_image_urls(listing):
    urls = []
    primary_url = (listing.get("image") or {}).get("imageUrl")
    if primary_url:
        urls.append(primary_url)
    for image in listing.get("additionalImages", []):
        image_url = image.get("imageUrl")
        if image_url:
            urls.append(image_url)
    return urls[:4]


def _detect_image_mime_type(resp, image_url):
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type.startswith("image/"):
        return content_type
    guessed_type, _ = mimetypes.guess_type(image_url)
    if guessed_type and guessed_type.startswith("image/"):
        return guessed_type
    return "image/jpeg"


def _strip_json_code_fence(text):
    cleaned = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return cleaned


def check_photos_with_gemini(listing):
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        logger.warning("Skipping Gemini photo check: GEMINI_API_KEY is not configured")
        return None
    # Use Google's rolling "-latest" alias instead of a pinned model name -
    # gemini-2.0-flash and gemini-2.5-flash/-flash-lite all 404 for this key
    # ("no longer available to new users"), confirmed live against the
    # actual API. The -latest alias always resolves to Google's current
    # lightweight flash-tier model, which also sidesteps this whole class
    # of bug going forward (no more silent breakage on model retirement).
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

    image_parts = []
    for image_url in _collect_listing_image_urls(listing):
        try:
            image_resp = requests.get(image_url, timeout=10)
            image_resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning("Skipping failed image download for Gemini check: %s", exc)
            continue

        image_parts.append({
            "inline_data": {
                "mime_type": _detect_image_mime_type(image_resp, image_url),
                "data": base64.b64encode(image_resp.content).decode("ascii"),
            }
        })

    if not image_parts:
        logger.warning("Skipping Gemini photo check: no listing images could be downloaded")
        return None

    prompt = (
        "Inspect these secondhand clothing or footwear listing photos for a menswear "
        "flipping business. Report strict JSON only, with no markdown fences, using "
        "this exact shape: {\"damage_found\": bool, \"damage_desc\": string, "
        "\"weird_logo_found\": bool, \"logo_desc\": string, \"looks_good\": bool, "
        "\"summary\": string}. damage_found means visible holes, stains, moth "
        "damage, heavy pilling, tears, or other undisclosed damage beyond normal "
        "light wear. weird_logo_found means prominent corporate, tournament, "
        "country-club, bank, or resort branding/embroidery visible in the photos. "
        "looks_good should be true only when no damage or unwanted logo is visible."
    )
    payload = {
        "contents": [{
            "parts": [{"text": prompt}] + image_parts,
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gemini_model}:generateContent?key={gemini_api_key}"
    )

    try:
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts)
        return json.loads(_strip_json_code_fence(text))
    except (requests.exceptions.RequestException, KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.warning("Gemini photo check failed; proceeding without AI result: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ALERT DISPATCH
# ---------------------------------------------------------------------------

def notify_bot_down(message):
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": "[ALERT-BOT DOWN]"},
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        logger.exception("Failed to send bot-down notification")


def append_alert_log(result):
    listing = result["listing"]
    price = result.get("price")
    if price is None:
        price_value = (listing.get("price") or {}).get("value", 0)
        price = float(0 if price_value is None else price_value)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "item_id": listing.get("itemId"),
        "title": listing.get("title", ""),
        "price": price,
        "verdict": result.get("verdict"),
        "reason": result.get("reason") or "; ".join(result.get("flags", [])),
    }
    if result.get("ai_flagged"):
        record["ai_flagged"] = True

    lines = []
    if ALERTS_LOG_PATH.exists():
        try:
            with ALERTS_LOG_PATH.open("r", encoding="utf-8") as log_file:
                lines = [line.rstrip("\n") for line in log_file if line.strip()]
        except OSError as exc:
            logger.warning("Failed to read alerts log; rewriting with current record: %s", exc)
            lines = []

    lines.append(json.dumps(record, separators=(",", ":")))
    lines = lines[-200:]
    with ALERTS_LOG_PATH.open("w", encoding="utf-8") as log_file:
        for line in lines:
            log_file.write(line + "\n")


def send_alert(result):
    listing = result["listing"]
    title = listing.get("title", "")
    price = result.get("price")
    url = listing.get("itemWebUrl", "")
    flags = "; ".join(result.get("flags", []))
    image_url = (listing.get("image") or {}).get("imageUrl")

    message = f"${price} - {title}\nFlags: {flags}"
    alert_title = f"[{result['verdict']}] Deal alert"
    if result.get("ai_flagged"):
        alert_title = f"[AI-FLAGGED] {alert_title}"

    tags = []
    if result.get("ai_flagged"):
        tags.append("warning")
    elif result.get("brand_tier") == "grab_on_sight":
        tags.append("moneybag")
    else:
        tags.append("eyes")

    headers = {
        "Title": alert_title,
        "Click": url,
        "Tags": ",".join(tags),
    }
    if image_url:
        headers["Attach"] = image_url
    if result.get("brand_tier") == "grab_on_sight":
        headers["Priority"] = "5"

    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers=headers,
            )
            resp.raise_for_status()
            return
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise last_exc


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run():
    logger.info("Starting eBay deal alert run")
    conn = init_db()
    try:
        token = get_ebay_token()
    except Exception as exc:
        logger.exception("Failed to get eBay OAuth token")
        notify_bot_down(f"eBay deal alert could not get an OAuth token: {exc}")
        conn.close()
        return

    try:
        gap_report = fetch_gap_report()
        logger.info("Fetched Wardrobe OS gap report")
    except Exception:
        logger.exception("Failed to fetch Wardrobe OS gap report; proceeding without gap data")
        gap_report = None

    gemini_calls = 0
    gemini_budget_logged = False

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

            size_tokens = saved_search.get("size")
            title = listing.get("title", "")
            if size_tokens and not any(
                re.search(rf"\b{re.escape(size_token)}\b", title, re.IGNORECASE)
                for size_token in size_tokens
            ):
                logger.info("Skipping %s because title does not match size filter", item_id)
                continue

            price_value = (listing.get("price") or {}).get("value", 999999)
            price = float(999999 if price_value is None else price_value)
            if price > saved_search["max_price"]:
                logger.info(
                    "Skipping %s over max price: $%s > $%s",
                    item_id,
                    price,
                    saved_search["max_price"],
                )
                mark_seen(conn, item_id)
                continue

            result = score_listing(listing, gap_report)
            logger.info(
                "Scored %s as %s: %s",
                item_id,
                result["verdict"],
                result.get("reason") or "; ".join(result.get("flags", [])),
            )
            if result["verdict"] == "PASS":
                append_alert_log(result)
                mark_seen(conn, item_id)
                continue

            if result["verdict"] in ("REVIEW",):
                ai_result = None
                if gemini_calls < GEMINI_CALL_LIMIT:
                    if gemini_calls > 0:
                        time.sleep(5)
                    gemini_calls += 1
                    ai_result = check_photos_with_gemini(listing)
                elif not gemini_budget_logged:
                    logger.info(
                        "Gemini call budget exhausted for this run, skipping AI check for remaining listings"
                    )
                    gemini_budget_logged = True

                if ai_result is not None and (
                    ai_result.get("damage_found") is True
                    or ai_result.get("weird_logo_found") is True
                ):
                    result["ai_flagged"] = True
                    result.setdefault("flags", []).append(
                        "AI photo check flagged damage or unwanted logo: "
                        + ai_result.get("summary", "manual review needed")
                    )
                    logger.info(
                        "Flagging %s for AI photo review: %s",
                        item_id,
                        ai_result.get("summary", "no summary provided"),
                    )
                if ai_result is not None and ai_result.get("looks_good"):
                    result.setdefault("flags", []).append(
                        "AI photo check: " + ai_result.get("summary", "looks good")
                    )

                append_alert_log(result)
                try:
                    send_alert(result)
                    logger.info("Sent alert for %s", item_id)
                    mark_seen(conn, item_id)
                except Exception:
                    logger.exception("Failed to send alert for %s", item_id)
            # PASS results are not sent — logged only if you add logging here

    logger.info("Finished eBay deal alert run")
    conn.close()


if __name__ == "__main__":
    run()
