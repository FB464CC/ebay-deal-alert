const crypto = require("crypto");

const AUTH_WINDOW_MS = 15 * 60 * 1000;
const AUTH_BACKOFF_AFTER = 3;
const AUTH_MAX_BACKOFF_SECONDS = 60;
const authAttempts = new Map();

const githubHeaders = () => ({
  Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "ebay-deal-alert-settings"
});

const contentsUrl = () => {
  const repo = process.env.GITHUB_REPO;
  return `https://api.github.com/repos/${repo}/contents/config.json`;
};

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  // Explicit UTF-8 Buffer, not a raw string - Vercel's Node runtime has a
  // known history of mis-encoding non-ASCII response strings (bytes get
  // read back as Latin-1), which mangles anything with an em-dash, curly
  // quote, etc.
  res.end(Buffer.from(JSON.stringify(body), "utf-8"));
};

const readBody = async (req) => {
  if (req.body && typeof req.body === "object") {
    return req.body;
  }
  if (typeof req.body === "string") {
    return JSON.parse(req.body);
  }

  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
};

const requireEnv = () => {
  const missing = ["GITHUB_TOKEN", "GITHUB_REPO", "SETTINGS_PASSWORD"].filter((key) => !process.env[key]);
  if (missing.length) {
    return `Missing required env vars: ${missing.join(", ")}`;
  }
  return null;
};

const passwordMatches = (provided) => {
  const expected = process.env.SETTINGS_PASSWORD;
  if (typeof provided !== "string" || typeof expected !== "string") {
    return false;
  }
  const providedHash = crypto.createHash("sha256").update(provided).digest();
  const expectedHash = crypto.createHash("sha256").update(expected).digest();
  return crypto.timingSafeEqual(providedHash, expectedHash);
};

const clientIp = (req) => {
  const forwarded = req.headers?.["x-forwarded-for"];
  return String(Array.isArray(forwarded) ? forwarded[0] : forwarded || req.socket?.remoteAddress || "unknown")
    .split(",")[0]
    .trim();
};

const authRetryAfter = (ip, now = Date.now()) => {
  const state = authAttempts.get(ip);
  if (!state || now - state.lastFailure > AUTH_WINDOW_MS) {
    authAttempts.delete(ip);
    return 0;
  }
  return Math.max(0, Math.ceil((state.blockedUntil - now) / 1000));
};

const recordAuthFailure = (ip, now = Date.now()) => {
  const prior = authAttempts.get(ip);
  const failures = !prior || now - prior.lastFailure > AUTH_WINDOW_MS ? 1 : prior.failures + 1;
  const retryAfter = failures < AUTH_BACKOFF_AFTER
    ? 0
    : Math.min(AUTH_MAX_BACKOFF_SECONDS, 2 ** (failures - AUTH_BACKOFF_AFTER));
  const state = { failures, lastFailure: now, blockedUntil: now + retryAfter * 1000 };
  authAttempts.set(ip, state);
  return { ...state, retryAfter };
};

const notifyAuthBurst = async (ip, failures) => {
  const topic = process.env.NTFY_TOPIC;
  if (!topic) return;
  try {
    const response = await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
      method: "POST",
      headers: { Title: "[ALERT-BOT DOWN]" },
      body: `Settings API authentication burst: ${failures} failures from ${ip}`
    });
    if (!response.ok) throw new Error(`ntfy returned HTTP ${response.status}`);
  } catch (error) {
    console.error("Unable to send config authentication alert", error);
  }
};

const validateStringArrayField = (body, field) => {
  if (!Array.isArray(body[field])) {
    return `${field} must be an array`;
  }
  const invalidIndex = body[field].findIndex((value) => typeof value !== "string");
  if (invalidIndex >= 0) {
    return `${field}[${invalidIndex}] must be a string`;
  }
  return null;
};

const validateConfig = (body) => {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return "Config body must be a JSON object";
  }

  if (!Array.isArray(body.SAVED_SEARCHES) || body.SAVED_SEARCHES.length === 0) {
    return "SAVED_SEARCHES must be a non-empty array";
  }
  for (const [index, search] of body.SAVED_SEARCHES.entries()) {
    if (!search || typeof search !== "object" || Array.isArray(search)) {
      return `SAVED_SEARCHES[${index}] must be an object`;
    }
    if (typeof search.query !== "string" || !search.query.trim()) {
      return `SAVED_SEARCHES[${index}].query must be a non-empty string`;
    }
    if (typeof search.max_price !== "number" || !Number.isFinite(search.max_price) || search.max_price < 0) {
      return `SAVED_SEARCHES[${index}].max_price must be a non-negative finite number`;
    }
    if ("size" in search && search.size !== null) {
      if (!Array.isArray(search.size)) {
        return `SAVED_SEARCHES[${index}].size must be a string array or null`;
      }
      const invalidSizeIndex = search.size.findIndex((value) => typeof value !== "string");
      if (invalidSizeIndex >= 0) {
        return `SAVED_SEARCHES[${index}].size[${invalidSizeIndex}] must be a string`;
      }
    }
    if ("enabled" in search && typeof search.enabled !== "boolean") {
      return `SAVED_SEARCHES[${index}].enabled must be a boolean`;
    }
    if ("profile" in search && typeof search.profile !== "string") {
      return `SAVED_SEARCHES[${index}].profile must be a string`;
    }
    if ("category_id" in search && typeof search.category_id !== "string") {
      return `SAVED_SEARCHES[${index}].category_id must be a string`;
    }
  }

  const arrayFields = [
    "GRAB_ON_SIGHT_BRANDS",
    "STANDARD_BRANDS",
    "PASS_BRANDS",
    "CORPORATE_LOGO_KEYWORDS",
    "CONDITION_HARD_FAIL_KEYWORDS",
    "CONDITION_FLAG_KEYWORDS",
    "FABRIC_GOOD_KEYWORDS",
    "GENDER_EXCLUDE_KEYWORDS"
  ];
  for (const field of arrayFields) {
    const error = validateStringArrayField(body, field);
    if (error) {
      return error;
    }
  }

  if (typeof body.FABRIC_POLY_KEYWORD !== "string") {
    return "FABRIC_POLY_KEYWORD must be a string";
  }
  if (typeof body.PIT_TO_PIT_CAP_INCHES !== "number" || Number.isNaN(body.PIT_TO_PIT_CAP_INCHES)) {
    return "PIT_TO_PIT_CAP_INCHES must be a number";
  }
  return null;
};

