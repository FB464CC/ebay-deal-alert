"""Bounded, image-first Vinted fast lane.

The catalog cadence is deliberately a code constant. Saved-search criteria and
run/cost bounds remain configuration so they can be tuned without weakening the
four-second request floor.
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

import ebay_deal_alert as engine
import platforms as marketplaces


VINTED_FAST_POLL_SECONDS = 4
VINTED_FAST_PAGE_SIZE = 24
VINTED_FAST_BACKOFF_INITIAL_SECONDS = 60
VINTED_FAST_BACKOFF_MAX_SECONDS = 15 * 60
VINTED_FAST_STATE_PATH = Path(__file__).resolve().with_name(
    "vinted_fast_state.json"
)
VINTED_FAST_SEEN_DB_PATH = Path(__file__).resolve().with_name(
    "vinted_fast_seen.db"
)
VINTED_FAST_LOG_PATH = Path(__file__).resolve().with_name(
    "vinted_fast_log.jsonl"
)

VINTED_FAST_ENABLED = bool(engine._CONFIG.get("VINTED_FAST_ENABLED", True))
VINTED_FAST_RUN_MINUTES = float(
    engine._CONFIG.get("VINTED_FAST_RUN_MINUTES", 70)
)
VINTED_FAST_MAX_AI_CALLS_PER_RUN = max(
    0, int(engine._CONFIG.get("VINTED_FAST_MAX_AI_CALLS_PER_RUN", 40))
)

logger = logging.getLogger(__name__)


class VintedRequestBlocked(RuntimeError):
    def __init__(self, status):
        super().__init__(f"Vinted HTTP {status}")
        self.status = int(status)


def enabled_vinted_searches(searches=None):
    """Return every enabled Vinted-scoped search, intentionally unfocused."""
    source = engine.SAVED_SEARCHES if searches is None else searches
    return [
        search
        for search in source
        if isinstance(search, dict)
        and search.get("enabled", True)
        and "vinted" in (search.get("platforms") or [])
    ]


def _load_state(path):
    try:
        with Path(path).open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    searches = state.get("searches")
    if not isinstance(searches, dict):
        searches = {}
    return {"version": 1, "searches": searches}


def _save_state(path, state):
    path = Path(path)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _raw_vinted_item_id(item):
    return str(item.get("id") or "").strip() if isinstance(item, dict) else ""


def _numeric_id_key(item_id):
    try:
        return 1, int(item_id)
    except (TypeError, ValueError):
        return 0, str(item_id)


def _item_marker(item):
    item_id = _raw_vinted_item_id(item)
    if not item_id:
        return None
    return {
        "created_at": marketplaces._vinted_item_creation_date(item),
        "item_id": item_id,
    }


def _marker_key(marker):
    if not isinstance(marker, dict):
        return (-math.inf, (0, ""))
    raw_created = marker.get("created_at")
    try:
        created = datetime.fromisoformat(
            str(raw_created).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        created = -math.inf
    return created, _numeric_id_key(marker.get("item_id"))


def items_newer_than_high_water(items, high_water):
    """Return genuinely newer rows and the page's newest durable marker."""
    marked = [(item, _item_marker(item)) for item in items]
    marked = [(item, marker) for item, marker in marked if marker is not None]
    if not marked:
        return [], high_water
    newest_marker = max((marker for _item, marker in marked), key=_marker_key)
    if not high_water:
        # First observation establishes a baseline. Existing catalog inventory
        # is not "new" and must not create a deployment alert flood.
        return [], newest_marker
    water_key = _marker_key(high_water)
    newer = [
        item for item, marker in marked if _marker_key(marker) > water_key
    ]
    newer.sort(key=lambda item: _marker_key(_item_marker(item)))
    return newer, max((high_water, newest_marker), key=_marker_key)


def _response_status(response):
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _main_seen_connection(path, lane_path):
    target = Path(path)
    try:
        if not target.exists() or target.resolve() == Path(lane_path).resolve():
            return None
        return sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error):
        return None


