#!/usr/bin/env python3
"""Pre-send INFRASTRUCTURE deliverability lint for one campaign.

The layer a content linter and SpamAssassin never check: can this campaign's
actual sending fleet reach this lead list? Reads the LIVE campaign through
`sequencer.py` (Instantly by default) and `dig`s the recipients' MX.

Checks (all read-only; never sends, never edits your sequencer):
  Recipients   target-mx-mix          recipient mail hosts, lead-weighted (dig MX)
               gateway-recipients     leads behind Mimecast/Proofpoint/... (>2% = red)
               projected-bounce-rate  pre-send estimate (>3% red, >5% STOP)
  Fleet        dead-inboxes           inboxes in connection/send error
               low-warmup-score       active inbox warmup score < 90
               inboxes-per-domain     > 3 inboxes on one sending domain
  Settings     tracking-off           open/link tracking should be OFF for cold
               list-hygiene-flags     allow-risky ON · >50/inbox/day · stop-on-reply OFF
  After send   live-bounce-rate       ACTUAL bounced/contacted (>3% red, 2-3% orange)
               bounce-esp-breakdown   dead leads by recipient ESP: same-ESP = list quality
               bounce-domain-clusters >=2 dead leads on one domain = catch-all/stale

    python3 deliv_lint.py <campaign_id> [--leads leads.csv] [--domains a.com,b.com]
                          [--max-domains N] [--json]
    python3 deliv_lint.py --selftest

--domains overrides the fleet (for a draft with no senders attached yet).
Without --leads the recipient checks skip. Needs `dig` for the MX sweep.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from deliverability import classify_mx

# Free-mail domains: never dig them. A big sweep can time out and misread
# gmail/hotmail as no-mx, which would strip good leads. Their host is fixed anyway.
FREE_PROVIDER = {
    "gmail.com": "google", "googlemail.com": "google",
    "hotmail.com": "microsoft", "hotmail.co.uk": "microsoft", "outlook.com": "microsoft",
    "outlook.co.uk": "microsoft", "live.com": "microsoft", "msn.com": "microsoft",
    "yahoo.com": "other", "yahoo.co.uk": "other", "aol.com": "other", "icloud.com": "other",
    "me.com": "other", "proton.me": "other", "protonmail.com": "other",
}

# Thresholds.
BOUNCE_RED = 0.03  # bounce > 3% -> RED (projected or actual)
BOUNCE_WARN = 0.02  # actual bounce 2-3% -> orange
BOUNCE_STOP = 0.05  # > 5% -> STOP
GATEWAY_RED = 0.02  # > 2% of leads behind a secure gateway
WARMUP_MIN = 90  # active inbox below this -> orange
MAX_INBOXES_PER_DOMAIN = 3
MAX_PER_INBOX_DAY = 50  # cold-send per-inbox daily ceiling

# Projected-bounce coefficients (calibrated on one real campaign; tune as you learn).
G_BLOCK = 0.40  # share of gateway-fronted recipients that hard-bounce cold
M_POLICY = 0.04  # non-Microsoft sender -> Microsoft 365 recipient policy-block rate
UNWARMED_UPLIFT = 0.01  # +1pp if any active inbox is under-warmed

MX_WORKERS = 12  # ~25 parallel digs is what gets a resolver to rate-limit you
BUCKETS = ("microsoft", "google", "gateway", "other", "no-mx")


def classify(domain: str) -> str:
    return FREE_PROVIDER.get(domain) or classify_mx(domain)


# ───────────────────────────────────────────────────────── recipient MX


def recipient_domains(csv_path: Path, cap: int) -> tuple[Counter, int]:
    counts: Counter = Counter()
    total = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            email = (row.get("email") or "").strip().lower()
            if "@" in email:
                total += 1
                counts[email.split("@", 1)[1]] += 1
    if cap and len(counts) > cap:
        counts = Counter(dict(counts.most_common(cap)))  # heaviest-lead domains
    return counts, total


def mx_profile(counts: Counter, classify_fn=classify) -> dict[str, Any]:
    domains = list(counts)
    with ThreadPoolExecutor(max_workers=max(1, min(MX_WORKERS, len(domains) or 1))) as ex:
        klass = dict(zip(domains, ex.map(classify_fn, domains)))
    leads_by_bucket: Counter = Counter()
    for dom, n in counts.items():
        leads_by_bucket[klass[dom]] += n
    # dns-error = "we don't know", not "dead": keep it out of the denominator so a
    # flaky resolver can't inflate no-mx and fake a bounce projection.
    unresolved = leads_by_bucket.get("dns-error", 0)
    total = (sum(counts.values()) - unresolved) or 1
    return {
        "share": {b: leads_by_bucket[b] / total for b in BUCKETS},
        "leads_by_bucket": dict(leads_by_bucket),
        "unresolved": unresolved,
        "unresolved_domains": sorted(d for d in domains if klass[d] == "dns-error"),
    }


# ──────────────────────────────────────────────────────────────── math


def fleet_esp_share(fleet: list[dict]) -> dict[str, float]:
    esp = Counter(a["esp"] for a in fleet)
    n = sum(esp.values()) or 1
    return {k: v / n for k, v in esp.items()}


def projected_bounce(target: dict[str, float], fleet: dict[str, float], any_low_warmup: bool) -> dict[str, float]:
    fleet_non_ms = sum(s for esp, s in fleet.items() if esp != "microsoft")
    comp = {
        "no-mx": target.get("no-mx", 0.0),
        "gateway": target.get("gateway", 0.0) * G_BLOCK,
        "cross-to-m365": target.get("microsoft", 0.0) * fleet_non_ms * M_POLICY,
        "unwarmed": UNWARMED_UPLIFT if any_low_warmup else 0.0,
    }
    comp["total"] = sum(comp.values())
    return comp


def bounce_esp_diagnosis(esp_counts: Counter, fleet_esp: str | None) -> str:
    """Same-ESP vs cross-ESP verdict for the dead leads."""
    total = sum(esp_counts.values())
    if not total:
        return "no dead leads to classify"
    mix = ", ".join(f"{n} {esp or 'unknown'}" for esp, n in esp_counts.most_common())
    if not fleet_esp or "" in esp_counts:
        return mix
    if esp_counts.get(fleet_esp, 0) / total >= 0.6:
        return (f"{mix} → mostly same-ESP ({fleet_esp}): the fleet reaches this host, so these are "
                f"dead mailboxes / list quality, not a reach problem. Verify the list harder")
    return f"{mix} → mostly cross-ESP (not {fleet_esp})"


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def low_warmup(fleet: list[dict]) -> list[tuple[str, float]]:
    return [(a["email"], a["warmup_score"]) for a in fleet
            if a["status"] == "active" and isinstance(a.get("warmup_score"), (int, float))
            and a["warmup_score"] < WARMUP_MIN]


# ────────────────────────────────────────────────────────────── checks


def lint(camp: dict, fleet: list[dict], target: dict | None, stats: dict | None,
         leads: list[dict] | None) -> list[dict]:
    """All findings from already-fetched data. Pure: no network."""
    findings: list[dict] = []

    def add(id_: str, sev: str, msg: str) -> None:
        findings.append({"id": id_, "sev": sev, "msg": msg})

    fleet_share = fleet_esp_share(fleet) if fleet else {}
    tshare = target["share"] if target else {}

    if target:
        mix = "  ".join(f"{b}={pct(tshare[b])}" for b in BUCKETS)
        if target["unresolved"]:
            mix += f"  (+{target['unresolved']} leads unresolved, excluded)"
            add("dns-unresolved", "yellow",
                f"{target['unresolved']} leads on {len(target['unresolved_domains'])} domains never resolved "
                f"— NOT counted as no-mx. Re-run to settle them (e.g. {', '.join(target['unresolved_domains'][:3])})")
        add("target-mx-mix", "info", mix)
        gw = target["leads_by_bucket"].get("gateway", 0)
        add("gateway-recipients", "red" if tshare["gateway"] > GATEWAY_RED else "pass",
            f"{gw} leads ({pct(tshare['gateway'])}) behind a secure gateway — strip or segment them "
            f"(run deliverability.py --split)" if gw else "no gateway-fronted recipients")
    else:
        add("target-mx-mix", "skip", "no lead CSV (pass --leads)")

    if fleet and tshare:
        pb = projected_bounce(tshare, fleet_share, bool(low_warmup(fleet)))
        stop = " — STOP, do not send" if pb["total"] > BOUNCE_STOP else ""
        add("projected-bounce-rate", "red" if pb["total"] > BOUNCE_RED else "pass",
            f"≈ {pct(pb['total'])}{stop}  [no-mx {pct(pb['no-mx'])} + gateway {pct(pb['gateway'])} + "
            f"cross→M365 {pct(pb['cross-to-m365'])} + unwarmed {pct(pb['unwarmed'])}] (estimate)")
    else:
        add("projected-bounce-rate", "skip", "needs both a resolved fleet and a lead CSV")

    # after send: real numbers
    if stats and stats.get("sent"):
        bounced, contacted = stats.get("bounced", 0), stats.get("contacted", 0)
        rate = bounced / contacted if contacted else 0.0
        add("live-bounce-rate", "red" if rate > BOUNCE_RED else "orange" if rate > BOUNCE_WARN else "pass",
            f"≈ {pct(rate)} ACTUAL ({bounced} bounced / {contacted} contacted)")
        dead = [le for le in leads or [] if le["bounced"]]
        fleet_esp = max(fleet_share, key=lambda k: fleet_share[k]) if fleet_share else None
        add("bounce-esp-breakdown", "info",
            f"{len(dead)} dead leads — {bounce_esp_diagnosis(Counter(le['esp'] for le in dead), fleet_esp)}")
        pool = leads or []
        by_esp = {e: (sum(le["bounced"] for le in pool if le["esp"] == e), sum(le["esp"] == e for le in pool))
                  for e in ("google", "microsoft")}
        r = {e: b / n for e, (b, n) in by_esp.items() if n >= 100}  # under 100 leads a rate is noise
        if len(r) == 2:
            hi, lo = sorted(r, key=r.get, reverse=True)
            gap = r[hi] >= BOUNCE_RED and r[hi] >= 2 * max(r[lo], 0.005)
            add("bounce-esp-gap", "orange" if gap else "info",
                f"bounce by recipient ESP: {hi} {pct(r[hi])} vs {lo} {pct(r[lo])}"
                + (f" — the fleet delivers to {lo} but not {hi}" if gap else ""))
        clusters = {d: n for d, n in Counter(le["email"].rsplit("@", 1)[-1].lower() for le in dead).items() if n >= 2}
        if clusters:
            add("bounce-domain-clusters", "yellow",
                f"{dict(list(clusters.items())[:5])} — several dead on one domain = likely catch-all/stale. "
                f"Suppress the dead mailboxes; only block the whole domain if none of its leads delivered")

    if fleet:
        dead_inboxes = [a["email"] for a in fleet if a["status"] == "error"]
        add("dead-inboxes", "orange" if dead_inboxes else "pass",
            f"{len(dead_inboxes)} inboxes in connection/send error: {dead_inboxes[:5]}" if dead_inboxes
            else "no dead inboxes in the fleet")
        low = low_warmup(fleet)
        add("low-warmup-score", "orange" if low else "pass",
            f"{len(low)} active inboxes warmup<{WARMUP_MIN}: {[f'{e}={s}' for e, s in low[:5]]}" if low
            else f"all active inboxes warmup≥{WARMUP_MIN} (or no score reported)")
        per_dom = Counter(a["email"].split("@", 1)[-1] for a in fleet)
        over = {d: n for d, n in per_dom.items() if n > MAX_INBOXES_PER_DOMAIN}
        add("inboxes-per-domain", "orange" if over else "pass",
            f"{len(over)} domains over {MAX_INBOXES_PER_DOMAIN} inboxes: {dict(list(over.items())[:5])}" if over
            else f"all domains ≤{MAX_INBOXES_PER_DOMAIN} inboxes")

    s = camp.get("settings") or {}
    if s:
        track = [k for k in ("open", "link") if s.get(f"{k}_tracking")]
        add("tracking-off", "orange" if track else "pass",
            f"{'+'.join(track)} tracking ON (adds a pixel / rewrites links — turn OFF for cold)" if track
            else "open+link tracking OFF")
        hyg = []
        if s.get("allow_risky"):
            hyg.append("🟠 allow-risky ON (sends to unverified leads → bounces)")
        dl = s.get("daily_limit")
        if isinstance(dl, int) and fleet and dl / len(fleet) > MAX_PER_INBOX_DAY:
            hyg.append(f"🟠 daily limit {dl} over {len(fleet)} inboxes = {dl // len(fleet)}/inbox/day "
                       f"(>{MAX_PER_INBOX_DAY})")
        if not s.get("stop_on_reply"):
            hyg.append("🟡 stop-on-reply OFF")
        add("list-hygiene-flags", "orange" if any("🟠" in h for h in hyg) else "yellow" if hyg else "pass",
            " · ".join(hyg) if hyg else "risky OFF, stop-on-reply ON")
    return findings


def verdict(findings: list[dict]) -> str:
    reds = sum(f["sev"] == "red" for f in findings)
    oranges = sum(f["sev"] == "orange" for f in findings)
    return "🔴 DO NOT SEND" if reds else "🟠 FIX FIRST" if oranges > 1 else "✓ INFRA OK"


SEV = {"red": "🔴", "orange": "🟠", "yellow": "🟡", "pass": "✓", "skip": "·", "info": "i"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("campaign_id", nargs="?")
    ap.add_argument("--leads", type=Path, help="recipient lead CSV (needs an `email` column)")
    ap.add_argument("--domains", help="comma-separated sending domains (override the attached fleet)")
    ap.add_argument("--max-domains", type=int, default=0, help="cap the dig sweep (0 = all)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.campaign_id:
        ap.error("campaign_id is required (or use --selftest)")

    from sequencer import get_sequencer

    seq = get_sequencer()
    camp = seq.campaign(a.campaign_id)
    accounts = seq.accounts()
    if a.domains:
        want = {d.strip().lower() for d in a.domains.split(",")}
        fleet = [x for x in accounts if x["email"].split("@", 1)[-1].lower() in want]
    else:
        senders = set(camp["senders"])
        fleet = [x for x in accounts if x["email"] in senders]

    target = None
    if a.leads:
        counts, total = recipient_domains(a.leads, a.max_domains)
        print(f"classifying MX for {len(counts)} recipient domains ({total} leads)...", file=sys.stderr)
        target = mx_profile(counts) if counts else None

    stats = seq.campaign_stats(a.campaign_id)
    leads = seq.campaign_leads(a.campaign_id) if stats.get("sent") else None
    findings = lint(camp, fleet, target, stats, leads)
    v = verdict(findings)

    if a.json:
        print(json.dumps({"campaign_id": a.campaign_id, "name": camp["name"], "verdict": v,
                          "fleet_esp": fleet_esp_share(fleet) if fleet else {},
                          "target_mx": target["share"] if target else {}, "findings": findings}, indent=2))
        return 1 if "🔴" in v else 0

    print(f"# Deliverability Lint — {camp['name']}\ncampaign_id: {a.campaign_id}")
    if fleet:
        doms = {x["email"].split("@", 1)[-1] for x in fleet}
        share = ", ".join(f"{e}={pct(s)}" for e, s in sorted(fleet_esp_share(fleet).items(), key=lambda x: -x[1]))
        print(f"fleet: {len(fleet)} inboxes across {len(doms)} domains — {share}")
    else:
        print("fleet: (no senders attached — attach inboxes or pass --domains)")
    print("\n## Checks\n")
    for f in findings:
        print(f"{SEV[f['sev']]} {f['id']:24s} {f['msg']}")
    reds = [f for f in findings if f["sev"] == "red"]
    print(f"\n**Verdict (infra layer): {v}** — {len(reds)} red · {sum(f['sev'] == 'orange' for f in findings)} orange")
    for f in reds:
        print(f"- {f['id']}: {f['msg']}")
    return 1 if reds else 0


def selftest() -> int:
    counts = Counter({"acme.com": 90, "gw.com": 5, "dead.com": 3, "flaky.com": 2})
    fake = {"acme.com": "microsoft", "gw.com": "gateway", "dead.com": "no-mx", "flaky.com": "dns-error"}
    t = mx_profile(counts, fake.get)
    assert t["unresolved"] == 2 and abs(t["share"]["gateway"] - 5 / 98) < 1e-9, "dns-error leaves the denominator"
    assert classify("gmail.com") == "google", "free-mail never dug"

    pb = projected_bounce({"no-mx": 0.01, "gateway": 0.05, "microsoft": 0.5}, {"google": 1.0}, False)
    assert abs(pb["total"] - (0.01 + 0.02 + 0.02)) < 1e-9

    google = [{"email": f"a{i}@s.com", "status": "active", "warmup_score": 99, "daily_limit": 30, "esp": "google"}
              for i in range(3)]
    camp = {"name": "t", "settings": {"open_tracking": True, "link_tracking": False, "allow_risky": False,
                                      "stop_on_reply": True, "daily_limit": 90}}
    f = {x["id"]: x for x in lint(camp, google, t, None, None)}
    assert f["gateway-recipients"]["sev"] == "red", "5% gateway > 2%"
    assert f["projected-bounce-rate"]["sev"] == "red"
    assert f["tracking-off"]["sev"] == "orange" and f["list-hygiene-flags"]["sev"] == "pass"
    assert f["inboxes-per-domain"]["sev"] == "pass"
    assert "live-bounce-rate" not in f, "nothing sent -> no live checks"

    sick = google + [{"email": "a3@s.com", "status": "error", "warmup_score": 50, "daily_limit": 30, "esp": "google"}]
    stats = {"sent": 1000, "contacted": 1000, "bounced": 40}
    leads = [{"email": "x@d.com", "bounced": True, "esp": "google"}, {"email": "y@d.com", "bounced": True, "esp": "google"},
             {"email": "z@e.com", "bounced": False, "esp": "microsoft"}]
    f = {x["id"]: x for x in lint(camp, sick, None, stats, leads)}
    assert f["live-bounce-rate"]["sev"] == "red", "4% actual > 3%"
    assert "same-ESP" in f["bounce-esp-breakdown"]["msg"]
    assert "d.com" in f["bounce-domain-clusters"]["msg"]
    assert f["dead-inboxes"]["sev"] == "orange" and f["inboxes-per-domain"]["sev"] == "orange"
    lopsided = ([{"email": f"g{i}@a.com", "bounced": False, "esp": "google"} for i in range(150)]
                + [{"email": f"m{i}@b.com", "bounced": i < 30, "esp": "microsoft"} for i in range(150)])
    f = {x["id"]: x for x in lint(camp, google, None, stats, lopsided)}
    assert f["bounce-esp-gap"]["sev"] == "orange" and "microsoft" in f["bounce-esp-gap"]["msg"]
    assert verdict(list(f.values())) == "🔴 DO NOT SEND"
    print("deliv_lint selftest: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
