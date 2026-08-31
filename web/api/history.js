const crypto = require("crypto");

const githubHeaders = () => ({
  Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "ebay-deal-alert-settings"
});

const contentsUrl = () => {
  const repo = process.env.GITHUB_REPO;
  return `https://api.github.com/repos/${repo}/contents/alerts_log.jsonl`;
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

const fetchHistoryFile = async () => {
  const response = await fetch(contentsUrl(), {
    method: "GET",
    headers: githubHeaders()
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    const message = body.message || "Failed to fetch alerts_log.jsonl from GitHub";
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

  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    return sendJson(res, 405, { error: "Method not allowed" });
  }

  try {
    const file = await fetchHistoryFile();
    if (!file) {
      return sendJson(res, 200, { history: [], skipped: 0 });
    }
    const text = Buffer.from(file.content, "base64").toString("utf8");
    const history = [];
    let skipped = 0;
    for (const [index, rawLine] of text.split("\n").entries()) {
      const line = rawLine.trim();
      if (!line) {
        continue;
      }
      try {
        history.push(JSON.parse(line));
      } catch (error) {
        skipped += 1;
        console.warn(`Skipping invalid alerts_log.jsonl line ${index + 1}: ${error.message}`);
      }
    }
    history.reverse();
    return sendJson(res, 200, { history, skipped });
  } catch (error) {
    return sendJson(res, error.status || 500, { error: error.message, details: error.details });
  }
};
