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
                        "description": "Location: Columbia, SC"}),
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
        self.assertEqual(len(logs.records), 2)

    def test_missing_empty_and_clear_are_safe(self):
        self.assertEqual(scout_queue.load_scout_queue(self.path), [])
        self.assertFalse(scout_queue.scout_queue_has_data(self.path))
        self.assertTrue(scout_queue.clear_scout_queue(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
