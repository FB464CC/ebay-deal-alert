"""Wave-3 buyable audit: unbuyable listings, suit label shorts, seller flag in title."""
from unittest import mock

import ebay_deal_alert as engine
import vinted_fast as fast
from test_vinted_fast import _fragrance_item, _fragrance_search, _listing_and_result

# Shapes copied from real logged-out item pages fetched 11-23.
_LIVE = r'"item":{\"can_buy\":true,\"instant_buy\":true,\"is_reserved\":false,\"is_hidden\":false}'
_SOLD = (r'{\"is_closed\":true,\"item_id\":\"10934942219\"}'
         r'\"can_buy\":false,\"instant_buy\":false,\"is_reserved\":false,\"is_hidden\":false')


def _page(text):
    resp = mock.Mock(status_code=200, text=text)
    with mock.patch.object(engine.requests, "get", return_value=resp):
        return engine.fetch_vinted_item_page_details("https://www.vinted.com/items/1")


def test_item_page_buyability_flags_are_parsed():
    live, sold = _page(_LIVE), _page(_SOLD)
    assert live["can_buy"] is True and live["is_reserved"] is False
    assert sold["can_buy"] is False and sold["is_closed"] is True
    assert "can_buy" not in _page("<html></html>")  # page change: no flag, fails open


def test_unbuyable_listing_rejects_before_ai(tmp_path):
    search = _fragrance_search()
    lane = fast.VintedFastLane(
        searches=[search], state_path=tmp_path / "state", seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl", schedule=[],
    )
    conn = engine.init_db(lane.seen_db_path)
    cases = [
        ({"_http_status": 200, "can_buy": False, "is_reserved": True}, "is_reserved"),
        ({"_http_status": 200, "can_buy": False, "is_closed": True}, "is_closed"),
        # Review 11-23 N3: can_buy false alone (one live sample) never blocks.
        ({"_http_status": 200, "can_buy": False}, None),
    ]
    for n, (details, word) in enumerate(cases):
        item = _fragrance_item(f"r{n}", "Parfums de Marly Delina Exclusif 75ml sealed", 220)
        with (
            mock.patch.object(engine, "fetch_vinted_item_page_details", return_value=details),
            mock.patch.object(engine, "check_photos_with_gemini") as photo,
        ):
            _listing, result, reason = lane._cheap_result(conn, None, item, search)
        if word is None:
            assert not str(reason or "").startswith("listing can't be bought")
            continue
        assert result is None and reason.startswith("listing can't be bought") and word in reason
        photo.assert_not_called()
    conn.close()


def _suit_view(tag, title="Z Zegna Men's Suit 40R 2 piece"):
    return {
        "listing": {"title": title, "size": "40R", "price": {"value": 50}},
        "category": "tailoring",
        "vinted_strong_checked": True,
        "vinted_strong_label_other_brand": False,
        "vinted_strong_counterfeit_indicators": False,
        "vinted_strong_size_on_tag": tag,
    }


def test_suit_label_short_or_off_size_vetoes_even_when_typed_size_fits():
    veto = fast.VintedFastLane._strong_look_veto_reason
    for tag in ("7-50 C Drop 7", "50 C", "52/C", "50 Corto", "50 Drop 7 C", "40S",
                "40 Short", "56 R", "38R", "25"):
        assert "suit label is not a 40-44" in (veto(None, _suit_view(tag)) or ""), tag
    for tag in ("42R", "52", "EU 52 L", "50 Drop 7 R", "US 42 / EU 52", "40L", None,
                "not readable", "L"):
        assert "suit label" not in (veto(None, _suit_view(tag)) or ""), tag
    # Not a suit: the tag rule leaves other garments to _off_size_reason.
    assert "suit label" not in (veto(None, _suit_view("50 C", "Zegna polo M")) or "")


def test_suit_pants_waist_in_title_no_longer_hides_a_short_label():
    # _off_size_reason returns early on a fitting pants waist, so the old tag
    # check never saw the jacket size on "suit ... pants 34" titles.
    veto = fast.VintedFastLane._strong_look_veto_reason
    view = _suit_view("50 C", "Zegna suit 40R jacket and pants 34")
    assert "suit label is not a 40-44" in veto(None, view)


def test_strong_prompt_asks_for_the_whole_size_code():
    assert "'7-50 C Drop 7'" in fast._vinted_strong_prompt({"listing": {"title": "suit"}}, "Zegna")


def test_new_or_low_rated_seller_is_in_the_alert_title(monkeypatch):
    monkeypatch.setattr(engine, "NTFY_TOPIC", "test-topic")
    for note, flag in (("new, 0 reviews", "NEW SELLER"),
                       ("LOW-RATED 3.2/5 (9 reviews): pay in-app only, report within 2 days",
                        "LOW-RATED"),
                       ("4.9/5 (40 reviews)", None)):
        _listing, result = _listing_and_result()
        result.update({"deal_rating": "Great Deal", "vinted_brand": "Peter Millar",
                       "vinted_expected_paid": 24.4, "vinted_seller_note": note})
        with (
            mock.patch.object(engine.requests, "post") as post,
            mock.patch.object(engine, "telegram_mirror"),
        ):
            engine.send_alert(result, feedback_item_id="vinted:123")
        title = post.call_args.kwargs["headers"]["Title"]
        head = title.split(":")[0]
        if flag:
            assert head.endswith(flag), title
        else:
            assert "SELLER" not in head and "LOW" not in head, title


def test_telegram_success_leaves_a_log_line(monkeypatch, caplog):
    monkeypatch.setattr(engine, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(engine, "TELEGRAM_CHAT_ID", "c")
    ok = mock.Mock(ok=True)
    ok.raise_for_status.return_value = None
    with mock.patch.object(engine.requests, "post", return_value=ok), caplog.at_level("INFO"):
        engine._telegram_send("STEAL Zegna\nbody", "https://x", "https://img")
        engine._telegram_send("STEAL Zegna\nbody", "https://x")
    sent = [r.getMessage() for r in caplog.records if "telegram mirror sent" in r.getMessage()]
    assert sent == ["telegram mirror sent (photo): STEAL Zegna",
                    "telegram mirror sent (text): STEAL Zegna"]


def test_suit_label_short_code_must_sit_beside_the_size():
    # Review 11-23 S3/N5: the S of "U.S.A." and "120's" vetoed real 42R suits;
    # Italian "Corta" let a short through.
    veto = fast.VintedFastLane._strong_look_veto_reason
    for tag in ("50 C", "52/C", "50 Corto", "52 Corta", "7-50 C Drop 7", "44S"):
        assert "suit label is not a 40-44" in (veto(None, _suit_view(tag)) or ""), tag
    for tag in ("42 R Made in U.S.A.", "U.S. 42 R", "42R (U.S.)", "42R Super 120's"):
        assert "suit label" not in (veto(None, _suit_view(tag)) or ""), tag
