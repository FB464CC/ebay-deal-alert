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
// Actual deletion notifications (POST) include an X-EBAY-SIGNATURE ECC
// signature. eBay's documented verification flow is to decode that header,
// fetch the referenced public key from the Notification API, and verify the
// JSON payload before processing it. Even though this app has nothing to
// purge, it must authenticate and validate a notification before acknowledging
// it so arbitrary callers cannot impersonate eBay.

const MAX_BODY_BYTES = 64 * 1024;
const PUBLIC_KEY_TTL_MS = 60 * 60 * 1000;
const TOKEN_EXPIRY_SKEW_MS = 60 * 1000;
const publicKeyCache = new Map();
let tokenCache;

class SignatureError extends Error {}

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(Buffer.from(JSON.stringify(body), "utf-8"));
};

const parseBody = async (req) => {
  if (req.body !== undefined) {
    if (Buffer.isBuffer(req.body)) {
      if (req.body.length > MAX_BODY_BYTES) throw new Error("Payload too large");
      return JSON.parse(req.body.toString("utf-8"));
    }
    if (typeof req.body === "string") {
      if (Buffer.byteLength(req.body, "utf-8") > MAX_BODY_BYTES) throw new Error("Payload too large");
      return JSON.parse(req.body);
    }
    if (req.body && typeof req.body === "object" && !Array.isArray(req.body)) return req.body;
    throw new Error("Invalid JSON body");
  }

  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new Error("Payload too large");
    chunks.push(buffer);
  }
  if (size === 0) throw new Error("Missing JSON body");
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
};

const isNonEmptyString = (value) => typeof value === "string" && value.trim().length > 0;

const isDeletionNotification = (body) => {
  const data = body?.notification?.data;
  return Boolean(
    body &&
      typeof body === "object" &&
      !Array.isArray(body) &&
      data &&
      typeof data === "object" &&
      !Array.isArray(data) &&
      (isNonEmptyString(data.username) || isNonEmptyString(data.userId)),
  );
};

const decodeSignatureHeader = (value) => {
  try {
    if (!isNonEmptyString(value) || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
      throw new Error();
    }

    const decoded = JSON.parse(Buffer.from(value, "base64").toString("utf-8"));
    if (
      decoded?.alg !== "ecdsa" ||
      decoded?.digest !== "SHA1" ||
      !isNonEmptyString(decoded.kid) ||
      !/^[A-Za-z0-9_-]+$/.test(decoded.kid) ||
      !isNonEmptyString(decoded.signature) ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(decoded.signature)
    ) {
      throw new Error();
    }
    return decoded;
  } catch (_error) {
    throw new SignatureError("Missing, malformed, or unsupported X-EBAY-SIGNATURE header");
  }
};

const getAppToken = async (clientId, clientSecret) => {
  if (tokenCache?.expiresAt > Date.now()) return tokenCache.value;

  const response = await fetch("https://api.ebay.com/identity/v1/oauth2/token", {
    method: "POST",
    headers: {
      Authorization: `Basic ${Buffer.from(`${clientId}:${clientSecret}`, "utf-8").toString("base64")}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      scope: "https://api.ebay.com/oauth/api_scope",
    }),
  });
  if (!response.ok) throw new Error(`eBay OAuth failed with HTTP ${response.status}`);

  const body = await response.json();
  if (!isNonEmptyString(body.access_token)) throw new Error("eBay OAuth response omitted access_token");
  const expiresIn = Number.isFinite(Number(body.expires_in)) ? Number(body.expires_in) : 7200;
  tokenCache = {
    value: body.access_token,
    expiresAt: Date.now() + Math.max(0, expiresIn * 1000 - TOKEN_EXPIRY_SKEW_MS),
  };
  return tokenCache.value;
};

const getPublicKey = async (keyId, clientId, clientSecret) => {
  const cached = publicKeyCache.get(keyId);
  if (cached?.expiresAt > Date.now()) return cached.value;

  const token = await getAppToken(clientId, clientSecret);
  const response = await fetch(
    `https://api.ebay.com/commerce/notification/v1/public_key/${encodeURIComponent(keyId)}`,
    { headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } },
  );
  if (!response.ok) throw new Error(`eBay public-key lookup failed with HTTP ${response.status}`);

  const body = await response.json();
  if (
    !isNonEmptyString(body.key) ||
    String(body.algorithm).toUpperCase() !== "ECDSA" ||
    String(body.digest).toUpperCase() !== "SHA1"
  ) {
    throw new Error("eBay returned an unsupported public key");
  }
  const key = body.key.replace("-----BEGIN PUBLIC KEY-----", "-----BEGIN PUBLIC KEY-----\n").replace(
    "-----END PUBLIC KEY-----",
    "\n-----END PUBLIC KEY-----",
  );
  publicKeyCache.set(keyId, { value: key, expiresAt: Date.now() + PUBLIC_KEY_TTL_MS });
  return key;
};

const verifyNotification = async (body, signatureHeader, clientId, clientSecret) => {
  const signature = decodeSignatureHeader(signatureHeader);
  const publicKey = await getPublicKey(signature.kid, clientId, clientSecret);
  try {
    const verifier = crypto.createVerify("sha1");
    verifier.update(JSON.stringify(body));
    verifier.end();
    return verifier.verify(publicKey, signature.signature, "base64");
  } catch (_error) {
    return false;
  }
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
    let body;
    try {
      body = await parseBody(req);
    } catch (_error) {
      return sendJson(res, 400, { error: "Malformed JSON notification payload" });
    }
    if (!isDeletionNotification(body)) {
      return sendJson(res, 400, { error: "Invalid account deletion notification payload" });
    }

    const clientId = process.env.EBAY_CLIENT_ID;
    const clientSecret = process.env.EBAY_CLIENT_SECRET;
    if (!clientId || !clientSecret) {
      return sendJson(res, 500, { error: "Missing EBAY_CLIENT_ID or EBAY_CLIENT_SECRET env var" });
    }

    try {
      const signatureHeader = req.headers?.["x-ebay-signature"] || req.headers?.["X-EBAY-SIGNATURE"];
      if (!(await verifyNotification(body, signatureHeader, clientId, clientSecret))) {
        return sendJson(res, 412, { error: "Invalid eBay notification signature" });
      }
    } catch (error) {
      if (error instanceof SignatureError) {
        return sendJson(res, 412, { error: "Invalid eBay notification signature" });
      }
      console.error("Unable to verify eBay account deletion notification", error);
      return sendJson(res, 500, { error: "Unable to verify eBay notification" });
    }

    // Authentic notification; nothing to purge - see module comment.
    return sendJson(res, 200, { ok: true });
  }

  res.setHeader("Allow", "GET, POST");
  return sendJson(res, 405, { error: "Method not allowed" });
};
