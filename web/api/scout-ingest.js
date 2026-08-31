const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const MAX_QUEUE_LINES = 2000;
const MAX_LISTINGS_PER_REQUEST = 250;
const QUEUE_FILE = "scout_queue.jsonl";
const SEEN_DB_FILE = "seen_items.db";
const RETRY_AFTER_SECONDS = 10 * 60;
const MAX_SEEN_DB_BYTES = 100 * 1024 * 1024;

let cachedSeenDatabase = null;
let seenDatabaseLoadPromise = Promise.resolve();

const githubHeaders = () => ({
  Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "ebay-deal-alert-scout"
});

const contentsUrl = (file = QUEUE_FILE) =>
  `https://api.github.com/repos/${process.env.GITHUB_REPO}/contents/${file}`;

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(Buffer.from(JSON.stringify(body), "utf8"));
};

const sendQueueFull = (res, body) => {
  res.setHeader("Retry-After", String(RETRY_AFTER_SECONDS));
  return sendJson(res, 429, { ...body, retryAfterSeconds: RETRY_AFTER_SECONDS });
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

const fetchRepositoryFile = async (file) => {
  const response = await fetch(contentsUrl(file), { headers: githubHeaders() });
  const body = await response.json().catch(() => ({}));
  if (response.status === 404) return null;
  if (!response.ok) {
    const error = new Error(body.message || `Failed to fetch ${file} from GitHub`);
    error.status = response.status;
    error.details = body;
    throw error;
  }
  return body;
};

const fetchCurrentFile = () => fetchRepositoryFile(QUEUE_FILE);

const seenDatabaseBytes = async (file) => {
  if (file?.encoding === "base64" && typeof file.content === "string" && file.content) {
    const bytes = Buffer.from(file.content.replace(/\s/g, ""), "base64");
    if (bytes.length > MAX_SEEN_DB_BYTES) throw new Error(`${SEEN_DB_FILE} exceeds ${MAX_SEEN_DB_BYTES} bytes`);
    return bytes;
  }
  if (!file?.download_url) throw new Error(`${SEEN_DB_FILE} has no downloadable content`);
  const response = await fetch(file.download_url, { headers: githubHeaders() });
  if (!response.ok) {
    const error = new Error(`Failed to download ${SEEN_DB_FILE} from GitHub`);
    error.status = response.status;
    throw error;
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > MAX_SEEN_DB_BYTES) throw new Error(`${SEEN_DB_FILE} exceeds ${MAX_SEEN_DB_BYTES} bytes`);
  return bytes;
};

const ensureSeenDatabase = async (file) => {
  if (!file?.sha) throw new Error(`${SEEN_DB_FILE} metadata is missing its sha`);
  if (cachedSeenDatabase?.sha === file.sha && fs.existsSync(cachedSeenDatabase.path)) {
    return cachedSeenDatabase.path;
  }

  seenDatabaseLoadPromise = seenDatabaseLoadPromise.catch(() => {}).then(async () => {
    if (cachedSeenDatabase?.sha === file.sha && fs.existsSync(cachedSeenDatabase.path)) {
      return cachedSeenDatabase.path;
    }
    const bytes = await seenDatabaseBytes(file);
    if (bytes.subarray(0, 16).toString("utf8") !== "SQLite format 3\u0000") {
      throw new Error(`${SEEN_DB_FILE} is not a SQLite database`);
    }
    const safeSha = file.sha.replace(/[^a-zA-Z0-9_-]/g, "");
    const databasePath = path.join(os.tmpdir(), `deal-scout-seen-${safeSha}.db`);
    const temporaryPath = `${databasePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
    await fs.promises.writeFile(temporaryPath, bytes);
    await fs.promises.rename(temporaryPath, databasePath).catch(async (error) => {
      await fs.promises.unlink(temporaryPath).catch(() => {});
      if (error.code !== "EEXIST") throw error;
    });
    const previous = cachedSeenDatabase;
    cachedSeenDatabase = { sha: file.sha, path: databasePath };
    if (previous?.path && previous.path !== databasePath) {
      await fs.promises.unlink(previous.path).catch(() => {});
    }
    return databasePath;
  });
  return seenDatabaseLoadPromise;
};

const findSeenKeysInDatabase = (databasePath, keys) => {
  const uniqueKeys = [...new Set(keys.filter((key) => typeof key === "string" && key))];
  if (!uniqueKeys.length) return new Set();
  const { DatabaseSync } = require("node:sqlite");
  const database = new DatabaseSync(databasePath, { readOnly: true });
  try {
    const placeholders = uniqueKeys.map(() => "?").join(",");
    const rows = database.prepare(`SELECT item_id FROM seen WHERE item_id IN (${placeholders})`).all(...uniqueKeys);
    return new Set(rows.map((row) => String(row.item_id)));
  } finally {
    database.close();
  }
};

const fetchSeenKeys = async (listings) => {
  const file = await fetchRepositoryFile(SEEN_DB_FILE);
  if (!file) throw new Error(`${SEEN_DB_FILE} was not found in the repository`);
  const databasePath = await ensureSeenDatabase(file);
  return findSeenKeysInDatabase(databasePath, listings.map(listingKey));
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
  for (const field of ["scoutSearchQuery", "scoutSearchLabel"]) {
    if (field in listing && (typeof listing[field] !== "string" || !listing[field].trim())) {
      return `${prefix}.${field} must be a non-empty string`;
    }
  }
  const limits = { platform: 100, itemId: 500, title: 1000, itemWebUrl: 4096, imageUrl: 4096, description: 10000, scoutSearchQuery: 1000, scoutSearchLabel: 1000 };
  for (const [field, limit] of Object.entries(limits)) {
    if (listing[field] && listing[field].length > limit) return `${prefix}.${field} exceeds ${limit} characters`;
  }
  for (const field of ["itemWebUrl", "imageUrl"]) {
    if (!listing[field]) continue;
    try {
      const url = new URL(listing[field]);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error();
    } catch {
      return `${prefix}.${field} must be an HTTP(S) URL without embedded credentials`;
    }
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
  ...(listing.scoutSearchQuery ? { scoutSearchQuery: listing.scoutSearchQuery.trim() } : {}),
  ...(listing.scoutSearchLabel ? { scoutSearchLabel: listing.scoutSearchLabel.trim() } : {}),
  discoveredAt: new Date().toISOString()
});

const listingKey = (listing) => `${listing.platform.trim().toLowerCase()}:${listing.itemId.trim()}`;

const existingKeys = (lines) => {
  const keys = new Set();
  for (const line of lines) {
    try {
      const listing = JSON.parse(line);
      if (listing && typeof listing.platform === "string" && typeof listing.itemId === "string") keys.add(listingKey(listing));
    } catch { /* malformed legacy line remains readable by the Python consumer */ }
  }
  return keys;
};

const withoutDuplicates = (listings, lines, durableSeenKeys = new Set()) => {
  const seen = existingKeys(lines);
  for (const key of durableSeenKeys) seen.add(key);
  return listings.filter((listing) => {
    const key = listingKey(listing);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
};

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
    if (body.listings.length > MAX_LISTINGS_PER_REQUEST) {
      return sendJson(res, 413, { error: `listings cannot exceed ${MAX_LISTINGS_PER_REQUEST} entries per request` });
    }
    for (const [index, listing] of body.listings.entries()) {
      const error = validateListing(listing, index);
      if (error) return sendJson(res, 400, { error });
    }

    const sanitized = body.listings.map(sanitizeListing);
    // The poller stores final dispositions in seen_items.db. Check that durable
    // state before considering queue capacity so periodic extension rescans do
    // not refill the queue with items the poller already resolved.
    const durableSeenKeys = await fetchSeenKeys(sanitized);
    const firstRead = await fetchCurrentFile();
    const currentLines = existingLines(firstRead);
    const newListings = withoutDuplicates(sanitized, currentLines, durableSeenKeys);
    if (!newListings.length) {
      return sendJson(res, 200, {
        ok: true,
        accepted: 0,
        dropped: body.listings.length,
        duplicates: body.listings.length,
        capacityDropped: 0,
        queueLines: currentLines.length
      });
    }
    if (currentLines.length >= MAX_QUEUE_LINES) {
      console.warn(`Scout ingest deferred ${newListings.length} listing(s): queue is at ${MAX_QUEUE_LINES} lines`);
      return sendQueueFull(res, {
        error: "Scout queue is full",
        accepted: 0,
        dropped: body.listings.length,
        duplicates: body.listings.length - newListings.length,
        capacityDropped: newListings.length,
        maxQueueLines: MAX_QUEUE_LINES
      });
    }

    // Re-fetch immediately before PUT and build against that content/sha.
    // If it changed since the capacity check, recalculate so the cap holds.
    const latest = await fetchCurrentFile();
    const latestLines = existingLines(latest);
    const finalAvailable = Math.max(0, MAX_QUEUE_LINES - latestLines.length);
    const finalCandidates = withoutDuplicates(newListings, latestLines, durableSeenKeys);
    const finalAccepted = finalCandidates.slice(0, finalAvailable);
    const capacityDropped = finalCandidates.length - finalAccepted.length;
    const duplicates = body.listings.length - finalCandidates.length;
    const finalDropped = body.listings.length - finalAccepted.length;
    if (!finalAccepted.length) {
      if (!capacityDropped) {
        return sendJson(res, 200, {
          ok: true, accepted: 0, dropped: finalDropped, duplicates,
          capacityDropped: 0, queueLines: latestLines.length
        });
      }
      return sendQueueFull(res, {
        error: "Scout queue is full",
        accepted: 0,
        dropped: finalDropped,
        duplicates,
        capacityDropped,
        maxQueueLines: MAX_QUEUE_LINES
      });
    }
    if (capacityDropped) console.warn(`Scout ingest accepted ${finalAccepted.length} and deferred ${capacityDropped} listing(s) at queue cap`);

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
    const result = {
      ok: !capacityDropped,
      accepted: finalAccepted.length,
      dropped: finalDropped,
      duplicates,
      capacityDropped,
      queueLines: allLines.length
    };
    return capacityDropped
      ? sendQueueFull(res, { error: "Scout queue is full", ...result, maxQueueLines: MAX_QUEUE_LINES })
      : sendJson(res, 200, result);
  } catch (error) {
    return sendJson(res, error.status || 500, { error: error.message, details: error.details });
  }
};

module.exports._test = { validateListing, withoutDuplicates, findSeenKeysInDatabase, sendQueueFull };
