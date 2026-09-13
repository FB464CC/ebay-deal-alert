"""Regression tests for the DeepSeek vision-provider switch.

The photo check was Gemini-only until DeepSeek shipped deepseek-v4-flash-
vision-exp (2026-08-21). These tests lock in the three things that switch
changed, each of which is a silent-failure trap on a 5-minute cron:

  1. Provider routing: DeepSeek is primary, Gemini is the automatic
     fallback, so a DeepSeek outage degrades to Gemini -- never to a blind
     trust (the "every alert must be AI-vetted" invariant).
  2. JSON-mode payload shape for DeepSeek's OpenAI-compatible route.
  3. eBay image-URL upscaling: the Browse API returns s-l225 thumbnails,
     which DeepSeek's ~800px internal resize makes too small to read a
     brand tag; the larger size restores that.

Pure stdlib unittest, mirroring test_ebay_deal_alert.py's conventions. Run:
    python -m unittest test_photo_provider -v
"""
import base64
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest import mock

import requests

import ebay_deal_alert as m


class UpscaleEbayImageUrl(unittest.TestCase):
    def test_small_thumbnail_upscaled(self):
        self.assertEqual(
            m._upscale_ebay_image_url("https://i.ebayimg.com/images/g/ABC/s-l225.jpg"),
            "https://i.ebayimg.com/images/g/ABC/s-l1600.jpg",
        )

    def test_already_large_unchanged(self):
        url = "https://i.ebayimg.com/images/g/ABC/s-l1600.jpg"
        self.assertEqual(m._upscale_ebay_image_url(url), url)

    def test_non_ebay_url_unchanged(self):
        url = "https://example.com/photo.jpg"
        self.assertEqual(m._upscale_ebay_image_url(url), url)


class MakeDeepseekImageBlock(unittest.TestCase):
    def test_base64_data_url(self):
        block = m._make_deepseek_image_block(b"\xff\xd8", "image/jpeg")
        self.assertEqual(block["type"], "image_url")
        self.assertEqual(block["image_url"]["url"], "data:image/jpeg;base64,/9g=")


class CallDeepseekJson(unittest.TestCase):
    def setUp(self):
        self.spend_patch = mock.patch.object(m, "_reserve_paid_ai_spend", return_value=True)
        self.spend_patch.start()

    def tearDown(self):
        self.spend_patch.stop()

    def test_no_key_returns_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(m._call_deepseek_json("prompt", []))

    def test_happy_path_parses_json(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {"choices": [{"message": {"content": '{"a": 1}'}}]}
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}):
            with mock.patch("requests.post", return_value=fake_resp) as post:
                result = m._call_deepseek_json("prompt JSON", [(b"x", "image/jpeg")])
        self.assertEqual(result, {"a": 1})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], m.DEEPSEEK_MODEL)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], m.DEEPSEEK_VISION_MAX_OUTPUT_TOKENS)
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")

    def test_successful_usage_settles_peak_rate_cost(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": '{"a": 1}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        }
        with mock.patch.object(m, "_reserve_paid_ai_spend", return_value=True), \
             mock.patch.object(m, "_settle_paid_ai_spend") as settle, \
             mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}), \
             mock.patch("requests.post", return_value=fake_resp):
            result = m._call_deepseek_json("prompt JSON", [])
        self.assertEqual(result, {"a": 1})
        settle.assert_called_once_with(m.AI_PAID_VISION_RESERVATION_USD, 0.00042)

    def test_code_fence_stripped(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {"choices": [{"message": {"content": '```json\n{"b":2}\n```'}}]}
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}):
            with mock.patch("requests.post", return_value=fake_resp):
                self.assertEqual(m._call_deepseek_json("prompt JSON", []), {"b": 2})


