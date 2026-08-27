(() => {
  const found = [];
  const seen = new Set();

  function walk(value) {
    if (!value || typeof value !== "object") return;
    if (!Array.isArray(value) && Object.prototype.hasOwnProperty.call(value, "marketplace_listing_title")) {
      const id = value.id != null ? String(value.id) : "";
      if (id && !seen.has(id)) {
        seen.add(id);
        const city = value.location?.reverse_geocode?.city;
        const state = value.location?.reverse_geocode?.state;
        const rawPrice = value.listing_price?.amount;
        const parsedPrice = typeof rawPrice === "number" ? rawPrice : Number(String(rawPrice || "").replace(/[^0-9.]/g, ""));
        found.push({
          platform: "facebook",
          itemId: id,
          title: value.marketplace_listing_title || "",
          price: Number.isFinite(parsedPrice) && parsedPrice > 0 ? parsedPrice : null,
          itemWebUrl: `https://www.facebook.com/marketplace/item/${encodeURIComponent(id)}/`,
          imageUrl: value.primary_listing_photo?.image?.uri || "",
          description: city && state ? `Location: ${city}, ${state}` : ""
        });
      }
    }
    for (const child of Array.isArray(value) ? value : Object.values(value)) walk(child);
  }

  for (const script of document.querySelectorAll('script[type="application/json"]')) {
    try { walk(JSON.parse(script.textContent)); } catch (_error) { /* unrelated/non-JSON block */ }
  }
  return found.filter((listing) => listing.title && listing.price !== null);
})();