const fetchCurrentFile = async () => {
  const response = await fetch(contentsUrl(), {
    method: "GET",
    headers: githubHeaders()
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body.message || "Failed to fetch config.json from GitHub";
    const error = new Error(message);
    error.status = response.status;
    error.details = body;
    throw error;
  }
  return body;
};

module.exports = async (req, res) => {
  const ip = clientIp(req);
  const blockedFor = authRetryAfter(ip);
  if (blockedFor > 0) {
    res.setHeader("Retry-After", String(blockedFor));
    return sendJson(res, 429, { error: "Too many authentication attempts", retryAfterSeconds: blockedFor });
  }
  if (!passwordMatches(req.headers["x-settings-password"])) {
    const failure = recordAuthFailure(ip);
    if (failure.retryAfter > 0) {
      res.setHeader("Retry-After", String(failure.retryAfter));
    }
    if (failure.failures === 5) {
      await notifyAuthBurst(ip, failure.failures);
    }
    return sendJson(res, 401, { error: "Unauthorized" });
  }
  authAttempts.delete(ip);

  const envError = requireEnv();
  if (envError) {
    return sendJson(res, 500, { error: envError });
  }

  if (req.method === "GET") {
    try {
      const file = await fetchCurrentFile();
      const config = JSON.parse(Buffer.from(file.content, "base64").toString("utf8"));
      // sha travels with the config so the client can send it back as
      // baseSha on POST - see the concurrency check below for why.
      return sendJson(res, 200, { config, sha: file.sha });
    } catch (error) {
      return sendJson(res, error.status || 500, { error: error.message, details: error.details });
    }
  }

  if (req.method === "POST") {
    try {
      const { config: nextConfig, baseSha } = await readBody(req);
      const validationError = validateConfig(nextConfig);
      if (validationError) {
        return sendJson(res, 400, { error: validationError });
      }

      // No retry loop. config.json is a single shared document edited
      // directly via git many times a day (searches added/tuned, radii
      // changed, etc) - a stale-sha retry that just refetches and clobbers
      // harder is a data-loss amplifier, not a fix: a real edit that landed
      // between page-load and Save would be silently reverted with a 200
      // "ok" response and no trace anywhere. One GET, one conditional PUT,
      // and a real conflict is surfaced to the user instead of overwritten.
      const currentFile = await fetchCurrentFile();
      if (baseSha !== currentFile.sha) {
        return sendJson(res, 409, {
          error: "config.json changed since you loaded it - reload and re-apply your change"
        });
      }

      // Merge onto the current document rather than replacing it wholesale
      // - the settings UI only models a subset of config.json's fields, so
      // a straight overwrite would silently drop any key it doesn't know
      // about (see web/api/ledger.js for the equivalent read-merge-write
      // pattern on the ledger's own concurrency path).
      const current = JSON.parse(Buffer.from(currentFile.content, "base64").toString("utf8"));
      const merged = { ...current, ...nextConfig };

      const response = await fetch(contentsUrl(), {
        method: "PUT",
        headers: {
          ...githubHeaders(),
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message: "Update config via mobile settings app",
          content: Buffer.from(JSON.stringify(merged, null, 2) + "\n", "utf8").toString("base64"),
            sha: baseSha
        })
      });
      const body = await response.json().catch(() => ({}));
      if (response.ok) {
        return sendJson(res, 200, { ok: true, sha: body.content?.sha });
      }
      if (response.status === 409) {
        return sendJson(res, 409, {
          error: "config.json changed while you were saving - reload and re-apply your change"
        });
      }
      return sendJson(res, response.status, {
        error: body.message || "GitHub update failed",
        details: body
      });
    } catch (error) {
      return sendJson(res, error.status || 500, { error: error.message, details: error.details });
    }
  }

  res.setHeader("Allow", "GET, POST");
  return sendJson(res, 405, { error: "Method not allowed" });
};