class PaidAiSpendGuard(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(m, "AI_SPEND_DB_PATH", pathlib.Path(self.tmpdir.name) / "spend.db")
        self.seen_db_patch = mock.patch.object(m, "DB_PATH", pathlib.Path(self.tmpdir.name) / "seen.db")
        self.budget_patch = mock.patch.object(m, "AI_PAID_MONTHLY_BUDGET_USD", 0.01)
        self.db_patch.start()
        self.seen_db_patch.start()
        self.budget_patch.start()

    def tearDown(self):
        self.budget_patch.stop()
        self.seen_db_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_reservations_stop_at_monthly_cap(self):
        self.assertTrue(m._reserve_paid_ai_spend(0.005))
        self.assertTrue(m._reserve_paid_ai_spend(0.005))
        self.assertFalse(m._reserve_paid_ai_spend(0.001))

    def test_paid_provider_is_not_called_when_cap_is_full(self):
        self.assertTrue(m._reserve_paid_ai_spend(0.01))
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}), \
             mock.patch("requests.post") as post:
            self.assertIsNone(m._call_deepseek_json("prompt JSON", []))
        post.assert_not_called()

    def test_successful_call_settles_below_the_conservative_reservation(self):
        self.assertTrue(m._reserve_paid_ai_spend(0.005, kind="vision"))
        self.assertTrue(m._settle_paid_ai_spend(0.005, 0.001234))
        with closing(sqlite3.connect(m.AI_SPEND_DB_PATH)) as ledger:
            aggregate = ledger.execute(
                "SELECT reserved_usd, calls FROM ai_paid_spend"
            ).fetchone()
            accounting = ledger.execute(
                "SELECT gross_reserved_usd, settled_credit_usd, vision_calls, "
                "settlements FROM ai_paid_accounting"
            ).fetchone()
        self.assertAlmostEqual(aggregate[0], 0.001234)
        self.assertEqual(aggregate[1], 1)
        self.assertAlmostEqual(accounting[0], 0.005)
        self.assertAlmostEqual(accounting[1], 0.003766)
        self.assertEqual(accounting[2:], (1, 1))

    def test_cache_mirror_restores_a_rolled_back_primary_journal(self):
        self.assertTrue(m._reserve_paid_ai_spend(0.005, kind="vision"))
        with closing(sqlite3.connect(m.AI_SPEND_DB_PATH)) as ledger:
            ledger.execute("DELETE FROM ai_paid_accounting")
            ledger.execute("UPDATE ai_paid_spend SET reserved_usd = 0, calls = 0")
            ledger.commit()
        snapshot = m._paid_ai_budget_snapshot(required_usd=0)
        self.assertAlmostEqual(snapshot["reserved_usd"], 0.005)
        self.assertEqual(snapshot["calls"], 1)
        with closing(sqlite3.connect(m.AI_SPEND_DB_PATH)) as ledger:
            restored = ledger.execute(
                "SELECT gross_reserved_usd, calls FROM ai_paid_accounting"
            ).fetchone()
        self.assertEqual(restored, (0.005, 1))


class PaidAiBudgetExhaustionNotification(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            m, "AI_SPEND_DB_PATH", pathlib.Path(self.tmpdir.name) / "spend.db"
        )
        self.seen_patch = mock.patch.object(
            m, "DB_PATH", pathlib.Path(self.tmpdir.name) / "seen.db"
        )
        self.db_patch.start()
        self.seen_patch.start()

    def tearDown(self):
        self.seen_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def _snapshot(self):
        return {
            "month": "2026-09",
            "reserved_usd": 10.0,
            "calls": 2232,
            "cap_usd": 10.0,
            "required_usd": 0.01,
            "exhausted": True,
            "reset_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "event_key": "2026-09:10.000000",
        }

    def test_notifies_once_across_both_persistent_ledgers(self):
        with mock.patch.object(m, "notify_bot_down", return_value=True) as notify:
            self.assertTrue(m._notify_paid_ai_budget_exhausted_once(self._snapshot()))
            self.assertFalse(m._notify_paid_ai_budget_exhausted_once(self._snapshot()))
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["title"], "[AI BUDGET EXHAUSTED]")
        self.assertIn("system-wide monthly outage", notify.call_args.args[0])

    def test_failed_delivery_releases_claim_for_next_run(self):
        with mock.patch.object(m, "notify_bot_down", side_effect=[False, True]) as notify:
            self.assertFalse(m._notify_paid_ai_budget_exhausted_once(self._snapshot()))
            self.assertTrue(m._notify_paid_ai_budget_exhausted_once(self._snapshot()))
        self.assertEqual(notify.call_count, 2)