def _already_seen(lane_conn, main_conn, item_id):
    if not engine.is_new(lane_conn, item_id):
        return True
    if main_conn is None:
        return False
    try:
        return main_conn.execute(
            "SELECT 1 FROM seen WHERE item_id = ?", (item_id,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _merge_ai_result(result, ai_result, category):
    """Merge the existing per-category vision response for the normal gate."""
    listing = result["listing"]
    if category == "poker-chips":
        result["poker_chips_ai_checked"] = True
        for source, target in (
            ("chip_type", "poker_chips_chip_type"),
            ("is_genuine_clay", "poker_chips_is_genuine_clay"),
            ("is_genuine_ceramic", "poker_chips_is_genuine_ceramic"),
            ("has_inlay_not_sticker", "poker_chips_has_inlay_not_sticker"),
            ("has_hot_stamp_not_sticker", "poker_chips_has_hot_stamp_not_sticker"),
            (
                "ceramic_design_embedded_not_sticker",
                "poker_chips_ceramic_design_embedded_not_sticker",
            ),
            ("edge_spots_consistent", "poker_chips_edge_spots_consistent"),
            ("recolor_suspected", "poker_chips_recolor_suspected"),
            ("recolor_reason", "poker_chips_recolor_reason"),
            (
                "identified_casino_or_maker",
                "poker_chips_identified_casino_or_maker",
            ),
            ("estimated_chip_count", "poker_chips_estimated_chip_count"),
            (
                "chip_count_visually_supported",
                "poker_chips_chip_count_visually_supported",
            ),
            ("is_complete_usable_set", "poker_chips_is_complete_usable_set"),
            (
                "set_completeness_reason",
                "poker_chips_set_completeness_reason",
            ),
            ("summary", "poker_chips_summary"),
        ):
            result[target] = ai_result.get(source)
        return None

    engine._merge_ai_counterfeit_assessment(result, ai_result)
    if category == "watches":
        result["watch_ai_checked"] = True
        result["watch_brand_mismatch"] = bool(ai_result.get("brand_mismatch"))
        result["watch_ai_seller_stated_value"] = engine._sane_ai_price(
            ai_result.get("seller_stated_value")
        )
        result["watch_seller_value_context"] = (
            ai_result.get("seller_value_context") or None
        )
        result["watch_seller_identity_claims"] = (
            ai_result.get("seller_identity_claims") or None
        )

    summary = str(ai_result.get("summary") or "")
    if engine.brand_in(summary.lower(), engine.GENDER_EXCLUDE_KEYWORDS):
        return f"AI photo check found gender-excluded keyword: {summary}"
    if engine.PET_PRODUCT_SIGNALS.search(summary.lower()):
        return f"AI photo check found pet product: {summary}"
    if (
        ai_result.get("damage_found") is True
        or ai_result.get("weird_logo_found") is True
    ):
        return "AI photo check found damage or unwanted logo: " + (
            summary or "manual review needed"
        )

    fabric = ai_result.get("fabric_from_tag")
    if fabric:
        result.setdefault("flags", []).append(f"AI fabric tag: {fabric}")
    liquidity = ai_result.get("liquidity")
    if liquidity in {"fast", "medium", "slow"}:
        result["liquidity"] = liquidity
    result["peter_millar_back_crown_visible"] = ai_result.get(
        "peter_millar_back_crown_visible"
    )
    result["damage_found"] = bool(ai_result.get("damage_found"))
    if ai_result.get("looks_good"):
        result.setdefault("flags", []).append(
            "AI photo check: " + (summary or "looks good")
        )

    result["estimated_retail_price"] = engine._sane_ai_price(
        ai_result.get("estimated_retail_price")
    )
    result["estimated_resale_value"] = engine._sane_ai_price(
        ai_result.get("estimated_resale_value")
    )
    result["price_confidence"] = ai_result.get("price_confidence")
    if category == "watches" and result["estimated_resale_value"] is not None:
        band = engine.watch_price_band(listing.get("title", ""))
        if band is not None:
            result["estimated_resale_value"] = engine.clamp_watch_resale_estimate(
                result["estimated_resale_value"], band
            )
    rating, discount = engine.compute_deal_rating(
        result.get("price"), result.get("estimated_resale_value")
    )
    if rating is not None:
        result["deal_rating"] = rating
        result["discount_pct"] = discount
    if category == "watches":
        engine.annotate_watch_description_value(result, listing)
    return None


class VintedFastLane:
    def __init__(
        self,
        *,
        searches=None,
        state_path=VINTED_FAST_STATE_PATH,
        seen_db_path=VINTED_FAST_SEEN_DB_PATH,
        log_path=VINTED_FAST_LOG_PATH,
        main_seen_db_path=engine.DB_PATH,
        dry_run=False,
        run_seconds=None,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        self.searches = enabled_vinted_searches(searches)
        self.state_path = Path(state_path)
        self.seen_db_path = Path(seen_db_path)
        self.log_path = Path(log_path)
        self.main_seen_db_path = main_seen_db_path
        self.dry_run = bool(dry_run)
        self.run_seconds = (
            float(run_seconds)
            if run_seconds is not None
            else VINTED_FAST_RUN_MINUTES * 60
        )
        self.clock = clock
        self.sleeper = sleeper
        self.session = None
        self.backoff_streak = 0
        self.ai_calls = 0
        self.samples = []
        self.stats = Counter()
        self.statuses = Counter()
        self._last_request_started = None
        self._summary_started = None

    def _log_result(self, result, *, delivered=False, delivery_error=None):
        result["vinted_fast_lane"] = True
        return engine.append_alert_log(
            result,
            delivered=delivered,
            delivery_error=delivery_error,
            log_path=self.log_path,
        )

    def _finish(
        self,
        conn,
        result,
        item_id,
        fingerprint,
        total_price,
        reason=None,
        verdict="REJECTED",
    ):
        if reason:
            result["verdict"] = verdict
            result["reason"] = reason
        self._log_result(result)
        engine.mark_seen(conn, item_id, fingerprint, total_price)

    def _respect_interval(self):
        if self._last_request_started is None:
            return
        remaining = (
            self._last_request_started
            + VINTED_FAST_POLL_SECONDS
            - self.clock()
        )
        if remaining > 0:
            self.sleeper(remaining)

    def _catalog_request(self, search):
        self._respect_interval()
        if self.session is None:
            self.session = marketplaces._get_vinted_session()
        if self.session is None:
            return 0, None
        positive_query = marketplaces.split_query_exclusions(
            search.get("query", "")
        )[0]
        params = {
            "search_text": positive_query,
            "order": "newest_first",
            "per_page": VINTED_FAST_PAGE_SIZE,
            "page": 1,
        }
        if search.get("max_price") is not None:
            params["price_to"] = search["max_price"]
            params["currency"] = "USD"
        self._last_request_started = self.clock()
        self.stats["requests"] += 1
        try:
            response = self.session.get(
                marketplaces.VINTED_CATALOG_URL,
                params=params,
                headers=marketplaces._vinted_catalog_headers(),
                timeout=marketplaces.HTTP_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("vinted-fast request failed for %s: %s", search.get("id"), exc)
            self.statuses["ERROR"] += 1
            return 0, None
        status = _response_status(response)
        self.statuses[str(status or "UNKNOWN")] += 1
        if status in {401, 403}:
            marketplaces._discard_vinted_session(self.session)
            self.session = None
        if status == 401:
            return status, None
        if status in {403, 429}:
            return status, None
        if not 200 <= status < 300:
            logger.warning("vinted-fast returned HTTP %s for %s", status, search.get("id"))
            return status, None
        try:
            body = response.json()
        except ValueError:
            logger.warning("vinted-fast returned non-JSON body for %s", search.get("id"))
            return status, None
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            logger.warning("vinted-fast response missing items for %s", search.get("id"))
            return status, None
        return status, items

    def _backoff(self, deadline):
        self.backoff_streak += 1
        delay = min(
            VINTED_FAST_BACKOFF_INITIAL_SECONDS
            * (2 ** (self.backoff_streak - 1)),
            VINTED_FAST_BACKOFF_MAX_SECONDS,
        )
        self.stats["backoffs"] += 1
        logger.warning(
            "vinted-fast HTTP block; backing off %ss (streak %s)",
            delay,
            self.backoff_streak,
        )
        remaining = max(0.0, deadline - self.clock())
        if remaining:
            self.sleeper(min(delay, remaining))
        return delay

    def _summary(self, *, force=False):
        now = self.clock()
        if self._summary_started is None:
            self._summary_started = now
        if force and not self.stats and not self.statuses:
            return
        if not force and now - self._summary_started < 60:
            return
        statuses = ",".join(
            f"{status}:{count}" for status, count in sorted(self.statuses.items())
        ) or "none"
        logger.info(
            "vinted-fast minute: requests=%s new_items=%s backoffs=%s statuses=%s",
            self.stats["requests"],
            self.stats["new_items"],
            self.stats["backoffs"],
            statuses,
        )
        self.stats.clear()
        self.statuses.clear()
        self._summary_started = now

    def _cheap_result(self, conn, main_conn, item, search):
        listing = marketplaces._vinted_item_to_listing(item)
        if not listing:
            return None
        item_id = listing["itemId"]
        if _already_seen(conn, main_conn, item_id):
            return None
        _positive, excluded = marketplaces.split_query_exclusions(
            search.get("query", "")
        )
        if marketplaces.title_matches_exclusion(listing.get("title"), excluded):
            return listing, None, "excluded query term in title"
        if not engine.is_relevant_marketplace_listing(
            listing, search.get("query", "")
        ):
            return listing, None, "marketplace title is not relevant to saved query"
        # The catalogue currently exposes only the cover photo. Reuse the
        # public-page description fetch to collect its gallery preloads too,
        # and apply the same four-second floor before this Vinted request.
        self._respect_interval()
        self._last_request_started = self.clock()
        self.stats["requests"] += 1
        details = engine.fetch_vinted_item_page_details(listing.get("itemWebUrl"))
        detail_status = details.get("_http_status")
        self.statuses[str(detail_status or "ERROR")] += 1
        if detail_status in {401, 403, 429}:
            raise VintedRequestBlocked(detail_status)
        description = details.get("description")
        if description:
            listing["description"] = description
        gallery_urls = details.get("image_urls") or []
        primary_url = (listing.get("image") or {}).get("imageUrl")
        image_urls = [primary_url] if primary_url else []
        image_urls.extend(url for url in gallery_urls if url not in image_urls)
        if image_urls:
            listing["image"] = {"imageUrl": image_urls[0]}
            listing["additionalImages"] = [
                {"imageUrl": image_url} for image_url in image_urls[1:8]
            ]
        if engine.has_excluded_single_e_shoe_width(search, listing):
            return listing, None, "configured shoe size has excluded E width"
        if not engine.listing_matches_size_filter(search, listing):
            return listing, None, "size mismatch for saved search"
        category = engine.classify_search_category(search)
        title = str(listing.get("title") or "")
        if category == "watches":
            if engine.WATCH_NOT_A_WATCH_SIGNALS.search(title):
                return listing, None, "watch title describes packaging/parts, not a timepiece"
            if engine.WATCH_AUTHENTICITY_RED_FLAGS.search(title):
                return listing, None, "watch title carries an authenticity red flag"
            if engine.WATCH_LOT_SIGNALS.search(title):
                return listing, None, "watch lot/bundle is not individually verifiable"
        item_price = float((listing.get("price") or {}).get("value") or 0)
        shipping = engine.get_shipping_cost(listing)
        total_price = item_price + shipping
        if total_price > float(search.get("max_price", math.inf)):
            return listing, None, (
                f"over max price: ${total_price:.2f} landed before tax > "
                f"${search.get('max_price')} cap"
            )
        result = engine.score_listing(
            listing,
            None,
            shipping_cost=shipping,
            category=category,
        )
        result.update(
            {
                "item_price": item_price,
                "shipping_cost": shipping,
                "shipping_cost_known": engine.is_shipping_cost_known(listing),
                "profile": search.get("profile", "slow"),
                "search_query": search.get("query"),
                "search_id": search.get("id"),
                "category": category,
                "category_id": search.get("category_id", "260012"),
                "vinted_fast_lane": True,
            }
        )
        return listing, result, None

    def _process_item(self, conn, main_conn, item, search, hard_stop):
        cheap = self._cheap_result(conn, main_conn, item, search)
        if cheap is None:
            return
        listing, result, cheap_reason = cheap
        item_id = listing["itemId"]
        item_price = float((listing.get("price") or {}).get("value") or 0)
        shipping = engine.get_shipping_cost(listing)
        total_price = item_price + shipping
        fingerprint = engine.listing_fingerprint(listing)
        if result is None:
            result = {
                "listing": listing,
                "item_price": item_price,
                "shipping_cost": shipping,
                "price": total_price * (1 + engine.SALES_TAX_RATE),
                "search_query": search.get("query"),
                "search_id": search.get("id"),
                "category": engine.classify_search_category(search),
                "verdict": "PASS",
                "vinted_fast_lane": True,
            }
            self._finish(
                conn, result, item_id, fingerprint, total_price, cheap_reason
            )
            return
        if result.get("verdict") != "REVIEW":
            self._finish(
                conn,
                result,
                item_id,
                fingerprint,
                total_price,
                result.get("reason") or "cheap scoring gate rejected listing",
            )
            return

        category = result["category"]
        jev_score = engine.jev_vinted_score(
            listing, category, hard_stop=hard_stop
        )
        if jev_score is not None:
            result["jev_vinted_score"] = jev_score
            if jev_score <= engine.JEV_VINTED_SKIP_THRESHOLD:
                self._finish(
                    conn,
                    result,
                    item_id,
                    fingerprint,
                    total_price,
                    "Jev pre-photo screen rejected listing "
                    f"({jev_score:.6f} <= {engine.JEV_VINTED_SKIP_THRESHOLD:.6f})",
                )
                return

        if self.ai_calls >= VINTED_FAST_MAX_AI_CALLS_PER_RUN:
            self._finish(
                conn,
                result,
                item_id,
                fingerprint,
                total_price,
                "no AI budget: Vinted fast-lane per-run AI cap reached",
                verdict="NO_AI_BUDGET",
            )
            return
        self.ai_calls += 1
        ai_result = engine.check_photos_with_gemini(
            listing,
            category=category,
            current_month_name=datetime.now(timezone.utc).strftime("%B"),
            hard_stop=hard_stop,
            search_query=search.get("query"),
        )
        if not isinstance(ai_result, dict):
            self._finish(
                conn,
                result,
                item_id,
                fingerprint,
                total_price,
                "no AI budget/provider result: every alert requires a real AI photo check",
                verdict="NO_AI_BUDGET",
            )
            return
        ai_reject = _merge_ai_result(result, ai_result, category)
        if ai_reject:
            self._finish(
                conn, result, item_id, fingerprint, total_price, ai_reject
            )
            return
        gate_reason = engine.is_blocked_by_steal_quality_gate(
            result, category=category
        )
        if gate_reason:
            self._finish(
                conn,
                result,
                item_id,
                fingerprint,
                total_price,
                f"blocked by steal-quality gate: {gate_reason}",
            )
            return
        sanity = engine._deepseek_alert_sanity_check(
            listing, ai_result, category, hard_stop=hard_stop,
            search_query=search.get("query"),
        )
        if sanity["is_part_or_accessory"] or not sanity["is_complete_item"]:
            self._finish(
                conn,
                result,
                item_id,
                fingerprint,
                total_price,
                "DeepSeek sanity check: "
                + (sanity.get("reason") or "part/accessory or incomplete item"),
            )
            return
        try:
            engine.send_alert(result)
        except Exception as exc:
            result["verdict"] = "DELIVERY_FAILED"
            result["reason"] = f"alert delivery failed after retries: {exc}"
            self._log_result(result, delivery_error=exc)
            engine.mark_seen(conn, item_id, fingerprint, total_price)
            return
        engine.mark_seen(conn, item_id, fingerprint, total_price)
        self._log_result(result, delivered=True)

    def run(self):
        if not VINTED_FAST_ENABLED:
            logger.info("vinted-fast disabled by VINTED_FAST_ENABLED=false")
            return {"requests": 0, "new_items": 0, "backoffs": 0}
        if not self.searches:
            logger.warning("vinted-fast has no enabled Vinted searches")
            return {"requests": 0, "new_items": 0, "backoffs": 0}
        state = _load_state(self.state_path)
        lane_conn = engine.init_db(self.seen_db_path)
        main_conn = _main_seen_connection(
            self.main_seen_db_path, self.seen_db_path
        )
        # Paid-provider accounting already mirrors into engine.DB_PATH. Point
        # that mirror at the cache-persisted lane DB for this process so the
        # unchanged monthly guard cannot reset between hourly workflow runs.
        original_engine_db_path = engine.DB_PATH
        engine.DB_PATH = str(self.seen_db_path)
        started = self.clock()
        deadline = started + max(0.0, self.run_seconds)
        self._summary_started = started
        totals = Counter()
        index = 0
        logger.info(
            "vinted-fast starting: searches=%s run_seconds=%.0f dry_run=%s",
            len(self.searches), self.run_seconds, self.dry_run,
        )
        try:
            while self.clock() < deadline:
                search = self.searches[index % len(self.searches)]
                index += 1
                status, items = self._catalog_request(search)
                totals["requests"] += 1
                if status in {403, 429}:
                    totals["backoffs"] += 1
                    self._backoff(deadline)
                    self._summary()
                    continue
                if status == 401 or items is None:
                    self._summary()
                    continue
                self.backoff_streak = 0
                for item in items:
                    if len(self.samples) >= 5:
                        break
                    title = str(item.get("title") or "").strip()
                    price = ((item.get("price") or {}).get("amount"))
                    if title:
                        self.samples.append((title, price))
                search_id = str(search.get("id") or search.get("query"))
                high_water = state["searches"].get(search_id)
                new_items, next_water = items_newer_than_high_water(
                    items, high_water
                )
                if new_items:
                    self.stats["new_items"] += len(new_items)
                    totals["new_items"] += len(new_items)
                pipeline_block_status = None
                if not self.dry_run:
                    for item in new_items:
                        try:
                            self._process_item(
                                lane_conn, main_conn, item, search, deadline
                            )
                        except VintedRequestBlocked as exc:
                            pipeline_block_status = exc.status
                            break
                if pipeline_block_status is not None:
                    if pipeline_block_status in {401, 403} and self.session is not None:
                        marketplaces._discard_vinted_session(self.session)
                        self.session = None
                    if pipeline_block_status in {403, 429}:
                        totals["backoffs"] += 1
                        self._backoff(deadline)
                    self._summary()
                    continue
                if next_water is not None:
                    state["searches"][search_id] = next_water
                    if not self.dry_run:
                        _save_state(self.state_path, state)
                self._summary()
        finally:
            self._summary(force=True)
            lane_conn.close()
            if main_conn is not None:
                main_conn.close()
            if self.session is not None:
                marketplaces._discard_vinted_session(self.session)
            engine.DB_PATH = original_engine_db_path
        for title, price in self.samples:
            logger.info("vinted-fast sample: $%s | %s", price, title)
        return dict(totals)


def _parse_args():
    parser = argparse.ArgumentParser(description="Run the bounded Vinted fast lane")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="poll and report samples without AI, alerts, seen writes, or state writes",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        help="test/smoke override for the configured bounded run duration",
    )
    parser.add_argument("--state-path", type=Path, default=VINTED_FAST_STATE_PATH)
    parser.add_argument("--seen-db", type=Path, default=VINTED_FAST_SEEN_DB_PATH)
    parser.add_argument("--log-path", type=Path, default=VINTED_FAST_LOG_PATH)
    return parser.parse_args()


def main():
    args = _parse_args()
    lane = VintedFastLane(
        state_path=args.state_path,
        seen_db_path=args.seen_db,
        log_path=args.log_path,
        dry_run=args.dry_run,
        run_seconds=args.run_seconds,
    )
    lane.run()


if __name__ == "__main__":
    main()
