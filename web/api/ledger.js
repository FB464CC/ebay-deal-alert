const crypto = require("crypto");

const githubHeaders = () => ({
  Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "ebay-deal-alert-settings"
});

const contentsUrl = () => {
  const repo = process.env.GITHUB_REPO;
  return `https://api.github.com/repos/${repo}/contents/ledger.jsonl`;
};

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
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

const fetchCurrentFile = async () => {
  const response = await fetch(contentsUrl(), {
    method: "GET",
    headers: githubHeaders()
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    const message = body.message || "Failed to fetch ledger.jsonl from GitHub";
    const error = new Error(message);
    error.status = response.status;
    error.details = body;
    throw error;
  }
  return body;
};

const parseLedger = (file) => {
  if (!file) {
    return [];
  }
  const text = Buffer.from(file.content, "base64").toString("utf8");
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
};

const validateNullableNumber = (entry, field) => {
  if (!(field in entry) || entry[field] === null) {
    return null;
  }
  return typeof entry[field] === "number" && Number.isFinite(entry[field]) && entry[field] >= 0
    ? null
    : `${field} must be a non-negative number or null`;
};

const validateNullableString = (entry, field) => {
  if (!(field in entry) || entry[field] === null) {
    return null;
  }
  return typeof entry[field] === "string" ? null : `${field} must be a string or null`;
};

const validateLedgerEntry = (entry) => {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    return "Ledger entry must be a JSON object";
  }
  if (typeof entry.item_id !== "string" || !entry.item_id.trim()) {
    return "item_id must be a non-empty string";
  }
  const stringFields = ["title", "bought_date", "sold_date", "sold_platform"];
  for (const field of stringFields) {
    const error = validateNullableString(entry, field);
    if (error) {
      return error;
    }
  }
  const numberFields = ["bought_price", "sold_price"];
  for (const field of numberFields) {
    const error = validateNullableNumber(entry, field);
    if (error) {
      return error;
    }
  }
  return null;
};

const serializeLedger = (ledger) => ledger.map((entry) => JSON.stringify(entry)).join("\n") + (ledger.length ? "\n" : "");

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
      return sendJson(res, 200, parseLedger(file));
    } catch (error) {
      return sendJson(res, error.status || 500, { error: error.message, details: error.details });
    }
  }

  if (req.method === "POST") {
    try {
      const entry = await readBody(req);
      const validationError = validateLedgerEntry(entry);
      if (validationError) {
        return sendJson(res, 400, { error: validationError });
      }

      const LEDGER_FIELDS = [
        "item_id", "title", "bought_price", "bought_date",
        "sold_price", "sold_date", "sold_platform"
      ];
      const sanitizeEntry = (raw) => {
        const clean = {};
        for (const field of LEDGER_FIELDS) {
          if (field in raw) {
            clean[field] = raw[field];
          }
        }
        clean.item_id = raw.item_id.trim();
        return clean;
      };
      const cleanEntry = sanitizeEntry(entry);
      const maxConflictRetries = 2;
      for (let attempt = 0; attempt <= maxConflictRetries; attempt += 1) {
        const currentFile = await fetchCurrentFile();
        const ledger = parseLedger(currentFile);
        const index = ledger.findIndex((line) => String(line.item_id) === cleanEntry.item_id);
        if (index >= 0) {
          ledger[index] = { ...ledger[index], ...cleanEntry };
        } else {
          ledger.push(cleanEntry);
        }

        const body = {
          message: "Update ledger via mobile settings app",
          content: Buffer.from(serializeLedger(ledger), "utf8").toString("base64")
        };
        if (currentFile?.sha) {
          body.sha = currentFile.sha;
        }

        const response = await fetch(contentsUrl(), {
          method: "PUT",
          headers: {
            ...githubHeaders(),
            "Content-Type": "application/json"
          },
          body: JSON.stringify(body)
        });
        const responseBody = await response.json().catch(() => ({}));
        if (response.ok) {
          return sendJson(res, 200, ledger);
        }
        if (response.status !== 409 || attempt === maxConflictRetries) {
          return sendJson(res, response.status, {
            error: responseBody.message || "GitHub update failed",
            details: responseBody
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