class CallPhotoCheckRouting(unittest.TestCase):
    def _images(self):
        return [(b"img", "image/jpeg")]

    def test_deepseek_primary_success_skips_gemini(self):
        with mock.patch.object(m, "AI_PHOTO_PROVIDER", "deepseek"):
            with mock.patch.object(m, "_call_deepseek_json", return_value={"deep": True}) as ds, \
                 mock.patch.object(m, "_call_gemini_json", return_value={"gemini": True}) as gm:
                result = m._call_photo_check("p", self._images())
        self.assertEqual(result, {"deep": True})
        gm.assert_not_called()

    def test_deepseek_none_falls_back_to_gemini(self):
        with mock.patch.object(m, "AI_PHOTO_PROVIDER", "deepseek"):
            with mock.patch.object(m, "_call_deepseek_json", return_value=None), \
                 mock.patch.object(m, "_call_gemini_json", return_value={"gemini": True}) as gm:
                result = m._call_photo_check("p", self._images())
        self.assertEqual(result, {"gemini": True})
        # Fallback must hand Gemini inline_data parts, not raw bytes.
        parts = gm.call_args[0][1]
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(parts[0]["inline_data"]["data"], base64.b64encode(b"img").decode("ascii"))

    def test_deepseek_exception_falls_back_to_gemini(self):
        with mock.patch.object(m, "AI_PHOTO_PROVIDER", "deepseek"):
            with mock.patch.object(m, "_call_deepseek_json", side_effect=requests.exceptions.RequestException("boom")), \
                 mock.patch.object(m, "_call_gemini_json", return_value={"gemini": True}) as gm:
                result = m._call_photo_check("p", self._images())
        self.assertEqual(result, {"gemini": True})

    def test_both_providers_fail_returns_none(self):
        with mock.patch.object(m, "AI_PHOTO_PROVIDER", "deepseek"):
            with mock.patch.object(m, "_call_deepseek_json", return_value=None), \
                 mock.patch.object(m, "_call_gemini_json", return_value=None):
                self.assertIsNone(m._call_photo_check("p", self._images()))

    def test_gemini_provider_skips_deepseek(self):
        with mock.patch.object(m, "AI_PHOTO_PROVIDER", "gemini"):
            with mock.patch.object(m, "_call_deepseek_json", return_value={"deep": True}) as ds, \
                 mock.patch.object(m, "_call_gemini_json", return_value={"gemini": True}):
                result = m._call_photo_check("p", self._images())
        self.assertEqual(result, {"gemini": True})
        ds.assert_not_called()


class DownloadListingImage(unittest.TestCase):
    def test_upscale_first_then_fallback(self):
        good = mock.Mock()
        good.headers = {"Content-Type": "image/jpeg"}
        good.content = b"img"
        good.raise_for_status.return_value = None

        def fake_get(url, timeout=10):
            if "s-l1600" in url:
                raise requests.exceptions.ConnectionError("no big image")
            return good

        with mock.patch("requests.get", side_effect=fake_get):
            content, mime = m._download_listing_image("https://i.ebayimg.com/images/g/ABC/s-l225.jpg")
        self.assertEqual(content, b"img")
        self.assertEqual(mime, "image/jpeg")

    def test_upscale_success_requests_larger_size(self):
        good = mock.Mock()
        good.headers = {"Content-Type": "image/jpeg"}
        good.content = b"big"
        good.raise_for_status.return_value = None
        seen = []

        def fake_get(url, timeout=10):
            seen.append(url)
            return good

        with mock.patch("requests.get", side_effect=fake_get):
            m._download_listing_image("https://i.ebayimg.com/images/g/ABC/s-l225.jpg")
        self.assertIn("s-l1600", seen[0])

    def test_all_fail_returns_none(self):
        def fake_get(url, timeout=10):
            raise requests.exceptions.ConnectionError("down")

        with mock.patch("requests.get", side_effect=fake_get):
            self.assertIsNone(m._download_listing_image("https://i.ebayimg.com/images/g/ABC/s-l225.jpg"))


if __name__ == "__main__":
    unittest.main()
