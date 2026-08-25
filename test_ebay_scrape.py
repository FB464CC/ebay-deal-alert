"""Unit tests for ebay_scrape.py - the supplementary scraped eBay lane.

No live network calls: Fetcher.get is mocked/replaced with a fake response
built from an inline HTML fixture modeled on eBay's real search results
markup (confirmed live: <li class="s-card ...> cards, each carrying a
data-listingid, a "s-card__title" span, a "$"-formatted "s-card__price"
span, and an "s-card__image" <img>, plus fake "Shop on eBay" ad cards that
must be skipped).
"""

from unittest.mock import patch

from ebay_scrape import search_ebay_scraped, _parse_listings

# Two real-shaped listing cards + one "Shop on eBay" ad placeholder card,
# same structural pattern eBay's live search page uses.
FIXTURE_HTML = """
<ul class="srp-results srp-grid clearfix">
<li class="s-card s-card--vertical" data-listingid="123456789012">
<div class="su-card-container"><a class="s-card__link" href="https://www.ebay.com/itm/123456789012?hash=abc">
<img class="s-card__image" loading="eager" src="https://i.ebayimg.com/images/g/aaa/s-l500.webp">
</a><div class="s-card__title"><span class="su-styled-text primary default">Rolex Datejust 36mm Steel Watch</span><span class="clipped">New Listing</span></div>
<span class="su-styled-text primary bold large-1 s-card__price">$1,234.56</span>
</div></li>
<li class="s-card s-card--vertical" data-listingid="222233334444">
<div class="su-card-container"><a class="s-card__link" href="https://www.ebay.com/itm/222233334444?hash=def">
<img class="s-card__image" loading="lazy" src="https://i.ebayimg.com/images/g/bbb/s-l500.webp">
</a><div class="s-card__title"><span class="su-styled-text primary default">Seiko 5 Automatic Watch</span><span class="clipped">New Listing</span></div>
<span class="su-styled-text primary bold large-1 s-card__price">$89.99</span>
</div></li>
<li class="s-card s-card--vertical" data-listingid="9999999999999999">
<div class="su-card-container"><a class="s-card__link image-treatment" href="https://ebay.com/itm/123456?hash=fake">
<img class="s-card__image" loading="lazy" src="https://ir.ebaystatic.com/rs/v/placeholder.png">
</a><div class="s-card__title"><span class="su-styled-text primary default">Shop on eBay</span></div>
<span class="su-styled-text primary bold large-1 s-card__price">$20.00</span>
</div></li>
</ul>
"""


class FakeResponse:
    def __init__(self, status, html_content=""):
        self.status = status
        self.html_content = html_content


def test_extracts_real_listings_from_fixture_html():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, FIXTURE_HTML)
        results = search_ebay_scraped("rolex")

    assert len(results) == 2
    titles = {r["title"] for r in results}
    assert titles == {"Rolex Datejust 36mm Steel Watch", "Seiko 5 Automatic Watch"}
    # "Shop on eBay" ad placeholder card must be filtered out.
    assert "Shop on eBay" not in titles


def test_listing_shape_matches_make_listing_output():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, FIXTURE_HTML)
        results = search_ebay_scraped("rolex")

    rolex = next(r for r in results if "Rolex" in r["title"])
    # itemId is deliberately reformatted to eBay's own bare "v1|<id>|0"
    # convention (NOT make_listing()'s default "platform:id" namespacing) -
    # see the override's comment in ebay_scrape.py. This is what lets this
    # scraped lane dedupe correctly against the official Browse API's own
    # itemId for the exact same physical listing, instead of looking like
    # two different items and potentially double-alerting.
    assert rolex["itemId"] == "v1|123456789012|0"
    assert rolex["itemWebUrl"] == "https://www.ebay.com/itm/123456789012"
    assert rolex["platform"] == "ebay_scraped"
    assert rolex["image"]["imageUrl"] == "https://i.ebayimg.com/images/g/aaa/s-l500.webp"


