const crypto = require("crypto");

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
  const providedBuffer = Buffer.from(provided);
  const expectedBuffer = Buffer.from(expected);
  if (providedBuffer.length !== expectedBuffer.length) {
    return false;
  }
  return crypto.timingSafeEqual(providedBuffer, expectedBuffer);
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
  if (!passwordMatches(req.headers["x-settings-password"])) {
    return sendJson(res, 401, { error: "Unauthorized" });
  }

  const envError = requireEnv();
  if (envError) {
    return sendJson(res, 500, { error: envError });
  }

  if (req.method === "GET") {
    try {
      const file = await fetchCurrentFile();
      const config = JSON.parse(Buffer.from(file.content, "base64").toString("utf8"));
      return sendJson(res, 200, config);
    } catch (error) {
      return sendJson(res, error.status || 500, { error: error.message, details: error.details });
    }
  }

  if (req.method === "POST") {
    try {
      const nextConfig = await readBody(req);
      const validationError = validateConfig(nextConfig);
      if (validationError) {
        return sendJson(res, 400, { error: validationError });
      }

      const maxConflictRetries = 2;
      for (let attempt = 0; attempt <= maxConflictRetries; attempt += 1) {
        const currentFile = await fetchCurrentFile();
        const response = await fetch(contentsUrl(), {
          method: "PUT",
          headers: {
            ...githubHeaders(),
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            message: "Update config via mobile settings app",
            content: Buffer.from(JSON.stringify(nextConfig, null, 2) + "\n", "utf8").toString("base64"),
            sha: currentFile.sha
          })
        });
        const body = await response.json().catch(() => ({}));
        if (response.ok) {
          return sendJson(res, 200, { ok: true });
        }
        if (response.status !== 409 || attempt === maxConflictRetries) {
          return sendJson(res, response.status, {
            error: body.message || "GitHub update failed",
            details: body
          });
        }
      }
    } catch (error) {
      return sendJson(res, error.status || 500, { error: error.message, details: error.details });
    }
  }

  res.setHeader("Allow", "GET, POST");
  return sendJson(res, 405, { error: "Method not allowed" });
};
