// Stateless pass-through for MXToolbox's blacklist lookup (their API sends no CORS headers either).
// Key arrives per request in `x-api-key`; never stored or logged.
module.exports = async (req, res) => {
  const fail = (code, error) => res.status(code).json({ error });
  if (req.method !== "POST") return fail(405, "POST only");
  const key = req.headers["x-api-key"];
  if (!key) return fail(401, "x-api-key header missing");
  const domain = String((req.body || {}).domain || "").toLowerCase();
  if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(domain)) return fail(400, "invalid domain");
  try {
    const r = await fetch("https://api.mxtoolbox.com/api/v1/lookup/blacklist/" + domain, { headers: { Authorization: key } }); // plain key, no Bearer
    res.status(r.status).setHeader("Content-Type", "application/json").send(await r.text());
  } catch (e) {
    fail(502, `upstream unreachable: ${e.message}`);
  }
};
