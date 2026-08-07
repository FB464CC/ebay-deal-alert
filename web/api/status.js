const crypto = require("crypto");

const githubHeaders = () => ({
  Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "ebay-deal-alert-settings"
});

const workflowRunsUrl = () => {
  const repo = process.env.GITHUB_REPO;
  return `https://api.github.com/repos/${repo}/actions/workflows/poll.yml/runs?per_page=1`;
};

const sendJson = (res, status, body) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
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
    const response = await fetch(workflowRunsUrl(), {
      method: "GET",
      headers: githubHeaders()
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      return sendJson(res, response.status, {
        error: body.message || "Failed to fetch workflow status",
        details: body
      });
    }
    const run = Array.isArray(body.workflow_runs) ? body.workflow_runs[0] : null;
    if (!run) {
      return sendJson(res, 200, {
        status: null,
        conclusion: null,
        created_at: null,
        updated_at: null
      });
    }
    return sendJson(res, 200, {
      status: run.status || null,
      conclusion: run.conclusion || null,
      created_at: run.created_at || null,
      updated_at: run.updated_at || null
    });
  } catch (error) {
    return sendJson(res, error.status || 500, { error: error.message, details: error.details });
  }
};
