// Stateless, read-only pass-through to Instantly API v2.
// Instantly sends no CORS headers, so the browser can't call it directly. The key
// arrives per request in `x-api-key`; it is never stored or logged.
const BASE = "https://api.instantly.ai/api/v2";
const ALLOWED = [ // read-only surface the checks use. POST /leads/list is a read.
  ["GET", /^\/campaigns$/], ["GET", /^\/campaigns\/[0-9a-f-]{36}$/], ["GET", /^\/campaigns\/analytics$/],
  ["GET", /^\/accounts$/], ["GET", /^\/accounts\/analytics\/daily$/], ["GET", /^\/custom-tag-mappings$/],
  ["POST", /^\/leads\/list$/],
];

module.exports = async (req, res) => {
  const fail = (code, error) => res.status(code).json({ error });
  if (req.method !== "POST") return fail(405, "POST only");
  const key = req.headers["x-api-key"];
  if (!key) return fail(401, "x-api-key header missing");
  const { method = "GET", path = "", params, body } = req.body || {};
  if (!ALLOWED.some(([m, rx]) => m === method && rx.test(path))) return fail(403, `not allowed: ${method} ${path}`);
  const qs = params ? "?" + new URLSearchParams(params) : "";
  try {
    const r = await fetch(BASE + path + qs, {
      method,
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", "User-Agent": "lead-helpers/1.0" },
      body: method === "POST" ? JSON.stringify(body || {}) : undefined,
    });
    res.status(r.status).setHeader("Content-Type", "application/json").send(await r.text());
  } catch (e) {
    fail(502, `upstream unreachable: ${e.message}`);
  }
};
