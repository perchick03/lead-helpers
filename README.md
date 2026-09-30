# lead-helpers — cold email health check

Pre-launch and ongoing health checks for cold email: the **lead list**, the **copy**,
the **sending infrastructure**, and **inbox health** per sending domain.

- Python 3.9+, **standard library only**. There's nothing to `pip install`.
- Everything is **read-only**: nothing sends, and nothing changes your sequencer or your files.
- Works with **Instantly** out of the box. Any other sequencer works once you
  implement one small file ([below](#using-another-sequencer)).

What each check means, its thresholds, and what to do when one goes red:
**[docs/deliverability-playbook.md](docs/deliverability-playbook.md)**.

## What's inside

| File | Checks | Needs |
|---|---|---|
| `deliverability.py` | Lead CSV: bad format, no MX, secure gateways (Mimecast, Proofpoint, …), disposable domains. Tags each recipient microsoft / google / other | `dig` |
| `spam_lint.py` | Copy: tiered spam words, formatting, subject shape. Can also lint a CSV column | nothing |
| `copy_check.py` | Live sequence vs lead CSV: merge tags resolve, no blank values, sane first names, no em-dashes, **no spam words inside lead values** (e.g. "Acme Discount Tours"), spintax. Prints rendered samples | sequencer key |
| `deliv_lint.py` | Campaign infra: recipient MX mix, gateway share, **projected bounce**, dead / under-warmed inboxes, inboxes per domain, tracking and risky-send settings. After sending: actual bounce, dead leads by ESP, dead-domain clusters | sequencer key, `dig` |
| `inbox_health.py` | Per **sending domain**: capacity, utilisation, bounce and reply over 14 days, recovery recommendations, Spamhaus blacklist | sequencer key, optional MXToolbox key |
| `trends.py` | **History**: reads `history/*.csv` (saved by every `inbox_health` run, or downloaded from the web UI) and flags bounce rising, replies falling, one domain worse than the fleet, Google delivering while Microsoft isn't | nothing |
| `sequencer.py` | The **only** file that talks to your sequencer (Instantly by default) | — |
| `.claude/` | Claude Code skill `/health-check` and three agents: `spam-checker`, `email-copy-check`, `campaign-infra-check` | Claude Code |
| `index.html` + `js/` + `api/` | MXSWEEP: all of the above as a browser console with tabs ([below](#mxsweep-web-ui)) | a browser |

## Setup

```bash
git clone https://github.com/perchick03/lead-helpers.git && cd lead-helpers
cp .env.example .env        # then fill in the keys
```

| Key | Needed for | Where to get it |
|---|---|---|
| `INSTANTLY_API_KEY` | `copy_check`, `deliv_lint`, `inbox_health` | Instantly → Settings → Integrations → API keys (v2) |
| `MX_TOOLBOX_API_KEY` | blacklist check in `inbox_health` (optional) | mxtoolbox.com → API (free plan: 64 lookups/day) |

`.env` is gitignored. Never commit it.

`dig` ships with macOS. On Debian/Ubuntu, install it with `apt-get install dnsutils`.

Find your campaign ids:

```bash
python3 -c "from sequencer import get_sequencer; [print(c['id'], c['active'], c['name']) for c in get_sequencer().campaigns()]"
```

## Run it with Claude Code (recommended)

Open Claude Code in this folder:

```
/health-check <campaign_id> leads.csv     # full pre-launch gate → GO / FIX FIRST / BLOCKED
/health-check fleet                       # daily inbox health across live campaigns
```

The campaign mode runs the lead check, then `copy_check`, then three agents in parallel.
`spam-checker` asks "will it land in the inbox?" `email-copy-check` asks "will anyone reply?"
`campaign-infra-check` runs `deliv_lint` and `inbox_health`. The results come back as one verdict table.
You can also call the agents on their own, e.g. "spam-check this subject and body".

## Run the scripts directly

```bash
python3 deliverability.py leads.csv --split                 # writes leads_checked / _clean / _parked
python3 spam_lint.py --subject "quick q" --body "$(cat email.txt)"
python3 spam_lint.py --csv leads.csv --col company_name     # spam words in a lead column
python3 copy_check.py <campaign_id> --csv leads.csv         # exit 0 clean · 1 blockers · 2 warnings
python3 deliv_lint.py <campaign_id> --leads leads.csv       # exit 1 = DO NOT SEND
python3 inbox_health.py                                     # or --campaign <id>, --window 14, --json
```

Every script has `--help`. Selftests run offline:

```bash
python3 spam_lint.py --selfcheck && python3 copy_check.py --selftest && python3 deliv_lint.py --selftest \
  && python3 inbox_health.py --selftest && python3 trends.py --selftest && python3 test_deliverability.py \
  && node test_checks.js
```

## Using another sequencer

Every check reads your sequencer through [`sequencer.py`](sequencer.py) and nowhere else.
To use Smartlead, EmailBison, Lemlist or anything else:

1. Subclass `Sequencer` in `sequencer.py` and implement its six read methods. Return plain
   dicts with exactly the keys listed in each docstring:

   | Method | Returns | Used by |
   |---|---|---|
   | `campaigns()` | id, name, active | finding ids, `inbox_health` |
   | `campaign(id)` | name, attached sender inboxes, steps → variants (subject, body), settings (tracking, allow-risky, stop-on-reply, daily limit) | `copy_check`, `deliv_lint`, `inbox_health` |
   | `accounts()` | every inbox: status, warmup score, daily limit, provider (google / microsoft / other) | `deliv_lint`, `inbox_health` |
   | `campaign_stats(id)` | sent, contacted, bounced | `deliv_lint` (post-send) |
   | `campaign_leads(id)` | email, bounced, recipient provider | `deliv_lint` (post-send) |
   | `inbox_daily_stats(emails, start, end)` | per inbox per day: sent, bounced, replies | `inbox_health` |

2. **Convert the copy syntax.** The checks read merge tags as `{{name}}` and spintax as
   `{{RANDOM|a|b}}` (Instantly's syntax). If your tool writes `{FIRST_NAME}` or `{a|b}`,
   convert inside `campaign()`. List the tags your tool fills itself (first name, signature, …)
   in `system_tags`, so `copy_check` doesn't look for them in the CSV.
3. Point `get_sequencer()` at your class and read your key from `.env`.

If your tool doesn't expose something (e.g. a warmup score), return `None` or `""` as the
docstring says and that check skips. The `Instantly` class at the bottom of the file is a
complete worked example (~100 lines).

## MXSWEEP web UI

`index.html` is a four-tab console. Every tab ends with a **download CSV** of that run.

| Tab | Input | Where it runs |
|---|---|---|
| List health | lead CSV; optional sending domains + MXToolbox key for a blacklist check | MX in the browser (DNS-over-HTTPS), lead data never leaves the tab. Blacklist via `api/mxtoolbox.js` |
| Spam & copy check | pasted subject/body, or a campaign id via API key; optional lead CSV | browser |
| Inbox health | API key | browser + proxy. Every run is saved in the browser, **trends** compare this week with last |
| Campaign infra | API key + campaign; optional lead CSV | browser + proxy |

Instantly and MXToolbox send no CORS headers, so their calls go through `api/instantly.js` and `api/mxtoolbox.js`, stateless
Vercel functions. It is read-only (allowlisted GET/`leads/list` paths), takes the key per request in a header,
and stores and logs nothing. The key stays in the tab's `sessionStorage`.

The JS in `js/checks.js` is a port of the Python checks; the spam word lists are generated from
`spam_lint.py` (`python3 spam_lint.py --export-words > spam_words.json`, checked by `test_deliverability.py`).
Thresholds live in both places, so change both. `node test_checks.js` uses the same fixtures as the Python selftests.

History CSV (`date,email,domain,esp,sent,bounced,replies`) is shared: the CLI writes it to `history/`, the UI
downloads and imports it, `python3 trends.py` and `/health-check trends` read it.

Run it locally with the proxy: `vercel dev` (a plain `python3 -m http.server` serves the List health tab only).

### List health tab details


The List health tab runs the `deliverability.py` lead check in the browser. Drop in a CSV, watch
the domains resolve live, and download the clean list. MX lookups run client-side over
DNS-over-HTTPS, so lead data never leaves the tab. It's a static page, so it deploys to
Vercel as-is. To run it locally:

```bash
python3 -m http.server 8000   # then open http://localhost:8000
```

By default the page runs the MX check only. Tick the boxes to add the format and domain checks.
The same three columns are appended in the browser and in the CLI:

- `park_reason` — `""` if the row passes, otherwise `bad-email-format` \| `no-mx` \| `gateway` \| `disposable`
- `mx` — provider for rows that pass: `microsoft` \| `google` \| `other` \| `dns-error`. `dns-error` means
  the resolver never answered. Those rows are **kept, not parked**. Re-run to settle them.
- `domain_mismatch` — `yes` if the email domain doesn't match `company_domain`. This is a flag only and never parks a row.

**Gateway list:** `mimecast, pphosted, ppe-hosted, proofpoint, barracuda, iphmx, ironport,
cisco, messagelabs, symanteccloud, fortimail, securence, mxthunder`. These are substring-matched
against MX hosts. It's a hand-maintained list, so it lags reality. Add new gateways to `_GATEWAY` in
`deliverability.py` as you find them.
