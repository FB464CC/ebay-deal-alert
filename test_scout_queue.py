import json
import logging
import tempfile
import unittest
from pathlib import Path

import scout_queue


class ScoutQueueTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "scout_queue.jsonl"

    def test_normalizes_with_make_listing_and_skips_malformed_rows(self):
        rows = [
            "not json",
            json.dumps({"platform": "facebook", "itemId": "123", "title": "Titleist golf club set", "price": 125,
                        "itemWebUrl": "https://www.facebook.com/marketplace/item/123/", "imageUrl": "https://img/1.jpg",
                        "description": "Location: Columbia, SC", "scoutSearchQuery": "golf club set",
                        "scoutSearchLabel": "Golf sets"}),
            json.dumps({"platform": "facebook", "itemId": "no-price", "title": "Incomplete", "price": None,
                        "itemWebUrl": "https://example.test/item", "imageUrl": "", "description": ""}),
        ]
        self.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with self.assertLogs("scout_queue", logging.WARNING) as logs:
            listings = scout_queue.load_scout_queue(self.path)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["itemId"], "facebook:123")
        self.assertEqual(listings[0]["price"], {"value": 125.0, "currency": "USD"})
        self.assertEqual(listings[0]["image"], {"imageUrl": "https://img/1.jpg"})
        self.assertEqual(listings[0]["_scout_queue_key"], "facebook:123")
        self.assertEqual(listings[0]["_scout_search_query"], "golf club set")
        self.assertEqual(listings[0]["_scout_search_label"], "Golf sets")
        self.assertEqual(len(logs.records), 2)

    def test_missing_empty_and_clear_are_safe(self):
        self.assertEqual(scout_queue.load_scout_queue(self.path), [])
        self.assertFalse(scout_queue.scout_queue_has_data(self.path))
        self.assertTrue(scout_queue.clear_scout_queue(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_remove_processed_rewrites_only_acknowledged_valid_rows(self):
        processed = {"platform": "facebook", "itemId": "done", "title": "Done", "price": 10,
                     "itemWebUrl": "https://example.test/done", "imageUrl": "", "description": ""}
        deferred = {"platform": "facebook", "itemId": "later", "title": "Later", "price": 20,
                    "itemWebUrl": "https://example.test/later", "imageUrl": "", "description": ""}
        malformed = "{this is still not json"
        self.path.write_text(
            json.dumps(processed) + "\n" + malformed + "\n" + json.dumps(deferred) + "\n",
            encoding="utf-8",
        )

        self.assertTrue(scout_queue.remove_processed_scout_queue({"facebook:done"}, self.path))

        remaining = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(remaining, [malformed, json.dumps(deferred)])

    def test_remove_processed_normalizes_prefixed_item_ids(self):
        row = {"platform": "Facebook", "itemId": "Facebook:123", "title": "Done", "price": 10,
               "itemWebUrl": "https://example.test/123", "imageUrl": "", "description": ""}
        self.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertTrue(scout_queue.remove_processed_scout_queue({"facebook:123"}, self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
