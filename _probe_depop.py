from scrapling.fetchers import Fetcher

def decode(resp):
    if isinstance(resp.body, bytes):
        return resp.body.decode(resp.encoding or "utf-8", errors="replace")
    return resp.body

url = "https://www.depop.com/products/jayusfinds-moissanite-rose-gold-luxury-watch-d1ca/"
resp = Fetcher.get(url, timeout=20)
print("SLUG-ONLY URL", url, "->", resp.status, getattr(resp, "url", "?"))
