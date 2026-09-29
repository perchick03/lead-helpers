---
name: health-check
description: Full cold-email health check. Two modes. CAMPAIGN (`/health-check <campaign_id> [leads.csv]`) is the pre-launch gate for one campaign — lead-list MX hygiene, copy + merge variables off the live sequence, spam and reply-quality judgment on the copy, and the infrastructure lint (sending fleet, recipient MX, projected bounce, settings) — rolled into one GO / FIX FIRST / BLOCKED verdict. FLEET (`/health-check fleet`) is the daily per-domain inbox health pass — capacity, utilisation, bounce and reply per sending domain, blacklist. Trigger on `/health-check`, or "is this campaign ready to send", "check this list before I upload it", "run the deliverability checks", "how are my inboxes doing", "is any domain burning". Read-only — never sends, never edits the sequencer, never edits the lead CSV.
---

# Health Check

Every check is a script at the repo root. **Run the scripts; never count, grep or
eyeball the CSV or the copy yourself** — the scripts are the source of truth, you
relay and judge. What each check means, its thresholds and the fix for each red:
[docs/deliverability-playbook.md](../../../docs/deliverability-playbook.md). Read the
relevant section before explaining a finding.

The sequencer is reached only through `sequencer.py` (Instantly by default). If a
script fails with a key error, tell the operator to set `INSTANTLY_API_KEY` in `.env`.

Don't know the campaign id? List them:

```bash
python3 -c "from sequencer import get_sequencer; [print(c['id'], c['active'], c['name']) for c in get_sequencer().campaigns()]"
```

## Mode 1 — campaign (`/health-check <campaign_id> [leads.csv]`)

The CSV is the lead list about to be uploaded (needs an `email` column). No CSV →
run steps 2-4 without it, and say the lead-side checks skipped.

### Step 1 — lead list hygiene

```bash
python3 deliverability.py <leads.csv> --dry-run
```

Parks bad-format, no-MX, secure-gateway and disposable emails. `--dry-run` writes
nothing. If anything parks, give the operator the command that writes the clean
file (`python3 deliverability.py <leads.csv> --split`) — don't run it yourself.

### Step 2 — copy + merge variables

```bash
python3 copy_check.py <campaign_id> --csv <leads.csv> --samples 3
```

Exit 0 clean · 1 blockers · 2 warnings. Blockers: a merge tag with no CSV column,
a column blank on some rows, em-dashes, an empty body or first subject. Warnings:
spam words inside lead values, bad first names, ALL CAPS values, thin spintax,
lowercase sentence starts. **Read the rendered samples it prints** — that is the
check no regex performs.

### Step 3 — copy judgment (two agents, spawn both in ONE message)

Pass each the subject + body of every step (take them from step 2's samples):

- `spam-checker` — will it land in the inbox.
- `email-copy-check` — will anyone reply. Give it the audience if the operator named one.

### Step 4 — infrastructure (agent)

Spawn `campaign-infra-check` with the campaign id and the CSV path. It runs
`deliv_lint.py` + `inbox_health.py` and returns a short rollup. Run it in the same
message as step 3 — they're independent.

### Verdict

```
# Health check — <campaign name>

**<🔴 BLOCKED | 🟠 FIX FIRST | ✅ GO>**

| Layer | Result | Detail |
|---|---|---|
| leads      | ✓ / 🟠 | N parked of M (no-mx X · gateway Y · disposable Z) |
| copy + vars| ✓ / 🟠 / 🔴 | blockers / warnings from copy_check |
| spam       | ✓ / 🟡 / 🟠 / 🔴 | spam-checker verdict |
| reply      | ✓ / 🟡 / 🔴 | email-copy-check verdict + score |
| infra      | ✓ / 🟠 / 🔴 | deliv_lint verdict · projected bounce |
| inboxes    | ✓ / 🟡 / 🔴 | inbox health verdict · blacklist |

## Fix before launch
1. <each blocker, with the number and the one fix — from the playbook's fix menu>
```

- **BLOCKED** = any copy_check blocker, spam-checker BLOCK, email-copy-check REWRITE,
  deliv_lint DO NOT SEND, or a Spamhaus DBL listing.
- **FIX FIRST** = no blocker, but gateway/no-MX leads not yet parked, copy warnings,
  a REWRITE/REVIEW from spam-checker, or infra FIX FIRST.
- **GO** = everything else. Say which checks skipped — a skipped check is not a pass.

A green result is not a placement guarantee: auth, complaints and a seed placement
test are outside this kit (playbook §5).

## Mode 2 — fleet (`/health-check fleet`)

```bash
python3 inbox_health.py            # every live campaign's fleet
```

Relay the allocation line, then only what needs action (then run Mode 3): 🔴 BLACKLISTED, ⛔ RECOMMEND
RECOVERY, 🟡 WATCH. If the blacklist line says NOT checked, say so — it is not clean
(set `MX_TOOLBOX_API_KEY`). For a recovery recommendation, walk the operator through
playbook §7; detaching is a manual step in their sequencer.

## Mode 3 — trends (`/health-check trends`, and always after fleet mode)

`inbox_health.py` saves a snapshot to `history/` on every run (gitignored; the web UI's
"Download history CSV" is the same format, drop it in there too). Then:

```bash
python3 trends.py          # last 7 days vs the 7 before; exit 1 = a 🔴
```

Relay only what moved, with the numbers. Read the ids like this:

- `bounce-up` / `reply-down` / `volume-drop` — a group is drifting. Say which
  (fleet, an ESP, or one domain) and since when.
- `domain-outlier` — one domain is far worse than the rest of the fleet: that domain is
  the problem, not the list. Point to playbook §7 (recovery).
- `esp-gap` — one sender ESP delivers while the other doesn't (e.g. google fine,
  microsoft bouncing or silent). Suspect the recipient mix / auth on that ESP, not copy.
- "not enough history yet" — say so; a trend needs several sending days on record.
  Recipient-side ESP splits for one campaign live in `deliv_lint.py` (`bounce-esp-gap`).

## Rules

- **Read-only.** Never send, never change sequencer settings, never edit or rewrite
  the lead CSV. You report; the operator acts.
- **Keep the numbers** in every finding you relay.
- **Don't re-implement a check** or "double-check" a script by reading raw data.
