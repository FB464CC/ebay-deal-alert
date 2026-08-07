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
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
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
  const envError = requireEnv();
  if (envError) {
    return sendJson(res, 500, { error: envError });
  }

  if (req.headers["x-settings-password"] !== process.env.SETTINGS_PASSWORD) {
    return sendJson(res, 401, { error: "Unauthorized" });
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
      if (!response.ok) {
        return sendJson(res, response.status, {
          error: body.message || "GitHub update failed",
          details: body
        });
      }
      return sendJson(res, 200, { ok: true });
    } catch (error) {
      return sendJson(res, error.status || 500, { error: error.message, details: error.details });
    }
  }

  res.setHeader("Allow", "GET, POST");
  return sendJson(res, 405, { error: "Method not allowed" });
};