def test_price_parsing_handles_comma_thousands_format():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, FIXTURE_HTML)
        results = search_ebay_scraped("rolex")

    rolex = next(r for r in results if "Rolex" in r["title"])
    assert rolex["price"] == {"value": 1234.56, "currency": "USD"}
    seiko = next(r for r in results if "Seiko" in r["title"])
    assert seiko["price"] == {"value": 89.99, "currency": "USD"}


def test_non_200_response_returns_empty_list():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(403, "")
        results = search_ebay_scraped("rolex")

    assert results == []


def test_proxy_env_var_gets_passed_to_fetcher():
    # Real fix: eBay 403s 100% of calls from GitHub Actions' shared runner
    # IP range (confirmed live against a real GH Actions run), vs ~1-in-10
    # from a residential IP. EBAY_SCRAPE_PROXY_URL routes through a real
    # residential proxy instead - must reach Fetcher.get's proxy kwarg.
    with patch("ebay_scrape.Fetcher") as mock_fetcher, \
         patch.dict("os.environ", {"EBAY_SCRAPE_PROXY_URL": "http://user:pass@proxy.example:8888"}):
        mock_fetcher.get.return_value = FakeResponse(200, FIXTURE_HTML)
        search_ebay_scraped("rolex")

    assert mock_fetcher.get.call_args.kwargs.get("proxy") == "http://user:pass@proxy.example:8888"


def test_no_proxy_configured_calls_fetcher_without_proxy_kwarg():
    # Optional by design - unset, this must still work exactly as before
    # (direct call, no proxy kwarg at all), not pass proxy=None.
    with patch("ebay_scrape.Fetcher") as mock_fetcher, \
         patch.dict("os.environ", {}, clear=True):
        mock_fetcher.get.return_value = FakeResponse(200, FIXTURE_HTML)
        search_ebay_scraped("rolex")

    assert "proxy" not in mock_fetcher.get.call_args.kwargs


def test_malformed_or_empty_html_returns_empty_list():
    assert _parse_listings("") == []
    assert _parse_listings("<html><body>not eBay markup at all</body></html>") == []

    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, "<html>garbage</html>")
        results = search_ebay_scraped("rolex")
    assert results == []


def test_network_exception_returns_empty_list_never_raises():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.side_effect = Exception("connection reset")
        results = search_ebay_scraped("rolex")

    assert results == []


# Real live bug: even after classify_stray_auction_listing() fixed the
# official Browse API lane, the user kept getting alerted on eBay auctions
# with days left. Root cause found live: THIS scrape lane's price regex
# also matches a live auction card's price span - which is just the
# CURRENT BID, not a real price - and _parse_listings() never extracted
# any buyingOptions/bidCount/end-date data at all, so every scraped
# auction sailed through as if it were fixed-price. Confirmed live against
# eBay's real search HTML: an auction card - and ONLY an auction card -
# carries a `s-card__time-left` span ("2d 23h", "2h 9m", "35s"), right by
# a "N bids" span; a fixed-price card has neither. Fix stamps
# buyingOptions=["AUCTION"]/bidCount/a synthesized itemEndDate onto these
# listings in the exact field shape classify_stray_auction_listing()
# already expects, so that ONE shared gate (not new threshold logic here)
# now protects both lanes.
AUCTION_CARD_HTML = """
<ul class="srp-results srp-grid clearfix">
<li class="s-card s-card--vertical" data-listingid="555566667777">
<div class="su-card-container"><a class="s-card__link" href="https://www.ebay.com/itm/555566667777?hash=ghi">
<img class="s-card__image" loading="eager" src="https://i.ebayimg.com/images/g/ccc/s-l500.webp">
</a><div class="s-card__title"><span class="su-styled-text primary default">Vintage Omega Speedmaster Auction</span><span class="clipped">New Listing</span></div>
<span class="su-styled-text primary bold large-1 s-card__price">$3,554.00</span>
<span class="su-styled-text secondary large">2 bids</span><span class="s-card__time "><span class="clipped">Time left</span><span class="s-card__time-left">2d 23h</span></span>
</div></li>
<li class="s-card s-card--vertical" data-listingid="888899990000">
<div class="su-card-container"><a class="s-card__link" href="https://www.ebay.com/itm/888899990000?hash=jkl">
<img class="s-card__image" loading="eager" src="https://i.ebayimg.com/images/g/ddd/s-l500.webp">
</a><div class="s-card__title"><span class="su-styled-text primary default">Fixed Price Casio Watch</span><span class="clipped">New Listing</span></div>
<span class="su-styled-text primary bold large-1 s-card__price">$45.00</span>
</div></li>
<li class="s-card s-card--vertical" data-listingid="111122223333">
<div class="su-card-container"><a class="s-card__link" href="https://www.ebay.com/itm/111122223333?hash=mno">
<img class="s-card__image" loading="eager" src="https://i.ebayimg.com/images/g/eee/s-l500.webp">
</a><div class="s-card__title"><span class="su-styled-text primary default">Auction Ending Soon Watch</span><span class="clipped">New Listing</span></div>
<span class="su-styled-text primary bold large-1 s-card__price">$210.00</span>
<span class="su-styled-text secondary large">0 bids</span><span class="s-card__time "><span class="clipped">Time left</span><span class="s-card__time-left">9m</span></span>
</div></li>
</ul>
"""


