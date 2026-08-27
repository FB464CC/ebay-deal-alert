(() => {
  const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;
  const title = document.querySelector('meta[property="og:title"]')?.content || document.title || canonical;
  const imageUrl = document.querySelector('meta[property="og:image"]')?.content || "";
  const description = document.querySelector('meta[property="og:description"]')?.content || "";
  const priceText = document.querySelector('meta[property="product:price:amount"]')?.content;
  const price = priceText && Number.isFinite(Number(priceText)) ? Number(priceText) : null;
  return [{
    platform: location.hostname.replace(/^www\./, ""),
    itemId: canonical,
    title,
    price,
    itemWebUrl: canonical,
    imageUrl,
    description
  }];
})();
