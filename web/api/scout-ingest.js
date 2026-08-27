const crypto = require("crypto");

const MAX_QUEUE_LINES = 2000;
const QUEUE_FILE = "scout_queue.jsonl";

const githubHeaders = () => ({
  Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "ebay-deal-alert-scout"
});

const contentsUrl = () =>
  `https://api.github.com/repos/${process.env.GITHUB_REPO}/contents/${QUEUE_FILE}`;

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(Buffer.from(JSON.stringify(body), "utf8"));
};

const readBody = async (req) => {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string") return JSON.parse(req.body);
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
};

const requireEnv = () => {
  const missing = ["GITHUB_TOKEN", "GITHUB_REPO", "SCOUT_INGEST_SECRET"]
    .filter((key) => !process.env[key]);
  return missing.length ? `Missing required env vars: ${missing.join(", ")}` : null;
};

const secretMatches = (provided) => {
  const expected = process.env.SCOUT_INGEST_SECRET;
  if (typeof provided !== "string" || typeof expected !== "string") return false;
  const actualBuffer = Buffer.from(provided);
  const expectedBuffer = Buffer.from(expected);
  return actualBuffer.length === expectedBuffer.length &&
    crypto.timingSafeEqual(actualBuffer, expectedBuffer);
};

const fetchCurrentFile = async () => {
  const response = await fetch(contentsUrl(), { headers: githubHeaders() });
  const body = await response.json().catch(() => ({}));
  if (response.status === 404) return null;
  if (!response.ok) {
    const error = new Error(body.message || `Failed to fetch ${QUEUE_FILE} from GitHub`);
    error.status = response.status;
    error.details = body;
    throw error;
  }
  return body;
};

const existingLines = (file) => file
  ? Buffer.from(file.content, "base64").toString("utf8").split("\n").filter((line) => line.trim())
  : [];

const validateListing = (listing, index) => {
  const prefix = `listings[${index}]`;
  if (!listing || typeof listing !== "object" || Array.isArray(listing)) return `${prefix} must be an object`;
  for (const field of ["platform", "itemId", "title"]) {
    if (typeof listing[field] !== "string" || !listing[field].trim()) return `${prefix}.${field} must be a non-empty string`;
  }
  if (!(listing.price === null || (typeof listing.price === "number" && Number.isFinite(listing.price)))) {
    return `${prefix}.price must be a number or null`;
  }
  for (const field of ["itemWebUrl", "imageUrl", "description"]) {
    if (typeof listing[field] !== "string") return `${prefix}.${field} must be a string`;
  }
  return null;
};

const sanitizeListing = (listing) => ({
  platform: listing.platform.trim(),
  itemId: listing.itemId.trim(),
  title: listing.title.trim(),
  price: listing.price,
  itemWebUrl: listing.itemWebUrl,
  imageUrl: listing.imageUrl,
  description: listing.description,
  discoveredAt: new Date().toISOString()
});

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return sendJson(res, 405, { error: "Method not allowed" });
  }
  if (!secretMatches(req.headers["x-scout-secret"])) return sendJson(res, 401, { error: "Unauthorized" });
  const envError = requireEnv();
  if (envError) return sendJson(res, 500, { error: envError });

  try {
    const body = await readBody(req);
    if (!body || typeof body !== "object" || !Array.isArray(body.listings) || !body.listings.length) {
      return sendJson(res, 400, { error: "listings must be a non-empty array" });
    }
    for (const [index, listing] of body.listings.entries()) {
      const error = validateListing(listing, index);
      if (error) return sendJson(res, 400, { error });
    }

    const firstRead = await fetchCurrentFile();
    const currentLines = existingLines(firstRead);
    const available = Math.max(0, MAX_QUEUE_LINES - currentLines.length);
    const accepted = body.listings.slice(0, available).map(sanitizeListing);
    const dropped = body.listings.length - accepted.length;
    if (!accepted.length) {
      console.warn(`Scout ingest dropped ${dropped} listing(s): queue is at ${MAX_QUEUE_LINES} lines`);
      return sendJson(res, 429, { error: "Scout queue is full", accepted: 0, dropped, maxQueueLines: MAX_QUEUE_LINES });
    }

    // Re-fetch immediately before PUT and build against that content/sha.
    // If it changed since the capacity check, recalculate so the cap holds.
    const latest = await fetchCurrentFile();
    const latestLines = existingLines(latest);
    const finalAvailable = Math.max(0, MAX_QUEUE_LINES - latestLines.length);
    const finalAccepted = accepted.slice(0, finalAvailable);
    const finalDropped = body.listings.length - finalAccepted.length;
    if (!finalAccepted.length) {
      console.warn(`Scout ingest dropped ${finalDropped} listing(s): queue filled during ingest`);
      return sendJson(res, 409, { error: "Scout queue filled during ingest", accepted: 0, dropped: finalDropped });
    }
    if (finalDropped) console.warn(`Scout ingest accepted ${finalAccepted.length} and dropped ${finalDropped} listing(s) at queue cap`);

    const allLines = latestLines.concat(finalAccepted.map((entry) => JSON.stringify(entry)));
    const update = {
      message: `Scout ingest: ${finalAccepted.length} listing(s) from extension`,
      content: Buffer.from(allLines.join("\n") + "\n", "utf8").toString("base64")
    };
    if (latest?.sha) update.sha = latest.sha;
    const response = await fetch(contentsUrl(), {
      method: "PUT",
      headers: { ...githubHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(update)
    });
    const responseBody = await response.json().catch(() => ({}));
    if (!response.ok) return sendJson(res, response.status, { error: responseBody.message || "GitHub update failed", details: responseBody });
    return sendJson(res, 200, { ok: true, accepted: finalAccepted.length, dropped: finalDropped, queueLines: allLines.length });
  } catch (error) {
    return sendJson(res, error.status || 500, { error: error.message, details: error.details });
  }
};