def test_auction_card_gets_tagged_with_buying_options_and_bid_count():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, AUCTION_CARD_HTML)
        results = search_ebay_scraped("omega")

    auction = next(r for r in results if "Speedmaster" in r["title"])
    assert auction["buyingOptions"] == ["AUCTION"]
    assert auction["bidCount"] == 2
    assert "itemEndDate" in auction


def test_auction_card_synthesized_end_date_is_far_out_days_from_now():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, AUCTION_CARD_HTML)
        results = search_ebay_scraped("omega")

    from datetime import datetime, timezone
    auction = next(r for r in results if "Speedmaster" in r["title"])
    end = datetime.fromisoformat(auction["itemEndDate"].replace("Z", "+00:00"))
    minutes_out = (end - datetime.now(timezone.utc)).total_seconds() / 60
    # "2d 23h" -> roughly 4260 minutes, nowhere near the 15-min closing
    # window - this is the exact listing classify_stray_auction_listing()
    # must skip rather than alert on at its current-bid price.
    assert 4000 < minutes_out < 4400


def test_fixed_price_card_gets_no_auction_fields():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, AUCTION_CARD_HTML)
        results = search_ebay_scraped("omega")

    fixed = next(r for r in results if "Casio" in r["title"])
    assert "buyingOptions" not in fixed
    assert "itemEndDate" not in fixed


def test_ending_soon_auction_card_end_date_is_within_minutes():
    with patch("ebay_scrape.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = FakeResponse(200, AUCTION_CARD_HTML)
        results = search_ebay_scraped("omega")

    from datetime import datetime, timezone
    ending_soon = next(r for r in results if "Ending Soon" in r["title"])
    assert ending_soon["bidCount"] == 0
    end = datetime.fromisoformat(ending_soon["itemEndDate"].replace("Z", "+00:00"))
    minutes_out = (end - datetime.now(timezone.utc)).total_seconds() / 60
    assert 7 < minutes_out < 11


def test_parse_time_left_minutes_handles_all_observed_formats():
    from ebay_scrape import _parse_time_left_minutes
    assert _parse_time_left_minutes("2d 23h") == 2 * 1440 + 23 * 60
    assert _parse_time_left_minutes("2h 9m") == 2 * 60 + 9
    assert _parse_time_left_minutes("35s") == 35 / 60
    assert _parse_time_left_minutes("9m") == 9
    assert _parse_time_left_minutes("") is None
    assert _parse_time_left_minutes(None) is None
    assert _parse_time_left_minutes("garbage") is None
