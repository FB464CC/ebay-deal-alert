"""Timing regressions for the Vinted poll and verdict pipeline."""

import json
import threading
import time
from unittest import mock

import vinted_fast as fast


def test_scheduled_feed_keeps_polling_during_slow_item_check(tmp_path):
    search = {"id": "s", "query": "peter millar polo", "platforms": ["vinted"]}
    (tmp_path / "state.json").write_text(json.dumps({"searches": {"s": {"ids": ["0"]}}}))
    lane = fast.VintedFastLane(
        searches=[search], state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db", log_path=tmp_path / "lane.jsonl",
        main_seen_db_path=tmp_path / "missing-main.db", run_seconds=0.18,
    )
    row = {"id": "1", "title": "Peter Millar polo", "price": {"amount": "10"}}
    check_started = threading.Event()
    check_done = threading.Event()
    feed_during_check = threading.Event()

    def feed(_search):
        if check_started.is_set() and not check_done.is_set():
            feed_during_check.set()
        time.sleep(0.01)
        return 200, [row]

    def slow_check(*_args):
        check_started.set()
        try:
            time.sleep(0.10)
        finally:
            check_done.set()

    with (
        mock.patch.object(lane, "_catalog_request", side_effect=feed),
        mock.patch.object(lane, "_instant_prepass"),
        mock.patch.object(lane, "_peek_brand_feed"),
        mock.patch.object(lane, "_process_item", side_effect=slow_check),
        mock.patch.object(lane, "_health_ping"),
    ):
        lane.run()

    assert check_done.is_set()
    assert feed_during_check.is_set(), "scheduled feeds stopped while an item check ran"


def test_poll_and_item_share_the_request_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(fast, "VINTED_FAST_POLL_SECONDS", 0.04)
    lane = fast.VintedFastLane(
        searches=[], state_path=tmp_path / "state.json",
        seen_db_path=tmp_path / "seen.db", log_path=tmp_path / "lane.jsonl",
        schedule=[],
    )
    barrier = threading.Barrier(3)
    starts = []

    def reserve():
        barrier.wait()
        lane._reserve_request()
        starts.append(lane.clock())

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(starts) == 2
    assert max(starts) - min(starts) >= 0.03
