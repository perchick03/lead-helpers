#!/usr/bin/env python3
"""Copy + merge-variable check for one campaign, BEFORE you upload the leads.

Pulls the LIVE sequence from your sequencer (subject + body, every step, every
variant) and cross-checks it against the lead CSV you are about to upload:

  - every `{{tag}}` the copy uses resolves to a CSV column (or a tag the tool fills)
  - no variable renders EMPTY on any row
  - first_name values look like first names (not ALL CAPS, not "Dr Sam", not "Acme LLC")
  - no em/en-dashes in the copy or in any variable value
  - no spam words INSIDE lead values ("Acme Discount Tours" ships `discount`
    in every email to that lead — no template review ever sees it)
  - enough spintax; lowercase sentence starts and paste artifacts in rendered samples
  - rendered samples printed so a human can read what actually goes out

Read-only. No LLM — the judgment layer is the `spam-checker` and
`email-copy-check` agents, which read the samples this prints.

    python3 copy_check.py <campaign_id> --csv leads.csv [--samples 3] [--json]
    python3 copy_check.py --selftest

Exit: 0 clean · 1 blockers · 2 warnings only.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from spam_lint import lint as spam_lint

# A first_name value that trips any of these is not a usable greeting.
TITLE_PREFIXES = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "prof", "prof.", "rev", "rev.",
                  "sir", "capt", "capt.", "col", "col.", "hon", "hon.", "miss", "mx", "mx.", "eng",
                  "eng.", "atty"}
COMPANY_MARKERS = {"llc", "inc", "inc.", "ltd", "ltd.", "corp", "corp.", "gmbh", "bv", "plc", "sa", "srl",
                   "pty", "llp", "co", "co.", "group", "travel", "agency"}
DASHES = {"—": "em-dash", "–": "en-dash"}

MIN_SPIN_BLOCKS = 2  # per variant; below this every recipient gets a near-identical email
MAX_NAME_LEN = 20
MAX_ACRONYM_LEN = 4  # "USA"/"LLC" are fine all-caps; longer means shouting

# Words whose trailing dot is not a sentence end. Dotted acronyms (U.S., e.g.)
# are caught structurally by `_ends_sentence`, so only whole-word ones list here.
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc", "ltd", "co", "corp",
                 "llc", "no", "approx", "dept", "est", "ave", "blvd", "fig", "al", "jan", "feb", "mar", "apr",
                 "jun", "jul", "aug", "sept", "sep", "oct", "nov", "dec"}
# Dotted acronyms that OPEN a sentence rather than sitting inside one.
SENTENCE_PREFIXES = {"p.s", "p.p.s", "n.b"}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]*")
TAG_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
RANDOM_RE = re.compile(r"\{\{\s*RANDOM\s*\|([^}]+)\}\}")


# ─────────────────────────────────────────────────────────────────── html


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("div", "p", "br"):
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("div", "p"):
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def strip_html(html: str) -> str:
    p = _Stripper()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", unescape("".join(p.out))).strip()


def snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


# ─────────────────────────────────────────────────────────── pure checks


def tags_used(text: str) -> set[str]:
    """Merge tags in `text`, excluding the RANDOM spintax marker itself."""
    return {t for t in TAG_RE.findall(RANDOM_RE.sub(lambda m: m.group(1), text)) if t != "RANDOM"}


def resolves(tag: str, columns: set[str], system_tags: frozenset = frozenset()) -> bool:
    """A tag renders if the sequencer fills it or some CSV column spells it."""
    if tag in system_tags:
        return True
    return any(c in columns for c in (tag, tag.lower(), snake(tag)))


def render(text: str, lead: dict[str, str]) -> str:
    """Substitute named tags first (so tags inside RANDOM resolve), then take the first spin option."""

    def repl(m: re.Match) -> str:
        key = m.group(1).strip()
        for k in (key, key.lower(), snake(key)):
            if lead.get(k):
                return lead[k]
        return f"[{key}?]"

    return RANDOM_RE.sub(lambda m: m.group(1).split("|")[0], TAG_RE.sub(repl, text))


def name_problem(value: str) -> str | None:
    """Why this first_name would embarrass us in a greeting, or None."""
    v = value.strip()
    if not v:
        return "empty"
    if "@" in v or v.startswith(("http", "www.")):
        return "looks like an email/url"
    if any(ch.isdigit() for ch in v):
        return "contains digits"
    words = v.split()
    if words[0].lower() in TITLE_PREFIXES:
        return f"leading title ({words[0]})"
    if any(w.lower().strip(",") in COMPANY_MARKERS for w in words):
        return "looks like a company name"
    if len(words) > 2:
        return f"{len(words)} words — likely a full name or company"
    if len(v) > MAX_NAME_LEN:
        return f"{len(v)} chars — too long for a greeting"
    if len(v) == 1:
        return "single character"
    if v.isupper():
        return "ALL CAPS"
    if v.islower():
        return "all lowercase"
    if not all(ch.isalpha() or ch in " -'." for ch in v):
        return "non-name characters"
    return None


def paste_artifacts(text: str) -> list[str]:
    """Words missing a space: a lowercase-starting token carrying a capital.
    `growingAcme` lost a space; `LinkedIn` is just a brand name."""
    return [w for w in WORD_RE.findall(text) if w[0].islower() and any(c.isupper() for c in w)]


def dash_hits(text: str) -> list[str]:
    return [name for ch, name in DASHES.items() if ch in text]


def _ends_sentence(text: str, i: int) -> bool:
    """Is the terminator at `text[i]` a real sentence end, or an abbreviation's dot?
    "...the U.S. market" is ONE sentence; "fixing" it yields "U.S. Market"."""
    if text[i] != ".":
        return True
    word = text[:i].split()[-1] if text[:i].split() else ""
    if word.lower().rstrip(".") in SENTENCE_PREFIXES:
        return True
    tail = word.rsplit(".", 1)[-1]  # a dotted acronym ends in a single letter: "U.S", "e.g"
    if len(tail) == 1 and tail.isalpha():
        return False
    return word.lower().rstrip(".") not in ABBREVIATIONS


def lowercase_sentence_starts(text: str) -> list[str]:
    """Sentences opening lowercase — usually a variable landing mid-sentence."""
    bad = []
    for line in text.splitlines():
        line = line.strip()
        for m in re.finditer(r"[.!?](\s+)(?=[a-z])", line):
            if _ends_sentence(line, m.start()) and line[m.end()].isalpha():
                bad.append(line[m.end():m.end() + 60])
    return bad


def shouty_values(rows: list[dict[str, str]], col: str) -> list[str]:
    """Long or multi-word ALL CAPS values ("ACME TRAVEL GROUP") — they read as shouting
    mid-sentence. Short tokens (`TX`, `USA`, `CEO`) are how those are written."""
    out = []
    for r in rows:
        v = (r.get(col) or "").strip()
        letters = [c for c in v if c.isalpha()]
        if letters and all(c.isupper() for c in letters) and (len(v) > MAX_ACRONYM_LEN or " " in v):
            out.append(v)
    return out


def spam_values(rows: list[dict[str, str]], col: str) -> dict[str, list[str]]:
    """{spam word: distinct values} for a rendered column. Words only — caps and
    dashes in values have their own checks. Same list as spam_lint.py."""
    out: dict[str, list[str]] = {}
    for v in dict.fromkeys(r.get(col, "") for r in rows):
        for f in spam_lint(v):
            if f["cat"] in ("word", "context"):
                out.setdefault(f["hit"], []).append(v)
    return out


# ──────────────────────────────────────────────────────────── csv side


def read_csv(path: Path) -> tuple[list[dict[str, str]], set[str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = [{k: (v or "") for k, v in r.items() if k} for r in reader]
        return rows, set(reader.fieldnames or [])


def audit_csv(rows: list[dict[str, str]], used: set[str], columns: set[str]) -> dict[str, Any]:
    """Value-level audit of the columns the copy actually renders."""
    cols_used = sorted({c for t in used for c in (t, t.lower(), snake(t)) if c in columns})
    empties, dashes, shouty, spam = {}, {}, {}, {}
    for col in cols_used:
        if n := sum(1 for r in rows if not r.get(col, "").strip()):
            empties[col] = n
        if samples := [r[col] for r in rows if dash_hits(r.get(col, ""))][:3]:
            dashes[col] = samples
        if hits := shouty_values(rows, col):
            shouty[col] = hits
        if words := spam_values(rows, col):
            spam[col] = words
    names: dict[str, list[str]] = {}
    if "first_name" in columns:
        for r in rows:
            if why := name_problem(r.get("first_name", "")):
                names.setdefault(why, []).append(r.get("first_name", "") or "(blank)")
    return {
        "rows": len(rows),
        "columns_rendered": cols_used,
        "empty_values": empties,
        "dash_values": dashes,
        "shouty_values": {k: {"count": len(v), "samples": v[:5]} for k, v in shouty.items()},
        "spam_values": spam,
        "name_problems": {k: {"count": len(v), "samples": v[:5]} for k, v in names.items()},
    }


# ──────────────────────────────────────────────────────── variant side


def audit_variant(subject_raw: str, body_html: str, columns: set[str],
                  system_tags: frozenset = frozenset()) -> dict[str, Any]:
    body_raw = strip_html(body_html)
    used = tags_used(subject_raw) | tags_used(body_raw)
    spins = RANDOM_RE.findall(body_raw) + RANDOM_RE.findall(subject_raw)
    prose = TAG_RE.sub(" ", RANDOM_RE.sub(lambda m: m.group(1), body_raw))  # camelCase tags aren't artifacts
    return {
        "tags_used": sorted(used),
        "unresolvable_tags": sorted(t for t in used if not resolves(t, columns, system_tags)),
        "spin_blocks": len(spins),
        "spin_options": [len(s.split("|")) for s in spins],
        "dashes": dash_hits(subject_raw) + dash_hits(body_raw),
        "paste_artifacts": paste_artifacts(prose)[:5],
        "empty_subject": not subject_raw.strip(),
        "empty_body": not body_raw.strip(),
        "_subject_raw": subject_raw,
        "_body_raw": body_raw,
    }


def verdict(blockers: list[str], warnings: list[str]) -> str:
    if blockers:
        return "BLOCKED"
    return "WARNINGS" if warnings else "CLEAN"


def sample_rows(rows: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    """Spread the samples across the file — the tail is where imports go wrong."""
    if len(rows) <= n:
        return rows
    step = len(rows) // n
    return [rows[i * step] for i in range(n)]


# ────────────────────────────────────────────────────────────── runtime


def run(campaign: dict, rows: list[dict[str, str]], columns: set[str], system_tags: frozenset,
        n_samples: int) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    report: dict[str, Any] = {"campaign": campaign.get("name") or campaign.get("id"), "variants": []}
    if not rows:
        warnings.append("no lead rows to check")
    steps = campaign.get("steps") or []
    if not steps:
        blockers.append("sequence has no steps")

    all_used: set[str] = set()
    for si, variants in enumerate(steps, 1):
        if not variants:
            warnings.append(f"step {si} has no variants at all")
        for vi, v in enumerate(variants):
            a = audit_variant(v["subject"], v["body"], columns, system_tags)
            label = f"step {si} variant {chr(65 + vi)}"
            all_used |= set(a["tags_used"])
            if a["empty_body"]:
                blockers.append(f"{label}: empty body")
            if a["empty_subject"] and si == 1:
                blockers.append(f"{label}: empty subject on the first email")
            if a["unresolvable_tags"]:
                blockers.append(f"{label}: tags with no CSV column: {a['unresolvable_tags']}")
            if a["dashes"]:
                blockers.append(f"{label}: {', '.join(sorted(set(a['dashes'])))} in copy")
            if a["spin_blocks"] < MIN_SPIN_BLOCKS:
                warnings.append(f"{label}: {a['spin_blocks']} spintax block(s), under {MIN_SPIN_BLOCKS}")
            if a["paste_artifacts"]:
                warnings.append(f"{label}: paste artifacts {a['paste_artifacts']}")
            a["samples"] = []
            for lead in sample_rows(rows, n_samples):
                body = render(a["_body_raw"], lead)
                if lower := lowercase_sentence_starts(body):
                    warnings.append(f"{label}: lowercase sentence start — {lower[0]!r}")
                a["samples"].append({"to": lead.get("email", "?"), "subject": render(a["_subject_raw"], lead),
                                     "body": body})
            a.pop("_subject_raw"), a.pop("_body_raw")
            report["variants"].append({"label": label, **a})

    ca = report["csv_audit"] = audit_csv(rows, all_used, columns) if rows else {}
    for col, n in (ca.get("empty_values") or {}).items():
        blockers.append(f"{col!r} empty in {n}/{ca['rows']} rows — renders blank in the copy")
    for col, samples in (ca.get("dash_values") or {}).items():
        blockers.append(f"em/en-dash in {col!r} values, e.g. {samples[0]!r}")
    for col, info in (ca.get("shouty_values") or {}).items():
        warnings.append(f"{col!r} ALL CAPS in {info['count']} row(s) — reads as shouting, e.g. {info['samples'][:3]}")
    for col, words in (ca.get("spam_values") or {}).items():
        hits = ", ".join(f"{w} ({len(vs)}: {vs[0]!r})" for w, vs in words.items())
        warnings.append(f"spam words in {col!r} values — fix before upload: {hits}")
    for why, info in (ca.get("name_problems") or {}).items():
        warnings.append(f"first_name {why}: {info['count']} row(s), e.g. {info['samples'][:3]}")

    report.update(blockers=blockers, warnings=warnings, verdict=verdict(blockers, warnings))
    return report


def render_text(report: dict[str, Any]) -> None:
    print(f"\n=== Copy + variable check — {report['campaign']} ===")
    ca = report.get("csv_audit") or {}
    if ca:
        print(f"  {ca['rows']} lead rows · columns rendered by the copy: {ca['columns_rendered']}")
    for v in report["variants"]:
        print(f"\n  --- {v['label']} ---")
        print(f"  tags: {v['tags_used']}  spintax: {v['spin_blocks']} block(s) {v['spin_options']}")
        for s in v["samples"]:
            print(f"\n  → {s['to']}\n  SUBJECT: {s['subject']}")
            for line in s["body"].splitlines():
                print(f"    {line}")
    print(f"\n**{report['verdict']}** — {len(report['blockers'])} blocker(s), {len(report['warnings'])} warning(s)")
    for b in report["blockers"]:
        print(f"  BLOCK: {b}")
    for w in report["warnings"]:
        print(f"  WARN:  {w}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("campaign_id", nargs="?")
    ap.add_argument("--csv", type=Path, help="the lead CSV you are about to upload")
    ap.add_argument("--samples", type=int, default=3, help="rendered emails to print per variant")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="offline asserts on the pure logic")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.campaign_id or not args.csv:
        ap.error("campaign_id and --csv are required (or use --selftest)")
    if not args.csv.is_file():
        ap.error(f"CSV not found: {args.csv}")

    from sequencer import get_sequencer

    seq = get_sequencer()
    rows, columns = read_csv(args.csv)
    report = run(seq.campaign(args.campaign_id), rows, columns, seq.system_tags, args.samples)
    print(json.dumps(report, indent=2)) if args.json else render_text(report)
    return {"CLEAN": 0, "BLOCKED": 1, "WARNINGS": 2}[report["verdict"]]


def selftest() -> int:
    assert tags_used("hi {{firstName}} at {{company_name}}") == {"firstName", "company_name"}
    assert tags_used("{{RANDOM|Hi {{firstName}}|Hey {{firstName}}}}") == {"firstName"}
    assert resolves("firstName", set(), frozenset({"firstName"})) is True
    assert resolves("hook", {"hook"}) is True
    assert resolves("hookLine", {"hook_line"}) is True
    assert resolves("hook", set()) is False

    assert render("Hi {{first_name}}", {"first_name": "Ana"}) == "Hi Ana"
    assert render("Hi {{firstName}}", {"first_name": "Ana"}) == "Hi Ana"
    assert render("{{RANDOM|Hi|Hey}} {{first_name}}", {"first_name": "Ana"}) == "Hi Ana"
    assert render("Hi {{nope}}", {}) == "Hi [nope?]"

    assert name_problem("Sam") is None
    assert name_problem("Mary Jo") is None
    assert name_problem("O'Brien") is None
    assert name_problem("SAM") == "ALL CAPS"
    assert name_problem("sam") == "all lowercase"
    assert name_problem("Dr Sam") == "leading title (Dr)"
    assert name_problem("Acme Travel LLC") is not None
    assert name_problem("Sam2") == "contains digits"
    assert name_problem("a@b.com") == "looks like an email/url"
    assert name_problem("") == "empty"
    assert name_problem("E") == "single character"

    assert dash_hits("a — b") == ["em-dash"]
    assert dash_hits("a - b") == []
    assert lowercase_sentence_starts("Hello there. saw your post") == ["saw your post"]
    assert lowercase_sentence_starts("Hello there. Saw your post") == []
    assert lowercase_sentence_starts("We sell into the U.S. market today") == []
    assert lowercase_sentence_starts("Cheap flights, e.g. to Rome") == []
    assert lowercase_sentence_starts("You met Dr. smith") == []
    assert lowercase_sentence_starts("Thanks. P.S. happy to run it") == ["happy to run it"]
    assert lowercase_sentence_starts("Nice work! saw your post") == ["saw your post"]

    assert shouty_values([{"c": "ACME TRAVEL GROUP"}, {"c": "PRESIDENT"}, {"c": "TX"}, {"c": "Acme"}], "c") == [
        "ACME TRAVEL GROUP", "PRESIDENT"]
    assert spam_values([{"c": "Acme Discount Tours"}, {"c": "Acme Tours"}, {"c": "ACME TOURS"}], "c") == {
        "discount": ["Acme Discount Tours"]}

    assert strip_html("<p>a</p><p>b</p>") == "a\n\nb"
    assert sample_rows([{"i": str(i)} for i in range(10)], 3) == [{"i": "0"}, {"i": "3"}, {"i": "6"}]
    assert paste_artifacts("we found you on LinkedIn") == []
    assert paste_artifacts("focus on growingAcme Vacations") == ["growingAcme"]

    a = audit_variant("Hi {{firstName}}", "<p>re {{hook}} — ok</p>", set())
    assert a["unresolvable_tags"] == ["firstName", "hook"] and a["dashes"] == ["em-dash"]

    # end to end on a fake campaign: blank column blocks, spam word in a value warns
    camp = {"name": "t", "steps": [[{"subject": "hi {{first_name}}",
                                      "body": "<p>{{RANDOM|Hi|Hey}} {{first_name}}, saw {{company_name}}.</p>"}]]}
    rows = [{"email": "a@x.com", "first_name": "Ana", "company_name": "Acme Discount Tours"},
            {"email": "b@y.com", "first_name": "Bo", "company_name": ""}]
    rep = run(camp, rows, {"email", "first_name", "company_name"}, frozenset(), 2)
    assert rep["verdict"] == "BLOCKED"
    assert any("'company_name' empty in 1/2" in b for b in rep["blockers"])
    assert any("spam words in 'company_name'" in w and "discount" in w for w in rep["warnings"])
    print("selftest OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
