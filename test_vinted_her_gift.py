"""Owner 11-24: women's Cucinelli / Loro Piana coats and knits, size S, for Kenzie."""
from unittest import mock

import ebay_deal_alert as engine
import vinted_fast as fast

PROVEN = {"_http_status": 200, "feedback_count": 20, "feedback_reputation": 1.0, "item_count": 30}


def _item(title="Brunello Cucinelli UK 8 black corduroy puffer coat", size="S / US 4-6",
          brand="Brunello Cucinelli", price="60", status="Very good"):
    return {
        "id": "10953454097", "title": title, "brand_title": brand, "size_title": size,
        "status": status, "price": {"amount": price},
        "total_item_price": {"amount": str(float(price) * 1.05 + 0.70)},
        "url": "https://www.vinted.com/items/10953454097", "user": {"id": 5, "login": "s"},
        "photo": {"url": "https://images.example/main.jpg", "is_main": True,
                  "high_resolution": {"timestamp": 1_700_000_000}},
        "photos": [],
    }


def test_matches_only_her_houses_size_and_garments():
    match = fast._her_gift_search
    assert match(_item())["category"] == "gift"
    assert match(_item(title="Cashmere cardigan", brand="Loro Piana"))["query"] == "loro piana"
    assert match(_item(size="S")) is None  # men's S goes to the menswear searches
    assert match(_item(size="M / US 8-10")) is None
    assert match(_item(brand="Max Mara", title="Max Mara puffer coat")) is None
    assert match(_item(title="Brunello Cucinelli silk dress")) is None
    assert match(_item(title="Brunello Cucinelli knit pants")) is None
    with mock.patch.dict(fast.HER_GIFT, {"enabled": False}):
        assert match(_item()) is None


def test_cap_is_all_in_and_condition_must_be_gift_ready():
    r = fast._her_gift_reason
    assert r("Very good", 60, 5) is None  # (63.70 + 5) * 1.06 = $72.82
    assert "over the $100 cap" in r("New with tags", 95, 0)  # $106.48
    assert "not gift-ready" in r("Good", 20, 0)


def _run(tmp_path, item, stats):
    lane = fast.VintedFastLane(
        searches=[], state_path=tmp_path / "state.json", seen_db_path=tmp_path / "seen.db",
        log_path=tmp_path / "lane.jsonl", main_seen_db_path=tmp_path / "missing-main.db",
        run_seconds=1, sleeper=lambda _s: None,
    )
    conn = engine.init_db(lane.seen_db_path)
    page = {"_http_status": 200, "catalog_path": ["Women", "Clothing", "Outerwear"],
            "description": "Excellent condition", "shipping_price": 5.0}
    with (
        mock.patch.object(engine, "fetch_vinted_item_page_details", return_value=page),
        mock.patch.object(fast, "fetch_vinted_seller_stats", return_value=stats),
        mock.patch.object(lane, "_ntfy") as ntfy,
        mock.patch.object(lane, "_finish", wraps=lane._finish) as finish,
    ):
        lane._process_item(conn, None, item, fast._her_gift_search(item), None)
    conn.close()
    return ntfy, finish


def test_womens_small_cucinelli_coat_alerts_from_a_proven_seller(tmp_path):
    ntfy, finish = _run(tmp_path, _item(), PROVEN)
    assert ntfy.called, finish.call_args
    assert "Gift for Kenzie" in ntfy.call_args.args[1]


def test_no_review_seller_never_alerts(tmp_path):
    # The 11-24 listing itself: seller guguc2 had 0 reviews.
    ntfy, finish = _run(tmp_path, _item(), {"_http_status": 200, "feedback_count": 0})
    assert not ntfy.called
    assert "seller not proven" in finish.call_args.args[5]
