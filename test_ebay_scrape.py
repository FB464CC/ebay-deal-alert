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
    assert rolex["itemId"] == "ebay_scraped:123456789012"
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
