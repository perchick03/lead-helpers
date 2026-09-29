# Cold Email Deliverability Playbook

How to read the kit's checks, what each threshold means and why, and what to do when something goes red.
Sequencer-neutral; Instantly-specific facts are labelled. Numbers below match what the scripts do.

## Contents

1. [Principles](#1-principles) · 2. [When to run what](#2-when-to-run-what) · 3. [Pre-send checks](#3-pre-send-checks) (`deliverability.py`, `spam_lint.py`, `copy_check.py`, `deliv_lint.py`)
4. [Ongoing: `inbox_health.py`](#4-ongoing-inbox_healthpy) · 5. [What the kit does not check](#5-what-the-kit-does-not-check) (auth, complaints, placement) · 6. [Fix menu](#6-fix-menu)
7. [Recovery pool](#7-recovery-pool) · 8. [Blacklists](#8-blacklists) · 9. [Capacity and allocation](#9-capacity-and-allocation) · 10. [Lessons from real incidents](#10-lessons-from-real-incidents)

## 1. Principles

| # | Principle | Why |
|---|---|---|
| 1 | **The unit of failure is the domain, not the inbox.** | Providers score reputation per sending domain. One flagged domain takes every inbox on it. 400 inboxes on 4 domains = 4 bets, not 400. Health, recovery and statistics all roll up per domain. |
| 2 | **Reputation and engagement beat word choice.** | Filters score *who you are* (domain reputation, sender↔recipient ESP pair, complaints, engagement) far more than *what you wrote*. Fix infra before rewriting copy. |
| 3 | **Auth passing ≠ deliverable.** | SPF/DKIM/DMARC pass + clean blacklists + SpamAssassin 0 is fully compatible with zero replies. Auth is necessary, cheap to check, never sufficient. |
| 4 | **Accepted-then-filtered vs refused.** | Low bounce + silence = delivered to spam. `5.7.x policy blocked` = refused for who you are. `5.1.1 no such user` = bad list. Opposite causes, opposite fixes. |
| 5 | **Read leading signals first.** | Bounce is known at send time. Complaints show in 1-2 days. Reply rate lags ~21 days. Subtract the infra signals first; the residual is copy/list. |
| 6 | **Warmup score ≠ placement.** | Warmup networks are synthetic peers with pre-built relationships. 100% warmup placement is compatible with spam at real prospects. Use it as a gate (≥90), never as proof. |
| 7 | **Small samples lie.** | 1% of 200 sends is an expected count of 2 (95% CI ≈ 0.1-3.6%). That's why the rates below carry volume floors. |

**The ESP pair: know it, don't judge on it.** An early seed test suggested Microsoft→Google placement near 0%. Later real reply data from healthy Microsoft fleets sending to Gmail contradicted it, and a check built on that matrix kept blocking healthy campaigns, so it was removed. **Cross-ESP sending is not a defect by itself.** The kit reports your fleet's ESP mix and your list's MX mix as context; the one ESP effect it does model is bounce (young non-Microsoft domains getting `5.7.x` policy blocks at corporate M365, see §3.4).

## 2. When to run what

| When | Run | Blocks launch on |
|---|---|---|
| Before uploading a lead CSV | `deliverability.py` | parks bad rows (never blocks) |
| Before switching a sequence on | `spam_lint.py` · `copy_check.py` · `deliv_lint.py` | spam score 8+ · unresolved merge tag · projected bounce >5% |
| Every day or two while sending | `inbox_health.py` | a 🔴 domain → recovery pool |
| After the first ~week of sends | `deliv_lint.py` post-send | actual bounce >3% |
| Monthly, and on any reply-rate drop | your sequencer's seed placement test | <70% inbox |

After any fix: **don't re-audit inside 7 days.** Reputation trails remediation by 1-2 weeks; early reads are noise.

## 3. Pre-send checks

### 3.1 `deliverability.py` — lead list MX check

| Result | Action | Why |
|---|---|---|
| `bad-email-format` | park | cannot deliver |
| `no-mx` / null MX | park | certain hard bounce |
| `gateway` (Mimecast, Proofpoint, Barracuda, Cisco/IronPort…) | park | Gateways policy-block cold mail from young domains. The `5.7.x` hard bounces land on *your* domain's record. Some also check URI blacklists against your From domain. |
| `disposable` | park | no human behind it |
| `dns-error` | keep, re-run | a resolver failure is not a verdict |
| tag `microsoft` / `google` / `other` | keep | feeds ESP matching and the projected-bounce model |

Also, before sending: **verification is a hard gate.** Unverified list = don't send. Keep role accounts (info, sales, admin, hello, support…) ≤5%. A true catch-all is a *domain* property; only an SMTP verifier sees it.

### 3.2 `spam_lint.py` — copy lint

Scoring: HIGH hit = 3 pts · MED hit = 1 pt · hits in the subject count ×2.

| Score | 0 | 1-3 | 4-7 | 8+ |
|---|---|---|---|---|
| Verdict | ✓ clean | 🟡 review | 🟠 rewrite | 🔴 block |

- **Why tiered:** single-word lists false-positive on normal B2B words (*budget, target, quote*); phrases carry the signal. **Why subject ×2:** filters and humans read it first, and it is short. Rewrite only the flagged span.
- Email 1 structure: **0 links**, no images, no tracking pixel, no HTML styling, ≤1 `!`, no ALL CAPS, plain-text signature.
- **Silence must never stop the sequence.** "I'll take silence as a no" is a lie when step 3 still sends. Offer an action instead: "a one-word no is enough and I'll stop."

### 3.3 `copy_check.py` — live sequence vs CSV

Pulls the **live** sequence from the sequencer, so it lints what will actually send, not a draft.

| Check | Why |
|---|---|
| Merge tag with no CSV column | ships as a literal `{{tag}}` |
| Blank values | "Hi ," in front of a prospect |
| Bad first names | ALL-CAPS, titles ("Dr"), initials, company names in the name field |
| Em-dashes | read as AI-written |
| Spintax | brace leakage (`{a\|b}` shipping literally); every combination must be a grammatical sentence. Spin the scaffolding (greeting, CTA), never the payload. |
| Spam words inside lead values | a company name with "Free" or "Cash" injects a trigger word the copy lint never saw |
| Rendered samples | read 3-5 real renders; the eye catches what rules miss |

### 3.4 `deliv_lint.py` — campaign infra lint

**Pre-send:**

| Check | Fires at | Sev | Why |
|---|---|---|---|
| Recipient MX mix | report | — | context: who hosts your recipients; feeds the projected bounce |
| Gateway recipients | >2% | 🔴 | policy-block bounces; park them with `deliverability.py` |
| Projected bounce | >3% · >5% | 🔴 · ⛔ STOP | see formula below; over 5% do not launch |
| Dead / connection-error inboxes | any | 🟠 | failed sends, skewed rotation |
| Warmup score | <90 | 🟠 | not ready for cold volume |
| Inboxes per sending domain | >3 | 🟠 | blast radius |
| Open / link tracking | ON | 🟠 | pixel + rewritten links add HTML and a shared tracking domain |
| Allow risky contacts | ON | 🟠 | sends to catch-all / unverifiable addresses, the main bounce driver |
| Emails per inbox per day | >50 | 🟠 | above the per-inbox ceiling |
| Stop on reply | OFF | 🟡 | following up after a reply earns complaints |

**Projected bounce** (an estimate, not a measurement):

```
projected = no_mx%    × 1.00        # no MX = certain bounce
          + gateway%  × 0.40        # gateways policy-block cold mail
          + M365_share × non_MS_fleet_share × 4%   # non-Microsoft senders → M365: 5.7.x blocks
          + 1pp if any sending inbox has low warmup
```

Example: 0.5% no-MX, 3% gateway, 70% M365, all-Google fleet → 0.5 + 1.2 + 2.8 = **4.5% 🔴**. Park no-MX + gateway first → **≈2.9%**, under the line. Calibration: the M365 term reproduced a live 2.83% hard bounce on the campaign it was built from.
Spam-folder placement shows as silence, not bounce, so it is never in this number. Only a placement test sees it (§5).

**Inboxes per domain, nuanced.** 2-3 is a Google Workspace rule. Microsoft 365 tenant fleets run 50-100 inboxes/domain by design, so the flag stays on. Accept it knowingly: the cost is blast radius, not listing risk.

**Post-send:**

| Signal | Fires at | Read as |
|---|---|---|
| Actual bounce | >3% 🔴 · 2-3% 🟠 | see fix menu |
| Dead leads, **same ESP** as sender | — | list quality → verify the list |
| Dead leads, **cross-ESP** | — | policy / reputation → ESP matching, domain age |
| ≥2 dead leads on one recipient domain | — | catch-all or stale company → suppress the dead mailboxes; block the whole domain only if none of its leads delivered |

## 4. Ongoing: `inbox_health.py`

Per **domain**, 14-day window. Read-only; recovery is a manual action.

**Bounce** = bounced / sent.

| Bounce | Volume | Verdict |
|---|---|---|
| >5% | ≥150 sent **and** ≥5 bounces | 🔴 recommend recovery |
| >5% | under the floor | 🟡 watch: too small to act (3/32 = 9.4% is noise) |
| 2-5% | any | 🟡 watch |
| 1-2% | any | ✓ normal for cold |
| <1% | any | ✓ clean; clear to exit recovery |

**Reply** = unique replies / sent. Judged only past 150 sent.

| Reply | ≥2.6% | ≥1% | ≥0.8% | <0.8% |
|---|---|---|---|---|
| Verdict | ✓ great | 🟡 look into | 🟠 low | 🔴 worry |

Reading reply rates: ignore the last 2-3 days (replies lag sends). Compare a domain to its **siblings in the same campaign** (same list, copy, window): all low together → copy / list / ICP; one lagging → that domain. Never judge per inbox; at 3-5 sends/day it never builds a readable sample.

**Capacity** = sum of each inbox's daily limit. **Utilisation** = last send-day volume / **active** capacity (see §9).

**Blacklist** via the MXToolbox API (optional `MX_TOOLBOX_API_KEY`). Rules in §8. A failed lookup reads "not checked", never "clean". MXToolbox free plan: ~64 lookups/day, one per domain per run, so run it once a day, not in a loop.

## 5. What the kit does not check

| Layer | Check | Threshold / trap |
|---|---|---|
| DKIM | Google: `google._domainkey` TXT · M365: `selector1` + `selector2._domainkey` CNAME | Probe per provider. A published M365 CNAME does **not** mean signing is on; only a received header (`dkim=pass header.d=yourdomain`) proves it. |
| SPF | one `v=spf1` record, `~all` | Two SPF records = `permerror`. Max 10 DNS lookups (Google + Microsoft includes ≈ 7). Never `+all`. |
| DMARC | passes if **either** SPF or DKIM aligns | `spf=pass` + `dmarc=fail` = alignment; fix via DKIM. Ladder: `p=none` + `rua` (day 0-14) → `quarantine` (14d+) → `reject` (30d+). |
| Complaints | Google Postmaster Tools (Microsoft: SNDS) | Design for <0.10%. ≥0.30% = Google's hard limit. The earliest warning there is. Enrol every domain that sends to Gmail. |
| Placement | sequencer seed test (Instantly has one built in) | ≥90% ✓ · 80-90% 🟡 fix top trigger, don't scale · 70-80% 🟠 stop scaling · <70% 🔴 pause, incident |

Running a seed test that means something:
- **Equalize senders per domain** (≤3 inboxes from each). A domain fielding 100 senders floods the seeds and gets foldered for volume alone. One test per sender pool, never two at once, seeds weighted to your real recipient MX mix, 100-200 seeds per cell you act on. One test = one body; test follow-ups separately.
- **Never average across providers.** 92% M365 + 55% Gmail averages to a healthy-looking 74%. The divergence is the diagnosis: Gmail fails → engagement/content; M365 fails → domain reputation/auth; both fail → auth or blacklist.

## 6. Fix menu

| Finding | Do |
|---|---|
| Gateway recipients >2%, no-MX, disposable | Park them. Re-run `deliv_lint.py`. |
| Projected bounce >3% after parking | Send M365 recipients from Microsoft inboxes (ESP matching), or from older domains. Split the list by ESP if needed. |
| Projected or actual bounce >5% | ⛔ Don't launch / stop the campaign. |
| Placement test fails on one provider only | ESP matching is one option (Instantly: "match lead ESP"); it only helps if you own inboxes on both providers. A fleet/list ESP mismatch alone is not a finding. |
| Dead inbox · warmup <90 | Reconnect (fails again after a credential fix → retire). Low warmup: pull from campaigns, warm until ≥90. |
| >3 inboxes/domain | Workspace: spread over more domains. M365 tenant: accept, keep a bench (§9). |
| Tracking ON · allow-risky ON · stop-on-reply OFF · >50/inbox/day | Flip the setting (free fix). Lower the daily limit or add inboxes. |
| Spam score 4-7 / 8+ | Rewrite the flagged spans / block until rewritten. |
| Merge tag unresolved, blanks, bad names | Fix the CSV column or add a fallback; drop rows missing required values. |
| Actual bounce >3%, mostly `5.1.1` | List problem: export unsent leads, verify, keep only valid. |
| Actual bounce >3%, mostly soft / `5.7.x` | Reputation problem: cut volume 50% per inbox for 2 weeks, keep warming, then ramp (below). |
| Domain 🔴 in `inbox_health.py` | Recovery pool (§7). |
| Reply low on every domain equally | Copy / list / ICP. Not infra. |
| Reply low + bounce <1% | Accepted then filtered: run a placement test. |
| Spamhaus DBL listing | Stop sending on that domain. Age rule (§8). |
| Complaints ≥0.30% | Pause. Rework targeting and copy. |

**Ramp back after a volume cut** (only after a passing re-test at D+14): D14 50% → D15-18 65% → D19-22 80% → D23+ 100%, watching daily for 7 days. Drop back to 50% if bounce, placement or complaints regress at any step.

## 7. Recovery pool

A domain past the bounce line is **detached from campaign sending and parked in warmup.** Warmup stays **ON.**

```
active ──(🔴: >5%, ≥150 sent, ≥5 bounces)──▶ recovery ──(warmup healthy ~14d)──▶ active
        detach from every campaign             warming only       re-attach at reduced volume
```

1. **Type the bounces first.** Mostly `5.1.1` across *all* the campaign's domains = the list, not this domain. Fix the list.
2. **Detach** the domain's inboxes from every live campaign in your sequencer. Keep warmup on.
3. **Record it** wherever you track the fleet: state `recovery`, date, trigger (e.g. "7.2%, 12/166 over 14d"). Only an operator moves a domain in or out; automation that clears the state loses the reason and the clock.
4. **Backfill capacity** from a warmed spare domain, if you have one.
5. **Wait.** No reads for 7 days.
6. **Exit is warmup-based.** A parked domain sends no cold mail, so a 0-sent window proves nothing. Exit when warmup score is ≥90 and steady for ~14 days. Re-attach at reduced volume and confirm the 14-day bounce settles <1%.
7. **No recovery after ~4 weeks** → retire the domain and replace it (age rule, §8).

## 8. Blacklists

| List | Reads | Who consults it | Action |
|---|---|---|---|
| **Spamhaus DBL** | sending domain + body links | Gmail, Microsoft, most of the industry | 🔴 **Stop sending on that domain.** Age rule below. |
| **SURBL** | mainly links in the body | content filters, some corporate gateways; **not Google or Microsoft** | Ignored by the tool. A zero-link email never triggers the lookup. Before acting, compare the domain to a clean sibling in the same campaign. |
| ivmURI, SEM FRESH, SORBS, others | links, domain age, domain | varies, mostly minor | 🟡 Look, no auto-action. SEM FRESH is age-only and expires by itself. |
| Sending **IP** on a DNSBL | IP | — | Ignore on Google/Microsoft mailboxes: shared outbound pools, not yours. |
| Lookup failed / timed out | — | — | **Not checked.** Never read it as clean. |

**Age rule** (replace vs repair a listed or burned domain):

| Domain age | Do |
|---|---|
| <30 days | **Replace.** Cleanup isn't worth it. |
| 30-90 days | Repair once. No recovery in 2 weeks → replace. |
| >90 days | Stop sending 7 days → submit delisting → resume slowly. |

Replace = retire the old domain (never reuse), buy a new one, **warm 2 weeks** before it carries traffic.

- **Several domains listed at once** = your content, list, or domain-buying pattern. Swapping domains re-runs the same experiment.
- **Delist as housekeeping, never as the fix.** Spamhaus: self-service at check.spamhaus.org. SURBL: removal form after the lookup; say what you changed.
- **Never host a linked asset on a sending domain.** One link in a reply turns an inert URI listing live.

**Before buying a domain:** DBL hit = don't buy. Check WHOIS + archive.org for a prior life. Spread across registrars. Don't register a same-day lookalike block on the same nameservers and redirect target. Age 14 days minimum (30 preferred) before the first cold send.

## 9. Capacity and allocation

| State | In a live campaign? | Capacity counts as |
|---|:-:|---|
| active | yes | **live**: what can send today |
| warmup | no | headroom: new or parked |
| recovery | no | headroom: parked after a bounce breach |

`inbox_health.py` sees only attachment, so it shows warmup and recovery together as **parked**. Which parked domains are recovering (and since when) lives in your own tracking sheet.

- **Utilisation uses active capacity only** (parked capacity can't send; counting it flatters the number) and the **last send day**, not "yesterday", or every Monday reads 0%.
- **Utilisation >80%** → capacity is the constraint. Promote a warmed domain.
- **Warmup capacity idle for weeks** → paid-for infra doing nothing. Wire it in or stop paying.
- **Keep a bench.** A new domain takes ~2 weeks to become sendable. One coach's rule: for every 10 Microsoft domains, warm 5 spares. Buy more when the bench drops below one domain's worth.
- **Your sequencer is the truth, your sheet is a label.** A domain's real state is whether its inboxes are attached to a live campaign. Reconcile often.

Volume reference: ≤50/inbox/day (the lint's line). Workspace: 2-3 inboxes/domain. M365 tenant: 200-500/day/domain at 3-5/inbox.

## 10. Lessons from real incidents

| What happened | Lesson |
|---|---|
| A ~1,500-email campaign got 3 human replies. Auth passed, SpamAssassin 0, every domain clean on every blacklist, bounce 0.55%. Long vs short copy: 76% vs 79% spam. It was first blamed on the fleet/list ESP pair; later data from healthy cross-ESP fleets contradicted that. | Clean auth + clean blacklists prove nothing, and copy was not the lever. Don't turn one incident into a rule: a check built on the first theory kept blocking healthy campaigns. |
| Switching it to fresh Google senders got the first replies, then ~10% hard-bounced `5.7.x policy blocked` at corporate M365. | Young domains get refused at corporate perimeters. Some audiences (behind M365 + gateways) are structurally hostile to cold email. |
| A placement test sent from 1,015 inboxes into 40 seeds at once. 100-inbox domains scored ~0% Gmail inbox, 3-inbox domains 100%. Real reply data showed the opposite. | The test measured its own sending pattern. Equalize senders per domain; check cheap real data before trusting the instrument; read cell by cell, not the headline. |
| ~40% of one fleet's domains showed up on SURBL, including one that had never sent. Twin domains with identical volume: one listed, one clean. Placement stayed 92-95%, bounce <1%. A day went into diagnosing it; the fix that needed no diagnosis was a warm spare domain, and there wasn't one. | Ask *which list, who reads it, what it reads*. Listings came from how the domains were bought (same day, same registrar, same nameservers), not from sending. A bench beats a diagnosis. |
| A daily Spamhaus DBL check ran through a public resolver for over a week. Spamhaus refuses those, so it silently checked nothing and reported "clean". | A failed lookup is "not checked". Canary-test the check, or use a service that queries from authorized infra. |
| The "2-3 inboxes per domain" theory was blamed for listings. Data: 1-3/domain were listed *more* often than 50/domain. | Match the density rule to the provider model. The M365 trade is blast radius, not listing risk. |
| A 300/day warmed domain sat idle for 10 days unnoticed. | Report warmup capacity every run. Idle headroom is money burning. |
| A fleet inventory from an unpaginated API call missed two live campaigns and five domains. | Paginate everything before trusting a count. |
