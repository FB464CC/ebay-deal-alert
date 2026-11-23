"""Audit 11-23 wave 3: watch scam-ring price floods, fresh-account id tracking."""
import json
import sqlite3
from unittest import mock

import platforms as marketplaces
import vinted_fast as fast

DAY = 86400


def flood(conn, seller, price=120, search="vinted-hamilton-khaki", item=None, reviews=0, now=0):
    item = item or f"vinted:{seller}-{price}-{now}"
    return fast._watch_flood_reason(conn, search, price, item, seller, reviews, now=now)


def test_third_seller_at_one_brand_price_is_a_flood():
    conn = sqlite3.connect(":memory:")
    assert flood(conn, "a", now=0) is None
    # khaki and jazzmaster are one brand: the 11-21 ring spread across both
    assert flood(conn, "b", search="vinted-hamilton-jazzmaster", now=600) is None
    reason = flood(conn, "c", search="vinted-hamilton-intra-matic", now=1200)
    assert "3 sellers" in reason and "hamilton" in reason and "$120" in reason


def test_one_seller_clearing_a_closet_is_not_a_flood():
    # 11-21: one seller relisted a gold Seiko 8x at $88.88
    conn = sqlite3.connect(":memory:")
    for n in range(8):
        assert flood(conn, "dad", price=88.88, search="vinted-seiko-automatic", now=n) is None


def test_other_price_brand_or_day_does_not_count():
    conn = sqlite3.connect(":memory:")
    flood(conn, "a", now=0)
    flood(conn, "b", now=1)
    assert flood(conn, "c", price=119, now=2) is None
    assert flood(conn, "d", search="vinted-seiko-automatic", now=3) is None
    assert flood(conn, "e", now=DAY + 2) is None  # a and b aged out


def test_established_seller_passes_but_still_counts():
    conn = sqlite3.connect(":memory:")
    flood(conn, "a", now=0)
    flood(conn, "b", now=1)
    assert flood(conn, "shop", reviews=40, now=2) is None
    assert flood(conn, "d", reviews=None, now=3)  # page had no review count


def test_replay_11_21_hamilton_120_flood():
    # (minutes after 11-20 22:46, alerted?): all 33 $120 Hamilton rows in the
    # 30-day logs; sellers assumed distinct (each passed the closet check)
    rows = [(0, False), (48, False), (68, False), (95, False), (969, True), (1018, True),
            (1021, False), (1078, False), (1090, False), (1118, False), (1135, False),
            (1147, False), (1159, False), (1167, False), (1179, False), (1182, False),
            (1191, False), (1191, False), (1197, False), (1219, True), (1231, False),
            (1236, True), (1251, False), (1258, False), (1269, False), (1300, False),
            (1302, False), (1302, False), (1302, False), (1304, True), (1310, False),
            (1310, False), (2447, False)]
    conn = sqlite3.connect(":memory:")
    blocked = [alerted for n, (minute, alerted) in enumerate(rows)
               if flood(conn, f"s{n}", now=minute * 60)]
    assert blocked.count(True) == 5


def _lane(tmp_path):
    return fast.VintedFastLane(
        searches=[],
        state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl",
        main_seen_db_path=tmp_path / "missing-main.db",
        run_seconds=1,
    )


def test_process_item_blocks_flood_before_the_seller_request(tmp_path):
    lane = _lane(tmp_path)
    conn = fast.engine.init_db(tmp_path / "seen.db")
    search = {"id": "vinted-hamilton-khaki", "category": "watches", "query": "hamilton khaki"}
    for n in range(3):
        listing = marketplaces.make_listing(
            "vinted", f"9{n}", f"hamilton khaki field h7045515{n} automatic watch", 120,
            f"https://www.vinted.com/items/9{n}",
        )
        result = {"verdict": "REVIEW", "category": "watches", "listing": listing,
                  "item_price": 120.0, "search_id": search["id"]}
        with mock.patch.object(lane, "_cheap_result", return_value=(listing, result, None)), \
                mock.patch.object(fast, "fetch_vinted_seller_stats", side_effect=AssertionError), \
                mock.patch.object(fast, "_prewarm_listing_images", side_effect=AssertionError):
            try:
                lane._process_item(conn, None, {"user": {"id": 3_218_000_000 + n}}, search, None)
            except AssertionError:
                assert n < 2  # the first two go on to the seller check
    rows = [json.loads(line) for line in lane.log_path.read_text(encoding="utf-8").splitlines()]
    assert "scam-ring flood" in rows[-1]["reason"]
    conn.close()


def test_sellers_counted_once_and_absurd_ids_ignored(tmp_path):
    lane = _lane(tmp_path)
    rows = [{"user": {"id": 3_218_000_000 + n % 50}} for n in range(250)]
    lane._note_sellers(rows)
    assert lane._user_rows_seen == 50  # 5 polls of 50 rows, not 250 rows
    lane._note_sellers([{"user": {"id": 10**12}}, {"user": {"id": "3218549999"}}, {"user": None}, "x"])
    assert lane._newest_user_id == 3_218_549_999
    # an absurd first id can't poison an empty run either
    fresh = _lane(tmp_path)
    fresh._note_sellers([{"user": {"id": 99_999_999_999}}])
    assert fresh._newest_user_id == 0 and fresh._user_rows_seen == 0


def test_flood_row_is_committed_so_the_ai_ledger_is_not_locked(tmp_path):
    # Review B1: the open write held the lane file locked through the paid
    # AI reservation (BEGIN IMMEDIATE on the same file) -> NO_AI_BUDGET.
    conn = sqlite3.connect(tmp_path / "lane.db")
    for n, reviews in enumerate((0, 40, 0)):
        flood(conn, f"s{n}", reviews=reviews, now=n)
        assert not conn.in_transaction
    assert flood(conn, "s3", now=3)
    assert not conn.in_transaction
    conn.close()


def test_watches_under_50_are_never_a_flood():
    # Review S5: honest cheap Seikos repeat at round prices.
    conn = sqlite3.connect(":memory:")
    for n in range(4):
        assert flood(conn, f"s{n}", price=45, search="vinted-seiko-automatic", now=n) is None
    assert flood(conn, "s9", price=50, now=9) is None  # $50 counts, but only one row


def test_rows_the_cheap_checks_rejected_do_not_count(tmp_path):
    # Review S5: women's, homage and over-cap rows aren't the ring.
    lane = _lane(tmp_path)
    conn = fast.engine.init_db(tmp_path / "seen.db")
    search = {"id": "vinted-hamilton-khaki", "category": "watches", "query": "hamilton khaki"}
    for n in range(3):
        listing = marketplaces.make_listing(
            "vinted", f"8{n}", f"hamilton khaki field h7045515{n} automatic watch", 120,
            f"https://www.vinted.com/items/8{n}",
        )
        cheap = (listing, None, "women's watch") if n < 2 else (listing, {
            "verdict": "REVIEW", "category": "watches", "listing": listing,
            "item_price": 120.0, "search_id": search["id"]}, None)
        with mock.patch.object(lane, "_cheap_result", return_value=cheap), \
                mock.patch.object(fast, "fetch_vinted_seller_stats", side_effect=AssertionError), \
                mock.patch.object(fast, "_prewarm_listing_images"):
            try:
                lane._process_item(conn, None, {"user": {"id": 3_218_000_000 + n}}, search, None)
            except AssertionError:
                assert n == 2  # the third seller goes on to the seller check
                break
    else:
        raise AssertionError("third row never reached the seller check")
    conn.close()
