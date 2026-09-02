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

// Real live bug: GitHub's Contents API only inlines `content` for files
// <=1MB - past that it returns 200 with content:null, encoding:"none", and
// a download_url instead. alerts_log.jsonl repeatedly crosses 1MB on a
// heavy day (golf's high junk-candidate volume in particular), so this
// silently made the dashboard's history view go blank/stale every time,
// with no error surfaced anywhere - Buffer.from(null, "base64") in the
// caller used to just produce an empty buffer, not a thrown error.
// download_url is itself a raw.githubusercontent.com link with no size
// ceiling, so following it (unauthenticated - the file lives in this
// public repo) fixes the read regardless of how oversized the file gets
// between prune cycles.
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
  if (typeof body.content === "string" && body.encoding === "base64") {
    return Buffer.from(body.content, "base64").toString("utf8");
  }
  if (!body.download_url) {
    const error = new Error(
      "alerts_log.jsonl has no inline content and no download_url - unexpected GitHub API response shape"
    );
    error.status = 502;
    error.details = body;
    throw error;
  }
  const rawResponse = await fetch(body.download_url);
  if (!rawResponse.ok) {
    const error = new Error(`Failed to fetch alerts_log.jsonl raw content (HTTP ${rawResponse.status})`);
    error.status = 502;
    throw error;
  }
  return rawResponse.text();
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
    const text = await fetchHistoryFile();
    if (text === null) {
      return sendJson(res, 200, { history: [], skipped: 0 });
    }
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
