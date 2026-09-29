#!/usr/bin/env python3
"""Per-DOMAIN inbox health + capacity for your sending fleet.

The unit of failure is the DOMAIN, not the inbox: filters judge a sending domain's
reputation, so one bad inbox drags its siblings down. This rolls up every inbox's
daily numbers by domain and answers:

  1. ALLOCATION — capacity/day (sum of each inbox's daily limit), split into
     ACTIVE (attached to a live campaign) and PARKED (warming / recovering),
     and how much of the active capacity the last send-day used.
  2. HEALTH — per domain, bounce and reply over the window, plus blacklist status.

Thresholds (see docs/deliverability-playbook.md):
  bounce > 5%   RED  -> recommend recovery, only past the volume floor
                        (>=150 sent AND >=5 bounces); under it = watch, too small to act
  bounce 2-5%   NOTE -> watch
  bounce < 1%        -> clear to come back from recovery
  reply  >=2.6% great · >=1% look-into · >=0.8% low · below = worry (judged past 150 sent)
  blacklist     Spamhaus DBL = stop sending on that domain. SURBL is ignored (a
                content-filter list Google/Microsoft don't read). Others = look.

Blacklist needs MX_TOOLBOX_API_KEY (free plan: 64 lookups/day). Without it the
check reports "NOT checked" — never "clean".

Read-only. Recovery (detaching a domain) is a manual step in your sequencer.

    python3 inbox_health.py                     # every live campaign's fleet
    python3 inbox_health.py --campaign <id>     # one campaign's fleet
    python3 inbox_health.py --window 14 --json
    python3 inbox_health.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

BOUNCE_RED = 0.05
BOUNCE_NOTE = 0.02
BOUNCE_EXIT = 0.01
MIN_SENT = 150  # volume floor before a >5% bounce is a RECOVER call, not a watch
MIN_BOUNCES = 5
REPLY_GREAT = 0.026
REPLY_LOOK = 0.010
REPLY_WORRY = 0.008
REPLY_MIN_SENT = 150  # reply rate is noise below this volume

MX_BLACKLIST_URL = "https://api.mxtoolbox.com/api/v1/lookup/blacklist/"
FATAL_LISTS = {"Spamhaus DBL"}  # receiving servers query it live: listed = refused or junked
IGNORED_LISTS = {"SURBL multi"}  # URI list read by content filters, not by Google/Microsoft


def _dom(email: str) -> str:
    return email.split("@", 1)[-1].lower()


# ───────────────────────────────────────────────── rollup + classify (pure)


def rollup_by_domain(daily_rows: list[dict], fleet: list[dict], last_day: str | None) -> dict[str, dict[str, int]]:
    """Per-inbox daily rows -> per-domain totals. Seeded from the FLEET, so a domain
    that sent nothing still shows up at zero instead of silently vanishing."""
    keys = ("inboxes", "capacity", "sent", "bounced", "replies", "sent_last_day")
    per: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(keys, 0))
    for a in fleet:
        per[_dom(a["email"])]["inboxes"] += 1
        per[_dom(a["email"])]["capacity"] += a.get("daily_limit") or 0
    for r in daily_rows:
        if "@" not in (r.get("email") or ""):
            continue
        m = per[_dom(r["email"])]
        for k in ("sent", "bounced", "replies"):
            m[k] += r.get(k) or 0
        if last_day and r.get("date") == last_day:
            m["sent_last_day"] += r.get("sent") or 0
    return dict(per)


def last_send_day(daily_rows: list[dict]) -> str | None:
    """Latest day the fleet actually sent. Not "yesterday": on a Monday that's a dead
    Sunday and utilisation would read 0% every week."""
    days = {r["date"] for r in daily_rows if (r.get("sent") or 0) > 0 and r.get("date")}
    return max(days) if days else None


def classify_domain(agg: dict[str, int], active: bool = True) -> dict[str, Any]:
    """Bounce + reply verdict for one domain. Only an ACTIVE domain earns an action:
    a parked one is already off the cold path."""
    sent, bounced, replies = agg.get("sent", 0), agg.get("bounced", 0), agg.get("replies", 0)
    bnc = bounced / sent if sent else 0.0
    rpl = replies / sent if sent else 0.0
    if bnc > BOUNCE_RED and sent >= MIN_SENT and bounced >= MIN_BOUNCES:
        bv, action = "RED", "recover"
    elif bnc > BOUNCE_RED:
        bv, action = "NOTE", "watch-lowvol"  # a 3/32 fluke must never pull a domain
    elif bnc >= BOUNCE_NOTE:
        bv, action = "NOTE", "watch"
    else:
        bv, action = "OK", None
    if sent < REPLY_MIN_SENT:
        rv = "n/a"
    elif rpl >= REPLY_GREAT:
        rv = "great"
    elif rpl >= REPLY_LOOK:
        rv = "look-into"
    elif rpl >= REPLY_WORRY:
        rv = "low"
    else:
        rv = "worry"
    return {**agg, "active": active, "bounce_pct": round(bnc, 4), "reply_pct": round(rpl, 4),
            "bounce_verdict": bv, "reply_verdict": rv, "action": action if active else None}


def allocation(domains: dict[str, dict]) -> dict[str, Any]:
    """Capacity split active/parked + utilisation against ACTIVE capacity only
    (parked capacity can't send, so folding it in would hide being capacity-bound)."""
    act = [m for m in domains.values() if m["active"]]
    parked = [m for m in domains.values() if not m["active"]]
    cap = sum(m["capacity"] for m in act)
    used = sum(m["sent_last_day"] for m in domains.values())
    return {"active_domains": len(act), "active_inboxes": sum(m["inboxes"] for m in act), "active_capacity": cap,
            "parked_domains": len(parked), "parked_inboxes": sum(m["inboxes"] for m in parked),
            "parked_capacity": sum(m["capacity"] for m in parked),
            "sent_last_day": used, "utilisation": round(used / cap, 4) if cap else 0.0}


# ──────────────────────────────────────────────────────── blacklist (IO)


def blacklist_check(domains: list[str]) -> dict[str, Any]:
    """Domain blacklists via MXToolbox (one call per domain). Spamhaus refuses public
    DNS resolvers, so a plain `dig` against dbl.spamhaus.org can't be trusted —
    MXToolbox queries from authorized infrastructure. A failed lookup lands in
    `errors` and reads as NOT CHECKED, never as clean."""
    key = os.environ.get("MX_TOOLBOX_API_KEY")
    listed: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for d in sorted(set(domains)):
        if not key:
            errors[d] = "MX_TOOLBOX_API_KEY not set"
            continue
        try:
            req = urllib.request.Request(MX_BLACKLIST_URL + d, headers={"Authorization": key})  # plain key, no Bearer
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read())
            if any(t.get("Name") in FATAL_LISTS for t in body.get("Timeouts") or []):
                errors[d] = "Spamhaus DBL lookup timed out"
                continue
            if hits := [f["Name"] for f in body.get("Failed") or []]:
                listed[d] = hits
        except Exception as exc:  # any failure means "not checked", never "clean"
            errors[d] = str(exc)[:120]
    return {"listed": listed, "errors": errors, "checked": len(set(domains)) - len(errors)}


