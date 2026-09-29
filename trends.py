#!/usr/bin/env python3
"""trends.py — is deliverability moving? Reads the saved per-inbox daily history.

`inbox_health.py` saves every run to history/inbox-<utc>.csv (the web UI's "Download
history" CSV is the same file, so drop those into history/ too). Runs overlap, so rows
are merged and de-duplicated on (date, email); history grows past the API window.

Compares the last 7 days with the 7 before, per sending domain, per sender ESP and
fleet-wide, and flags what moved:

  bounce-up     bounce rate rising          reply-down   reply rate falling
  volume-drop   sends collapsed (paused / disconnected inbox)
  domain-outlier  one domain far worse than the rest of the fleet
  esp-gap       google inboxes deliver while microsoft ones don't (or the reverse)

  python3 trends.py                 # history/*.csv
  python3 trends.py --dir path --json
  python3 trends.py --selftest

Exit 1 if any 🔴. js/checks.js mirrors these rules for the web UI: change both.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HISTORY = Path(__file__).resolve().with_name("history")
COLS = ["date", "email", "domain", "esp", "sent", "bounced", "replies"]
WINDOW = 7
MIN_SENT = 150        # per group per window; below this a rate is noise
MIN_DOMAIN_SENT = 100
BOUNCE_MIN = 0.02     # never flag a bounce rate under 2%
BOUNCE_JUMP = 0.01    # ...and it must have risen by 1 point
BOUNCE_RED = 0.05
REPLY_DROP = 0.6      # recent reply rate < 60% of prior
REPLY_FLOOR = 0.01    # only when the prior rate was meaningful
OUTLIER_X = 2.0       # domain bounce >= 2x the rest of the fleet
ESP_X = 2.0


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load(directory: Path) -> list[dict]:
    """Merge every history CSV; the later file wins on a duplicate (date, email)."""
    merged: dict[tuple[str, str], dict] = {}
    for p in sorted(directory.glob("*.csv")):
        with p.open(newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("date") and r.get("email"):
                    merged[(r["date"], r["email"])] = {
                        "date": r["date"], "email": r["email"],
                        "domain": r.get("domain") or r["email"].split("@", 1)[-1],
                        "esp": r.get("esp") or "other",
                        **{k: int(float(r.get(k) or 0)) for k in ("sent", "bounced", "replies")}}
    return list(merged.values())


def _agg(rows: list[dict]) -> dict:
    s = sum(r["sent"] for r in rows)
    b = sum(r["bounced"] for r in rows)
    p = sum(r["replies"] for r in rows)
    return {"sent": s, "bounced": b, "replies": p, "bounce": b / s if s else 0.0, "reply": p / s if s else 0.0}


def analyze(rows: list[dict]) -> dict:
    """{'window': {...}, 'findings': [{'sev','id','group','msg'}]}. Pure."""
    days = sorted({r["date"] for r in rows if r["sent"] > 0})
    if len(days) < 2:
        return {"window": None, "findings": [], "note": "not enough history yet (need sending days on record)"}
    end = date.fromisoformat(days[-1])
    cut = (end - timedelta(days=WINDOW)).isoformat()
    cut2 = (end - timedelta(days=2 * WINDOW)).isoformat()
    recent = [r for r in rows if r["date"] > cut]
    prior = [r for r in rows if cut2 < r["date"] <= cut]
    win = {"recent": [cut, end.isoformat()], "prior": [cut2, cut], "days_on_record": len(days)}
    f: list[dict] = []

    def add(sev: str, id_: str, group: str, msg: str) -> None:
        f.append({"sev": sev, "id": id_, "group": group, "msg": msg})

    def pc(x: float) -> str:
        return f"{x * 100:.1f}%"

    keyed: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: {"recent": [], "prior": []})
    for tag, src in (("recent", recent), ("prior", prior)):
        for r in src:
            for kind, key in (("fleet", "fleet"), ("esp", r["esp"]), ("domain", r["domain"])):
                keyed[(kind, key)][tag].append(r)

    stat = {k: (_agg(v["recent"]), _agg(v["prior"])) for k, v in keyed.items()}
    for (kind, key), (rc, pr) in sorted(stat.items()):
        label = key if kind == "fleet" else f"{kind} {key}"
        floor = MIN_DOMAIN_SENT if kind == "domain" else MIN_SENT
        if rc["sent"] >= floor and rc["bounce"] >= BOUNCE_MIN and rc["bounce"] - pr["bounce"] >= BOUNCE_JUMP \
                and pr["sent"] >= floor:
            add("red" if rc["bounce"] > BOUNCE_RED else "orange", "bounce-up", label,
                f"{label}: bounce {pc(pr['bounce'])} → {pc(rc['bounce'])} ({rc['bounced']}/{rc['sent']} sent this week)")
        if pr["sent"] >= MIN_SENT and rc["sent"] >= MIN_SENT and pr["reply"] >= REPLY_FLOOR \
                and rc["reply"] < REPLY_DROP * pr["reply"]:
            add("orange", "reply-down", label, f"{label}: reply {pc(pr['reply'])} → {pc(rc['reply'])}")
        if pr["sent"] >= MIN_SENT and rc["sent"] <= pr["sent"] * 0.5:
            add("orange", "volume-drop", label,
                f"{label}: sent {pr['sent']} → {rc['sent']} — paused or disconnected inboxes?")

    fleet_r = stat.get(("fleet", "fleet"), (None, None))[0]
    if fleet_r:
        for (kind, key), (rc, _) in sorted(stat.items()):
            if kind != "domain" or rc["sent"] < MIN_DOMAIN_SENT:
                continue
            rest = _agg([r for r in recent if r["domain"] != key])  # everyone else, so one domain can't hide itself
            if rc["bounce"] >= BOUNCE_MIN + BOUNCE_JUMP and rc["bounce"] >= OUTLIER_X * max(rest["bounce"], 0.005):
                add("red" if rc["bounce"] > BOUNCE_RED else "orange", "domain-outlier", f"domain {key}",
                    f"{key}: bounce {pc(rc['bounce'])} vs {pc(rest['bounce'])} for the rest of the fleet")

    e = {k[1]: v[0] for k, v in stat.items() if k[0] == "esp" and v[0]["sent"] >= MIN_SENT}
    for a in sorted(e):
        for b in sorted(e):
            if a < b:
                hi, lo = (a, b) if e[a]["bounce"] >= e[b]["bounce"] else (b, a)
                if e[hi]["bounce"] >= BOUNCE_MIN + BOUNCE_JUMP and e[hi]["bounce"] >= ESP_X * max(e[lo]["bounce"], 0.005):
                    add("orange", "esp-gap", f"esp {hi}",
                        f"{lo} inboxes deliver ({pc(e[lo]['bounce'])} bounce) while {hi} inboxes don't "
                        f"({pc(e[hi]['bounce'])}) — {hi} recipients or {hi}-hosted senders are the problem")
                hi, lo = (a, b) if e[a]["reply"] >= e[b]["reply"] else (b, a)
                if e[lo]["reply"] * ESP_X <= e[hi]["reply"] and e[hi]["reply"] >= REPLY_FLOOR:
                    add("yellow", "esp-gap", f"esp {lo}",
                        f"{lo} inboxes get {pc(e[lo]['reply'])} replies vs {pc(e[hi]['reply'])} on {hi} "
                        f"— likely landing in spam on {lo}")
    return {"window": win, "findings": f}


def render(res: dict) -> str:
    if not res["window"]:
        return f"Trends: {res['note']}"
    w = res["window"]
    out = [f"Trends — last 7d ({w['recent'][0]}..{w['recent'][1]}) vs the 7d before · {w['days_on_record']} sending days on record"]
    icon = {"red": "🔴", "orange": "🟠", "yellow": "🟡"}
    out += [f"  {icon[x['sev']]} [{x['id']}] {x['msg']}" for x in res["findings"]] or ["  ✓ nothing moved"]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=HISTORY)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = load(a.dir)
    if not rows:
        print(f"No history in {a.dir}. Run inbox_health.py (it saves a snapshot) or drop the web UI's history CSV there.")
        return 0
    res = analyze(rows)
    print(json.dumps(res, indent=2) if a.json else render(res))
    return 1 if any(x["sev"] == "red" for x in res["findings"]) else 0


def _mk(days_ago: int, email: str, esp: str, sent: int, bounced: int, replies: int, end: str = "2026-09-28") -> dict:
    d = (date.fromisoformat(end) - timedelta(days=days_ago)).isoformat()
    return {"date": d, "email": email, "domain": email.split("@")[1], "esp": esp,
            "sent": sent, "bounced": bounced, "replies": replies}


def selftest() -> int:
    rows = []
    for i in range(14):
        new = i < 7  # days_ago 0-6 = recent week
        rows += [
            _mk(i, "a@good.com", "google", 30, 0, 1),                       # steady
            _mk(i, "b@good.com", "google", 30, 0, 1),
            _mk(i, "c@burn.com", "microsoft", 30, 5 if new else 0, 0 if new else 1),  # burning + reply gone
            _mk(i, "d@ms.com", "microsoft", 30, 4 if new else 0, 0),        # microsoft bounces, google doesn't
        ]
    ids = {(x["id"], x["group"]) for x in analyze(rows)["findings"]}
    assert ("bounce-up", "domain burn.com") in ids and ("domain-outlier", "domain burn.com") in ids
    assert ("esp-gap", "esp microsoft") in ids, "google fine, microsoft not"
    assert ("reply-down", "domain burn.com") in ids
    assert not any(g == "domain good.com" for _, g in ids), "steady domain stays quiet"
    quiet = [_mk(i, "a@x.com", "google", 30, 0, 1) for i in range(14)]
    assert analyze(quiet)["findings"] == [], "flat history flags nothing"
    fall = [_mk(i, "a@x.com", "google", 5 if i < 7 else 30, 0, 0) for i in range(14)]
    assert any(x["id"] == "volume-drop" for x in analyze(fall)["findings"])
    assert analyze([_mk(0, "a@x.com", "google", 30, 0, 0)])["window"] is None, "one day is not a trend"
    import tempfile
    with tempfile.TemporaryDirectory() as t:  # overlapping snapshots dedupe; later wins
        write_csv(Path(t) / "a.csv", [_mk(0, "a@x.com", "google", 10, 0, 0)])
        write_csv(Path(t) / "b.csv", [_mk(0, "a@x.com", "google", 12, 1, 0), _mk(1, "a@x.com", "google", 5, 0, 0)])
        got = load(Path(t))
        assert len(got) == 2 and next(r for r in got if r["date"] == "2026-09-28")["sent"] == 12
    print("trends selftest: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
