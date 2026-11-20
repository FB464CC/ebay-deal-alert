"""Seven-day, official Browse API-only luxury menswear trial."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import logging
import math
from pathlib import Path
import re
import sqlite3
import time

import requests

import ebay_deal_alert as engine
import vinted_fast as fast


log = logging.getLogger(__name__)
CONFIG = engine._CONFIG
TRIAL_UNTIL = datetime.fromisoformat(
    CONFIG.get("EBAY_TOP5_TRIAL_UNTIL", "2026-11-25T23:59:00Z").replace("Z", "+00:00")
)
SEEN_DB = Path("ebay_top5_seen.db")
QUERY = "(loro piana,brunello cucinelli,kiton,brioni,zegna)"
HOUSES = ("loro piana", "brunello cucinelli", "kiton", "brioni", "zegna")
FAKE_FLOORS = CONFIG.get("EBAY_TOP5_FAKE_PRICE_FLOORS", {
    "loro piana": 25, "brunello cucinelli": 25, "kiton": 25,
    "brioni": 20, "zegna": 15,
})
# The lane's paid-AI ledger lives in its own cache, separate from Vinted's, so
# hold the trial to its own small cap (stricter than AI_PAID_MONTHLY_BUDGET_USD).
AI_BUDGET_USD = float(CONFIG.get("EBAY_TOP5_AI_BUDGET_USD", 2.0))
# Reasons a title alone can't settle: judge them again after getItem adds size and description.
SIZE_EVIDENCE = {"size missing or does not fit", "suit is not a 40-44 R/L (EU 50-54)",
                 "suit size only in the description"}
# ponytail: flat hourly getItem cap; searches use ~96 calls/h of eBay's daily Browse quota.
DETAILS_PER_HOUR = int(CONFIG.get("EBAY_TOP5_DETAILS_PER_HOUR", 60))
# Audit 11-20: "Loro Piana Cashmere Crewneck", "Zegna Half Zip" and "Kiton Shirt" died as
# "not a wanted garment". Plain shirts come in through fast._VINTED_WOVEN_SHIRT (no tees).
_KNIT = r"sweater|jumper|knit|cardigan|pullover|quarter[ -]?zip|1/4[ -]?zip|crew[ -]?neck|turtle[ -]?neck|half[ -]?zip|1/2[ -]?zip"
GARMENT = re.compile(rf"\b(?:{_KNIT}|polo|dress shirt|button[ -]?(?:down|up)|woven shirt|suit)\b", re.I)
# Review 11-20: "Brunello Cucinelli Crewneck T-Shirt", "Kiton Crewneck Sweatshirt" and "Tee Shirt"
# rode in on crewneck/shirt. Tees and sweatshirts aren't in the wardrobe.
_TEE = re.compile(r"\b(?:t[\s-]?shirts?|tees?|sweatshirts?|tanks?(?:\s+tops?)?)\b", re.I)
# Review 11-20: "Jos A Bank Ermenegildo Zegna Suit" is Zegna cloth, not the house. Unanchored
# "Ermenegildo Zegna" counts only after condition/colour/price words, never after another maker.
_ZEGNA_EBAY_TITLE = re.compile(
    r"^[\W_]*(?:(?:euc|nwt|nwot|nib|new|brand|gorgeous|stunning|beautiful|excellent|rare|vintage|vtg"
    r"|authentic|genuine|men[’']?s?|luxury|italian|navy|gr[ae]y|dark|light|charcoal|black|blue"
    r"|brown|beige|camel|olive|tan|cream|white|green|burgundy|red|pink|\$[\d,.]+|retail|msrp)[\W_]+)*"
    r"ermenegildo\s+zegna\b", re.I)
# Audit 11-20: "16 1/2", "16½", "Neck 16.5", "16.5 34/35" and cm "42" read as no collar, so a
# 16.5 Kiton shirt died as "collar 16.5 not confirmed". A half size on a shirt is a collar.
_HALF = r"(?:\.5|\.75|\s?1/2|½)"  # .75: cm 40 normalizes to 15.75 (review 11-20)
# Review 11-20: "Kiton Shirt Chest 44 cm" read as collar 17.5; skip cm values next to a body measurement.
_MEASURE = re.compile(r"chest|shoulder|pit|length|waist|sleeve|width|across|hem", re.I)
_COLLAR_TEXT = re.compile(
    rf"\b(?:neck|collar)(?:\s+size)?\s*:?\s*(1[4-8]{_HALF}?|3[7-9]|4[0-6])(?![\d.])"
    rf"|\b(1[4-8]{_HALF})(?![\d.])|\b(3[7-9]|4[0-6])\s?cm\b", re.I)
_COLLAR_ASPECT = re.compile(rf"\s*(1[4-8]{_HALF}?|3[7-9]|4[0-6])\s*(?:in(?:ches)?|cm|\"|\(\d{{2}}\))?\s*", re.I)
_COLLAR_CM = {37: 14.5, 38: 15, 39: 15.5, 40: 15.75, 41: 16, 42: 16.5, 43: 17, 44: 17.5, 45: 18, 46: 18.5}


def _inches(token):
    value = float(re.sub(r"\s?1/2|½", ".5", token))
    return _COLLAR_CM[int(value)] if value >= 37 else value


def _collar(listing):
    """Collar in inches, or None when none is stated (unknown, not a reject)."""
    collar = fast._collar_size(listing)
    if collar is not None:
        return collar
    for text in (str(listing.get("size") or ""), str(listing.get("title") or "")):
        for match in _COLLAR_TEXT.finditer(text):
            if match.group(3) and _MEASURE.search(text[max(0, match.start() - 15):match.end() + 15]):
                continue
            return _inches(next(g for g in match.groups() if g))
    return None


def searches():
    common = {"query": QUERY, "category_id": "1059", "max_price": 250}
    return common, {**common, "closing_soon_minutes": 30,
                    "contested_closing_soon_minutes": 30,
                    "search_window_minutes": 30, "sort": "endingSoonest"}


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS judged (item_id TEXT PRIMARY KEY, reason TEXT, alerted INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def _brand(title):
    text = str(title or "").casefold()
    if fast._VINTED_ZEGNA_DIFFUSION.search(text):
        return None
    if "zegna" in text:
        # Audit 11-20: eBay titles carry no Vinted brand-tag prefix, so
        # "EUC Ermenegildo Zegna Cashmere Sweater" is the house.
        mainline = fast._VINTED_ZEGNA_MAINLINE_TITLE.search(title) or _ZEGNA_EBAY_TITLE.search(title)
        return "zegna" if mainline else None
    # Audit 11-20: "Cucinelli Cashmere Sweater" never matched "brunello cucinelli".
    return next((house for house in HOUSES if house in text), None) or ("brunello cucinelli" if "cucinelli" in text else None)


def _category(title):
    if fast._VINTED_SUIT.search(title):
        return "tailoring"
    if re.search(rf"\b(?:{_KNIT})\b", title, re.I):
        return "knitwear"
    return "other"


def _seller_feedback(listing):
    return int(listing.get("seller_feedback_score")), float(listing.get("seller_feedback_percentage"))


def _seller_reason(listing):
    # Owner 11-20: "no-review sellers = flag, not block". Only real negative history
    # (20+ feedback under 95%) blocks; a 97.8% seller with 3,000 feedback used to die here.
    try:
        score, pct = _seller_feedback(listing)
    except (ValueError, TypeError):
        return "seller feedback missing"
    return "seller feedback below 95% (20+ ratings)" if score >= 20 and pct < 95 else None


def _seller_note(listing):
    """Alert-text flag for sellers let through: under 20 feedback or 95-98% positive."""
    try:
        score, pct = _seller_feedback(listing)
    except (ValueError, TypeError):
        return ""
    if score < 20:
        return f" | NEW eBay seller ({score} feedback, {pct:g}%): Money Back Guarantee covers not-as-described/fake"
    return f" | seller {pct:g}% positive ({score} feedback)" if pct < 98 else ""


def _quantity_reason(listing, auction):
    if auction:
        return None
    try:
        return "multiple available (replica shop pattern)" if int(listing.get("estimatedAvailabilities", [{}])[0].get("estimatedAvailableQuantity", 1)) > 1 else None
    except (ValueError, TypeError, IndexError, AttributeError):
        return "quantity unclear"


def max_bid(resale, shipping):
    """Whole-dollar ceiling whose taxed landed cost stays at or under 40%."""
    return max(0, math.floor((0.4 * resale) / (1 + engine.SALES_TAX_RATE) - shipping + 1e-9))


def _fetch_details(token, item_id):
    """Thin Browse getItem call; propagate 403/429 so the lane backs off."""
    response = requests.get(
        f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                 "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=US,zip=29201"},
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):  # malformed getItem: judge on the summary alone
        return {}
    description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(body.get("description") or "")))).strip()
    # Search summaries carry no size; item specifics ("Size", "Neck Size", ...) do.
    aspects = [(str(a.get("name") or "").casefold(), str(a.get("value") or "").strip())
               for a in (body.get("localizedAspects") or []) if isinstance(a, dict)]
    # A bare neck size ("16.5") reads as a collar only as "16.5 in", first in the string.
    # Audit 11-20: "16 1/2", "16½", "16.5 (42)" and cm "42" normalize to "16.5 in" too.
    neck = [f"{_inches(m.group(1)):g} in" if (m := _COLLAR_ASPECT.fullmatch(value)) else value
            for name, value in aspects if "neck" in name and value]
    other = [value for name, value in aspects if "size" in name and "neck" not in name and value]
    size = " ".join(neck + other).strip()
    return {**{key: body[key] for key in ("shippingOptions", "estimatedAvailabilities") if key in body},
            **({"description": description} if description else {}),
            **({"size": size} if size else {})}


class EbayTop5:
    def __init__(self, *, db_path=SEEN_DB, run_seconds=None, poll_seconds=None,
                 clock=time.monotonic, now=lambda: datetime.now(timezone.utc), sleeper=time.sleep):
        self.db_path = db_path
        self.run_seconds = float(run_seconds if run_seconds is not None else CONFIG.get("EBAY_TOP5_RUN_MINUTES", 340) * 60)
        self.poll_seconds = float(poll_seconds if poll_seconds is not None else CONFIG.get("EBAY_TOP5_POLL_SECONDS", 75))
        self.clock, self.now, self.sleeper = clock, now, sleeper
        self.vision = fast.VintedFastLane(searches=[], run_seconds=self.run_seconds)
        self.calls = Counter()
        self.reasons = Counter()

    def _record(self, conn, item_id, reason, alerted=False):
        self.reasons[reason] += 1
        conn.execute("INSERT OR IGNORE INTO judged VALUES (?, ?, ?)", (item_id, reason, int(alerted)))
        conn.commit()

    def _trial_over(self, conn):
        log.info("trial over")
        if conn.execute("SELECT 1 FROM state WHERE key='summary_sent'").fetchone():
            return
        alerts = conn.execute("SELECT count(*) FROM judged WHERE alerted=1").fetchone()[0]
        judged = conn.execute("SELECT count(*) FROM judged").fetchone()[0]
        reasons = conn.execute("SELECT reason, count(*) FROM judged WHERE alerted=0 GROUP BY reason ORDER BY count(*) DESC LIMIT 3").fetchall()
        message = (f"eBay top 5 trial complete: {alerts} alerts sent; {judged} candidates judged. "
                   + "Top rejections: " + (", ".join(f"{reason} ({count})" for reason, count in reasons) or "none"))
        if engine._send_ntfy_notice(message, "eBay top 5 trial summary"):
            conn.execute("INSERT OR IGNORE INTO state VALUES ('summary_sent', '1')")
            conn.commit()

    def _cheap_reason(self, listing, auction):
        title = str(listing.get("title") or "")
        brand = _brand(title)
        if not brand:
            return None, None, "house or mainline not confirmed"
        category = _category(title)
        if category == "tailoring" and re.search(r"\b(?:suit\s+(?:pants|trousers|vest|jacket)|jacket\s+only)\b", title, re.I):
            return brand, category, "suit separates are off; full suits only"
        reason = (fast._off_profile_reason(title)
                  or fast._menswear_shape_reason(listing, brand, category)
                  or fast._off_size_reason(listing, category)
                  or _quantity_reason(listing, auction)
                  or _seller_reason(listing))
        if reason:
            return brand, category, reason
        shirt = fast._VINTED_WOVEN_SHIRT.search(title) and not fast._VINTED_POLO.search(title)
        if _TEE.search(title) or not (GARMENT.search(title) or shirt):
            return brand, category, "not a wanted garment"
        # Audit 11-20: the R/L-only suit check here rejected "Kiton suit size 52"; the
        # shared _menswear_shape_reason above already applies Vinted's bare 40-44/50-54
        # check (fast._VINTED_SUIT_SIZE), so it is the only suit-size gate now.
        # Audit 11-20: an unstated collar is unknown, not a reject; a stated one must be 16.5.
        collar = _collar(listing) if shirt else None
        if collar is not None and collar != 16.5:
            return brand, category, f"collar {collar:g} won't fit (owner wears 16.5)"
        price = engine._sane_ai_price((listing.get("price") or {}).get("value"))
        if price is None or price < float(FAKE_FLOORS[brand]):
            return brand, category, "below per-brand fake-price floor"
        if fast._VINTED_POLO.search(title):
            search = {"size": ["M", "L"]}
        elif category == "tailoring":
            # Review 11-20: "50% wool" / "pants waist 40" in the description met the shape
            # check; the chest size must be in the title or the size field.
            if not fast._VINTED_SUIT_SIZE.search(f"{title} {listing.get('size') or ''}"):
                return brand, category, "suit size only in the description"
            search = {}  # size confirmed above
        elif re.search(r"quarter[ -]?zip|1/4[ -]?zip", title, re.I):
            search = {"size": ["M", "L", "XL"]}
        elif shirt and (collar == 16.5 or not listing.get("size")):
            search = {}  # the collar is the fit; no size at all is unknown (strong look reads the tag)
        else:
            search = {"size": ["M", "L", "XL", "16.5"]}
        if not engine.listing_matches_size_filter(search, listing):
            return brand, category, "size missing or does not fit"
        return brand, category, None

    def _judge(self, conn, token, listing, auction, deadline):
        item_id = str(listing.get("itemId") or "")
        if not item_id or conn.execute("SELECT 1 FROM judged WHERE item_id=?", (item_id,)).fetchone():
            return
        brand, category, reason = self._cheap_reason(listing, auction)
        if reason and reason not in SIZE_EVIDENCE:
            self._record(conn, item_id, reason)
            return
        if auction:
            remaining = float(listing.get("auction_minutes_remaining") or 999)
            if not 0 <= remaining <= 30:
                return
        if self.vision.ai_calls >= fast.VINTED_FAST_MAX_AI_CALLS_PER_RUN:
            return
        if self.calls["details"] >= DETAILS_PER_HOUR:
            return  # unrecorded: retried next hour
        self.calls["details"] += 1
        details = _fetch_details(token, item_id)
        if details:
            listing.update(details)
        brand, category, reason = self._cheap_reason(listing, auction)
        if reason:
            self._record(conn, item_id, reason)
            return
        item_price = float(listing["price"]["value"])
        shipping = engine.get_shipping_cost(listing)
        all_in = (item_price + shipping) * (1 + engine.SALES_TAX_RATE)
        result = {"listing": listing, "category": category, "item_price": item_price,
                  "shipping_cost": shipping, "price": all_in, "vinted_expected_paid": all_in,
                  # The listing's own house, not the 5-brand QUERY: "loro piana" in
                  # QUERY made the LP/Cucinelli bar reject every Kiton, Brioni and
                  # Zegna title (11-18 audit). LP/Cucinelli keep their Steal bar.
                  "search_query": brand,
                  # score_listing() never runs here, so the knitwear bar saw no tier and
                  # rejected every non-LP/Cucinelli knit as "not grab_on_sight-tier".
                  "brand_tier": "grab_on_sight" if brand in engine.GRAB_ON_SIGHT_BRANDS else None}
        fast._vinted_brand_and_floor(result)
        self.vision.ai_calls += 1
        ai = self.vision._first_photo_check(listing, category, deadline, QUERY)
        if not isinstance(ai, dict):
            # Recorded so a newly-listed item that stays in the feed is not
            # re-sent to the paid check every poll.
            self._record(conn, item_id, "AI photo check failed")
            return
        reason = (fast._merge_ai_result(result, ai, category)
                  or fast._off_profile_reason(ai.get("summary"), schools_only=True)
                  or fast._ai_lone_tailoring_reason(ai.get("summary"), category, listing, brand)
                  or fast._vinted_apparel_bar_reason(result, category)
                  or engine.is_blocked_by_steal_quality_gate(result, category=category))
        if reason:
            self._record(conn, item_id, str(reason))
            return
        strong = self.vision._run_vinted_strong_vision(result, deadline)
        if not isinstance(strong, dict):
            self._record(conn, item_id, "AI strong look failed")
            return
        reason = self.vision._strong_look_veto_reason(result)
        if reason:
            self._record(conn, item_id, str(reason))
            return
        reason = fast._steal_only_size_reason(result, {"size": ["L", "XL"], "steal_only_size": ["M"]}) if re.search(r"quarter[ -]?zip|1/4[ -]?zip", listing["title"], re.I) else None
        if reason:
            self._record(conn, item_id, reason)
            return
        resale = result["vinted_conservative_resale_value"]
        if all_in > 0.4 * resale:
            self._record(conn, item_id, "over 40% of conservative resale")
            return
        if self.now() >= TRIAL_UNTIL:
            return
        sanity = engine._deepseek_alert_sanity_check(
            listing, ai, category, hard_stop=deadline, search_query=QUERY,
        )
        if sanity["is_part_or_accessory"] or not sanity["is_complete_item"]:
            self._record(conn, item_id, "DeepSeek sanity check: incomplete item or accessory")
            return
        size = result.get("vinted_strong_size_on_tag") or listing.get("size") or "size unverified"
        note = _seller_note(listing)
        if auction:
            bid_ceiling = max_bid(resale, shipping)
            message = (f"STEAL: {listing['title']} | ends in {math.ceil(remaining)} min | "
                       f"current bid ${item_price:g} | {listing.get('bid_count', 0)} bids | "
                       f"Max bid ${bid_ceiling} | ${all_in:.2f} all-in | ~${resale:g} resale | "
                       f"size {size}{note} | {listing.get('itemWebUrl', '')}")
        else:
            message = (f"STEAL: {listing['title']} | ${item_price:g} price | ${all_in:.2f} all-in | "
                       f"~${resale:g} resale | size {size}{note} | {listing.get('itemWebUrl', '')}")
        # Claim before delivery: a worker crash after ntfy accepts the post
        # cannot produce a second alert in the next chained run.
        self._record(conn, item_id, "delivery attempted")
        if engine._send_ntfy_notice(message, "eBay top 5 STEAL", priority=5, tags=["fire"]):
            conn.execute("UPDATE judged SET reason='alerted', alerted=1 WHERE item_id=?", (item_id,))
            conn.commit()

    def run(self):
        conn = _db(self.db_path)
        original_db, original_cap = engine.DB_PATH, engine.AI_PAID_MONTHLY_BUDGET_USD
        engine.DB_PATH = str(self.db_path)  # Persist the shared paid-AI ledger in the lane cache.
        engine.AI_PAID_MONTHLY_BUDGET_USD = min(original_cap, AI_BUDGET_USD)
        try:
            if self.now() >= TRIAL_UNTIL:
                self._trial_over(conn)
                return
            deadline = self.clock() + self.run_seconds
            last_hour = self.clock()
            buy_search, auction_search = searches()
            while self.clock() < deadline:
                if self.now() >= TRIAL_UNTIL:
                    self._trial_over(conn)
                    break
                started = self.clock()
                # eBay app tokens last 2 h; runs last ~5.7 h. get_ebay_token()
                # serves its file cache until 5 min before expiry, so asking
                # every poll renews on time. A fixed 90-min refresh got the
                # same cached token back and ran blind (401) from 2 h on.
                try:
                    token = engine.get_ebay_token()
                    self.calls["search"] += 1
                    buys, _ = engine.search_ebay(token, buy_search)
                    self.calls["auction"] += 1
                    auctions, _ = engine.search_ebay_ending_soon_auctions(token, auction_search, now=self.now())
                    for listing, auction in [(item, True) for item in auctions] + [(item, False) for item in buys]:
                        if not auction and "FIXED_PRICE" not in (listing.get("buyingOptions") or []):
                            continue
                        # Fresh per listing: a slow poll can outlive a token served 5 min from expiry.
                        self._judge(conn, engine.get_ebay_token(), listing, auction, deadline)
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code in {403, 429}:
                        log.warning("eBay Browse HTTP %s; backing off", exc.response.status_code)
                        self.sleeper(min(900, max(0, deadline - self.clock())))
                    else:
                        log.warning("eBay Browse HTTP error: %s", exc)
                        if exc.response is not None and exc.response.status_code == 401:
                            # Dead before its stated expiry: drop the cache so the next poll re-auths.
                            engine.TOKEN_CACHE_PATH.unlink(missing_ok=True)
                except requests.RequestException as exc:
                    log.warning("eBay Browse request failed: %s", exc)
                except (KeyError, ValueError):
                    # A malformed token or Browse body must cost one poll, not the
                    # rest of a 5.7 h run; the traceback still lands in the log.
                    log.exception("eBay poll failed on a malformed response")
                if self.clock() - last_hour >= 3600:
                    log.info("Browse calls this hour: searches=%s auctions=%s details=%s",
                             self.calls["search"], self.calls["auction"], self.calls["details"])
                    log.info("Judged this hour: %s", dict(self.reasons.most_common()) or "none")
                    self.calls.clear()
                    self.reasons.clear()
                    last_hour = self.clock()
                self.sleeper(max(0, min(self.poll_seconds - (self.clock() - started), deadline - self.clock())))
        finally:
            engine.DB_PATH, engine.AI_PAID_MONTHLY_BUDGET_USD = original_db, original_cap
            conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-seconds", type=float)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    EbayTop5(run_seconds=args.run_seconds).run()


if __name__ == "__main__":
    main()