# ────────────────────────────────────────────────────────────── render


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render(domains: dict[str, dict], alloc: dict, bl: dict, window: tuple[str, str], last_day: str | None) -> str:
    out = [f"# Inbox health — {window[0]}..{window[1]}", "",
           "**Allocation** (emails/day)", "",
           "| | domains | inboxes | capacity/day |", "|---|--:|--:|--:|",
           f"| active (attached to a live campaign) | {alloc['active_domains']} | {alloc['active_inboxes']} "
           f"| {alloc['active_capacity']} |",
           f"| parked (warming / recovering) | {alloc['parked_domains']} | {alloc['parked_inboxes']} "
           f"| {alloc['parked_capacity']} |", "",
           f"Last send-day{f' ({last_day})' if last_day else ''}: **{alloc['sent_last_day']}** sent / "
           f"{alloc['active_capacity']} active capacity = **{_pct(alloc['utilisation'])}** used.", "",
           "**Domains**", "",
           "| domain | state | inboxes | cap/day | sent | bounced | bounce% | replies | reply% | verdict |",
           "|---|:--|--:|--:|--:|--:|--:|--:|--:|:--|"]
    recover, watch = [], []
    for dom, m in sorted(domains.items(), key=lambda kv: (not kv[1]["active"], -kv[1]["bounce_pct"])):
        if m["active"]:
            v = {"RED": "⛔ RED", "NOTE": "🟡 NOTE", "OK": "✓ OK"}[m["bounce_verdict"]]
            if m["reply_verdict"] not in ("n/a", "great"):
                v += f" · reply {m['reply_verdict']}"
        else:
            v = "parked" + (" · clear to re-attach" if m["sent"] == 0 or m["bounce_pct"] < BOUNCE_EXIT else "")
        out.append(f"| {dom} | {'active' if m['active'] else 'parked'} | {m['inboxes']} | {m['capacity']} | "
                   f"{m['sent']} | {m['bounced']} | {_pct(m['bounce_pct'])} | {m['replies']} | "
                   f"{_pct(m['reply_pct'])} | {v} |")
        if m["action"] == "recover":
            recover.append(f"{dom}: bounce {_pct(m['bounce_pct'])} ({m['bounced']}/{m['sent']}) — over 5% "
                           f"past the volume floor")
        elif m["action"] == "watch-lowvol":
            watch.append(f"{dom}: over 5% but under the {MIN_SENT}-send floor (sample too small to act)")
        elif m["action"] == "watch":
            watch.append(f"{dom}: bounce {_pct(m['bounce_pct'])} in the 2-5% band")
        if m["active"] and m["reply_verdict"] == "worry":
            watch.append(f"{dom}: reply {_pct(m['reply_pct'])} — below 0.8% on {m['sent']} sent")
    out.append("")

    fatal = {d: h for d, h in bl["listed"].items() if FATAL_LISTS.intersection(h)}
    minor = {d: [x for x in h if x not in IGNORED_LISTS] for d, h in bl["listed"].items() if d not in fatal}
    minor = {d: h for d, h in minor.items() if h}
    if bl["errors"]:
        out.append(f"- Blacklist: ⚠ {len(bl['errors'])} of {len(bl['errors']) + bl['checked']} domains NOT checked "
                   f"({next(iter(bl['errors'].values()))}) — this is NOT a clean result")
    else:
        out.append(f"- Blacklist: {bl['checked']} domains clean on Spamhaus DBL ✓")
    out.append("")
    if fatal:
        out += ["## 🔴 BLACKLISTED (Spamhaus DBL) — stop sending on these domains"]
        out += [f"- {d}: {', '.join(h)}" for d, h in fatal.items()]
        out += ["_Young domain (<30 days): replace it. Older: pause, request delisting, resume slowly. "
                "Several listed at once = your content or list, not bad luck._", ""]
    if minor:
        out += ["## 🟡 Minor blacklist — look, no action"]
        out += [f"- {d}: {', '.join(h)}" for d, h in minor.items()]
        out += ["_Compare against a clean sibling domain in the same campaign before touching it._", ""]
    if recover:
        out += ["## ⛔ RECOMMEND RECOVERY"] + [f"- {r}" for r in recover]
        out += ["_Detach the domain's inboxes from every campaign, keep warmup ON. Re-attach once warmup is "
                "healthy and a small test window bounces under 1%. See docs/deliverability-playbook.md._", ""]
    if watch:
        out += ["## 🟡 WATCH (no action yet)"] + [f"- {w}" for w in watch] + [""]
    if not recover and not watch and not fatal:
        out.append("_All domains OK — no bounce over 2%, no reply-rate worry._")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────── main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", help="one campaign id (default: every live campaign)")
    ap.add_argument("--window", type=int, default=14, help="rollup window in days (default 14)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="don't write the history snapshot")
    ap.add_argument("--selftest", action="store_true", help="offline asserts, no network")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    from sequencer import get_sequencer

    seq = get_sequencer()
    ids = [a.campaign] if a.campaign else [c["id"] for c in seq.campaigns() if c["active"]]
    attached = {_dom(e) for cid in ids for e in seq.campaign(cid)["senders"]}
    accounts = seq.accounts()
    # One campaign: its attached domains. All: the whole workspace fleet (parked domains included).
    fleet = [x for x in accounts if _dom(x["email"]) in attached] if a.campaign else accounts
    if not fleet:
        print("No sending inboxes found (no live campaign, or none attached).")
        return 0

    end = date.today()
    start = end - timedelta(days=a.window)
    rows = seq.inbox_daily_stats([x["email"] for x in fleet], start, end)
    if not a.no_save:  # history/ feeds trends.py; the web UI's history CSV is the same format
        import trends
        esp = {x["email"]: x["esp"] for x in fleet}
        snap = trends.HISTORY / f"inbox-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
        trends.write_csv(snap, [{**r, "domain": _dom(r["email"]), "esp": esp.get(r["email"], "other")} for r in rows])
        print(f"history saved: {snap.name}", file=sys.stderr)
    last_day = last_send_day(rows)
    domains = {d: classify_domain(m, active=d in attached) for d, m in rollup_by_domain(rows, fleet, last_day).items()}
    alloc = allocation(domains)
    bl = blacklist_check(list(domains))

    if a.json:
        print(json.dumps({"window": [start.isoformat(), end.isoformat()], "last_send_day": last_day,
                          "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "allocation": alloc, "domains": domains, "blacklist": bl}, indent=2))
    else:
        print(render(domains, alloc, bl, (start.isoformat(), end.isoformat()), last_day))
    bad = any(m["action"] == "recover" for m in domains.values()) or any(
        FATAL_LISTS.intersection(h) for h in bl["listed"].values())
    return 1 if bad else 0


def selftest() -> int:
    hot = classify_domain({"sent": 400, "bounced": 30, "replies": 8})
    assert hot["bounce_verdict"] == "RED" and hot["action"] == "recover", "7.5% on 400 sent -> recover"
    fluke = classify_domain({"sent": 32, "bounced": 3, "replies": 2})
    assert fluke["action"] == "watch-lowvol", "9.4% on 32 sent -> watch, not a pull"
    assert classify_domain({"sent": 700, "bounced": 21, "replies": 16})["action"] == "watch", "3% -> watch"
    ok = classify_domain({"sent": 700, "bounced": 6, "replies": 8})
    assert ok["bounce_verdict"] == "OK" and ok["reply_verdict"] == "look-into"
    assert classify_domain({"sent": 50, "bounced": 0, "replies": 1})["reply_verdict"] == "n/a"
    assert classify_domain({"sent": 400, "bounced": 30, "replies": 8}, active=False)["action"] is None, \
        "a parked domain never gets an action"

    rows = [
        {"date": "2026-07-28", "email": "a@d1.com", "sent": 10, "bounced": 1, "replies": 0},
        {"date": "2026-07-27", "email": "a@d1.com", "sent": 5, "bounced": 0, "replies": 1},
        {"date": "2026-07-28", "email": "b@d1.com", "sent": 5, "bounced": 0, "replies": 0},
        {"date": "2026-07-28", "email": "c@d2.com", "sent": 20, "bounced": 2, "replies": 1},
        {"date": "2026-07-29", "email": "a@d1.com", "sent": 0, "bounced": 0, "replies": 0},
    ]
    fleet = [{"email": e, "daily_limit": 30} for e in ("a@d1.com", "b@d1.com", "c@d1.com", "c@d2.com", "x@idle.com")]
    assert last_send_day(rows) == "2026-07-28", "last SEND day, not the last row"
    ru = rollup_by_domain(rows, fleet, "2026-07-28")
    assert ru["d1.com"]["sent"] == 20 and ru["d1.com"]["inboxes"] == 3 and ru["d1.com"]["capacity"] == 90
    assert ru["d1.com"]["sent_last_day"] == 15
    assert ru["idle.com"]["sent"] == 0 and ru["idle.com"]["inboxes"] == 1, "a 0-send domain still gets a row"

    doms = {d: classify_domain(m, active=d != "idle.com") for d, m in ru.items()}
    al = allocation(doms)
    assert al["active_capacity"] == 120 and al["parked_capacity"] == 30
    assert al["utilisation"] == round(35 / 120, 4), "utilisation vs ACTIVE capacity only"

    bl = {"listed": {"d2.com": ["Spamhaus DBL"], "d1.com": ["SURBL multi"]}, "errors": {}, "checked": 3}
    out = render(doms, al, bl, ("2026-07-14", "2026-07-28"), "2026-07-28")
    assert "BLACKLISTED" in out and "d2.com" in out, "DBL is the top finding"
    assert "Minor blacklist" not in out, "SURBL-only is never a finding"
    no_key = render(doms, al, {"listed": {}, "errors": {"d1.com": "MX_TOOLBOX_API_KEY not set"}, "checked": 2},
                    ("a", "b"), None)
    assert "NOT checked" in no_key and "clean on Spamhaus" not in no_key, "unchecked never reads as clean"
    print("inbox_health selftest: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
