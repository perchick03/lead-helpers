# MXSWEEP — Free Deliverability Checks

Clean a lead CSV of dead domains and secure email gateways before you send.
Two ways to run it; **same logic** ported from our production lead pipeline:

- **`index.html`** — a browser dashboard. Drop a CSV, watch domains resolve live,
  download the clean list. MX lookups run client-side over DNS-over-HTTPS, so the
  lead data never leaves the tab. Static — deploy to Vercel as-is.
- **`deliverability.py`** — the CLI, for batch/offline use (uses local `dig`).

The three FREE ($0, no API key) checks:

| check | what it does | action |
|---|---|---|
| **bad-email-format** | non-empty email fails a basic format regex | park |
| **mx** | resolve each unique recipient domain's MX; tag provider; detect Secure Email Gateways (Mimecast, Proofpoint incl. Essentials, Barracuda, MXThunder, …) and no-MX / RFC-7505 null-MX | park gateway + no-mx; tag `microsoft\|google\|other` |
| **domain-check** | disposable mailbox domains; email↔company-domain mismatch | park disposable; **flag** mismatch (non-blocking) |

Paid stages (email verification, crawl, research) are **not** included.
By default the web UI runs **mx only**; tick the boxes to add format / domain checks.

## Web UI

```bash
# locally: any static server works, e.g.
python3 -m http.server 8000   # then open http://localhost:8000
```

**Deploy to Vercel:** push this folder to GitHub → import the repo in Vercel →
no build step, no config (it's a static site). The MX check needs no server: it
resolves over Google + Cloudflare DNS-over-HTTPS straight from the browser.

> Note vs the CLI: the browser resolves MX over DoH instead of `dig`. Same MX
> records, same classifier — verdicts match. `park_reason` / `mx` /
> `domain_mismatch` columns are identical. The only browser dependency is
> PapaParse (loaded from a CDN) for robust CSV parsing.

## Requirements

- **Python 3.9+** — standard library only, nothing to `pip install`.
- **`dig`** — DNS lookup tool, used by the MX check.
  - macOS: preinstalled.
  - Debian/Ubuntu: `apt-get install dnsutils`.
  - No `dig`? Pass `--no-mx` to skip the MX check (format + disposable + mismatch still run).

## Usage

```bash
python deliverability.py leads.csv                       # writes leads_checked.csv
python deliverability.py leads.csv -o out.csv --split    # + out's dir gets _clean.csv / _parked.csv
python deliverability.py leads.csv --dry-run             # summary only, write nothing
python deliverability.py leads.csv --no-mx               # skip DNS (no dig needed)
python deliverability.py leads.csv --email-col Email --domain-col Website
```

The input needs an **email column** (default `email`). A company-domain column
(default `company_domain`) is optional — without it the mismatch flag is skipped.

## Output

Three columns appended to every row:

- `park_reason` — `""` if it passes, else `bad-email-format` \| `no-mx` \| `gateway` \| `disposable`
- `mx` — provider for survivors: `microsoft` \| `google` \| `other` (`""` if parked/unchecked)
- `domain_mismatch` — `yes` if the email domain doesn't match `company_domain` (does **not** park)

`--split` additionally writes `<input>_clean.csv` (passes) and `<input>_parked.csv`.

## Gateway list

`mimecast, pphosted, ppe-hosted, proofpoint, barracuda, iphmx, ironport, cisco,
messagelabs, symanteccloud, fortimail, securence, mxthunder` — substring-matched
against MX hosts. It's a hand-list, so it lags reality; add new gateway substrings
to `_GATEWAY` in the script as you find them.
