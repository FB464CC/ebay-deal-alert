const crypto = require("crypto");

// eBay Marketplace Account Deletion/Closure Notifications endpoint.
// Required prerequisite for eBay's Application Growth Check (the free
// process to raise the Browse API's default 5,000 calls/day limit) - eBay
// won't let you submit that request until this is registered and verified.
//
// This app has nothing to purge on a deletion notice: it never stores any
// eBay buyer/seller PII (usernames, user IDs) tied to a real identity -
// only public listing metadata (item id, title, price, url) in a local
// dedupe cache. This endpoint exists purely to satisfy the subscription
// requirement, not because there's real user data at stake here.
//
// eBay's verification handshake (GET with ?challenge_code=X):
//   1. eBay sends a GET with a challenge_code query param.
//   2. Respond with {"challengeResponse": sha256hex(challengeCode + verificationToken + endpointUrl)},
//      hashed in exactly that order.
//   3. verificationToken is a secret string YOU choose (this app: EBAY_DELETION_VERIFICATION_TOKEN),
//      registered alongside the endpoint URL in the eBay Developer Portal.
//   4. endpointUrl must be the EXACT URL registered in the portal (including https://, no trailing slash
//      difference) - a mismatch here is the most common reason this fails eBay's live verification check.
//
// On actual deletion notifications (POST), just acknowledge with 200 - no
// action needed given nothing PII-bearing is stored.

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(Buffer.from(JSON.stringify(body), "utf-8"));
};

module.exports = async (req, res) => {
  const verificationToken = process.env.EBAY_DELETION_VERIFICATION_TOKEN;
  const endpointUrl = process.env.EBAY_DELETION_ENDPOINT_URL;

  if (!verificationToken || !endpointUrl) {
    return sendJson(res, 500, {
      error: "Missing EBAY_DELETION_VERIFICATION_TOKEN or EBAY_DELETION_ENDPOINT_URL env var",
    });
  }

  if (req.method === "GET") {
    const challengeCode = req.query?.challenge_code || new URL(req.url, "https://x").searchParams.get("challenge_code");
    if (!challengeCode) {
      return sendJson(res, 400, { error: "Missing challenge_code query param" });
    }
    const hash = crypto.createHash("sha256");
    hash.update(challengeCode);
    hash.update(verificationToken);
    hash.update(endpointUrl);
    return sendJson(res, 200, { challengeResponse: hash.digest("hex") });
  }

  if (req.method === "POST") {
    // Real deletion notification. Nothing to purge - see module comment.
    // Just acknowledge receipt as eBay requires.
    return sendJson(res, 200, { ok: true });
  }

  res.setHeader("Allow", "GET, POST");
  return sendJson(res, 405, { error: "Method not allowed" });
};
