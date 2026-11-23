"""Audit 11-23 (wave3 quality): alerts that broke the owner's wardrobe rules."""
from unittest import mock

import ebay_deal_alert as engine
import vinted_fast as fast
from test_vinted_fast import _lane, _no_live_seller_lookup, _run_offer  # noqa: F401  (autouse fixture)


def _shape(title, brand="peter millar", category="other"):
    listing = {"title": title, "size": "", "price": {"value": 10}}
    return fast._menswear_shape_reason(listing, brand, category)


def test_swim_trunks_are_off_like_shorts():
    # Sent 10-28 and 11-03 although trousers/shorts are off.
    assert "trousers/shorts" in _shape("Peter Millar Men's swim trunks")
    assert "trousers/shorts" in _shape("Peter Millar Men swimshorts")
    assert _shape("Peter Millar Men's quarter zip") is None
    # Review 11-23 N2: only swim trunks/shorts/wear, not every "swim" word.
    assert "trousers/shorts" in _shape("Peter Millar swimwear")
    assert "trousers/shorts" in _shape("Peter Millar swim shorts")
    assert "trousers/shorts" in _shape("Peter Millar trunks")
    assert "trousers/shorts" not in str(_shape("Peter Millar Gamecocks Swim & Dive polo"))
    assert "trousers/shorts" not in str(_shape("Peter Millar swimming pool print shirt"))


def test_eu_us_and_sz_collar_sizes_are_read():
    # Sent 11-01 (41/16) and 11-11 (Sz 16); the owner's collar is 16.5.
    assert fast._collar_size({"title": "Brioni dress shirt 41/16"}) == 16
    assert fast._collar_size({"title": "Zegna Dress Shirt Mens Sz 16 P2P. 22"}) == 16
    assert fast._collar_size({"title": "Kiton shirt 42/16.5"}) == 16.5
    assert fast._collar_size({"title": "Zegna dress shirt size 16.5"}) == 16.5
    assert fast._collar_size({"title": "Brioni dress shirt size 16 1/2"}) == 16.5
    assert fast._collar_size({"title": "Kiton shirt 42/16½"}) == 16.5
    assert fast._collar_size({"title": "Brooks Brothers Men's Suit 42L/38"}) is None
    assert fast._collar_size({"title": "Zegna pants 38/32"}) is None
    assert "collar 16 " in _shape("Brioni dress shirt 41/16", "brioni") + " "
    assert _shape("Kiton dress shirt 42/16.5", "kiton") is None


def test_offer_alerts_do_not_buzz_but_still_reach_telegram_silently(tmp_path):
    # Message alerts keep priority 3 and no mirror, as before.
    for resale, head, priority, silent in ((210, "OFFER", "2", True), (180, "MESSAGE", "3", False)):
        (tmp_path / head).mkdir()
        row, ntfy, send = _run_offer(tmp_path / head, 115, resale)
        send.assert_not_called()
        assert row["verdict"] == "OFFER_ALERTED"
        assert ntfy.call_args.args[0].startswith(head)
        assert ntfy.call_args.args[2] == priority
        assert ntfy.call_args.kwargs["silent_mirror"] is silent


def test_silent_offer_is_mirrored_without_sound(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    lane = _lane(tmp_path)
    with (
        mock.patch.object(engine, "telegram_mirror") as mirror,
        mock.patch.object(fast.requests, "post"),
    ):
        lane._ntfy("OFFER x", "body", "2", "moneybag", "https://www.vinted.com/items/1",
                   silent_mirror=True)
        lane._ntfy("SKIP x", "body", "2", "x")  # other priority-2 notes stay off Telegram
    assert mirror.call_count == 1 and mirror.call_args.kwargs["silent"] is True
    with mock.patch.object(engine.requests, "post") as post:
        engine._telegram_send("OFFER x\nbody", silent=True)
    assert post.call_args.kwargs["data"]["disable_notification"] == "true"
