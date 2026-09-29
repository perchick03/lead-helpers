#!/usr/bin/env python3
"""spam_lint.py — deterministic spam/deliverability lint for cold-email COPY. NO LLM.

The Python half of the two-pass copy check. FLAGS (never rewrites). The AI half —
tone, false urgency, phishing-shaped framing, and rescuing this script's context-
dependent MED hits — is `.claude/agents/spam-checker.md`, which runs this first.

Word list distilled from mailmeteor's "349+ spam words" (2026) + the coldoutbound
spam-word-checker + a finance-tuned list. Deliberately TIERED,
not flat: 2026 filters (Gmail/Yahoo/M365) weight sender reputation + engagement far
above any single token, and a flat ban list on words like `cost`, `check`, `bank`,
`offer`, `price`, `today` false-positives on every legit B2B line. So:

  HIGH (3 pts) — promo/pressure/phishing phrases ~never legit in B2B cold copy.
  MED  (1 pt)  — real words that are fine in context but accumulate risk. The
                 agent downgrades these when the vertical justifies them
                 (`insurance` for an insurance client is not a spam word).
  fmt          — formatting/structure: caps, emoji, links, HTML, shouting.

Subject-line hits count DOUBLE (subject is the highest-leverage surface and the
one most filters weight hardest).

  python spam_lint.py "your draft line"
  python spam_lint.py --subject "quick question" --body "$(cat draft.txt)"
  python spam_lint.py --file draft.md [--json]
  python spam_lint.py --csv pool_with_angles.csv [--col personalization_angle]
  python spam_lint.py --csv pool.csv --check someone@co.com
  python spam_lint.py --selfcheck
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- word lists

# HIGH — multi-word promo / pressure / scam / phishing. High precision by design:
# every entry is a phrase, so `free` alone never fires here but `free trial` does.
HIGH_PHRASES = [
    # money / offer
    "50% off", "100% free", "100% off", "100% guaranteed", "100% satisfied",
    "one hundred percent free", "avoid bankruptcy", "bad credit", "best bargain",
    "best deal", "best offer", "best price", "best rates", "big bucks", "big profit",
    "cash bonus", "cash out", "cents on the dollar", "claim your discount",
    "consolidate debt", "credit card offers", "double your", "drastically reduced",
    "earn cash", "earn extra income", "earn from home", "earn money", "earn per month",
    "earn per week", "easy income", "extra cash", "extra income", "fast cash",
    "financial freedom", "for free", "for just $", "free access", "free consultation",
    "free gift", "free hosting", "free info", "free investment", "free membership",
    "free money", "free preview", "free quote", "free trial", "full refund",
    "get out of debt", "get paid", "guaranteed deposit", "increase revenue",
    "increase sales", "increase traffic", "instant earnings", "instant income",
    "instant savings", "investment advice", "join millions", "lowest price",
    "make money", "make $", "million dollars", "money back guarantee", "no cost",
    "no credit check", "only $", "potential earnings", "price protection",
    "pure profit", "save $", "save big", "save big money", "save up to",
    "subject to credit", "why pay more", "your income",
    # scam / too-good-to-be-true
    "amazing deal", "amazing offer", "amazing stuff", "be amazed", "be your own boss",
    "cannot miss", "can't miss", "exclusive deal", "fantastic deal", "fantastic offer",
    "great news", "great offer", "guaranteed results", "important information",
    "incredible deal", "must read", "new customers only", "no catch", "no obligation",
    "no strings attached", "once in a lifetime", "once in lifetime",
    "only available here", "risk free", "satisfaction guaranteed", "special invitation",
    "special offer", "special promotion", "unbeatable offer",
    "will not believe", "you are a winner", "you will not believe your eyes",
    # urgency / pressure / CTA
    "access now", "act fast", "act immediately", "act now", "action required",
    "apply here", "apply now", "before it's too late", "buy direct", "buy now",
    "buy today", "call now", "call free", "cancel now", "cancellation required",
    "claim now", "click below", "click here", "click me to download", "click now",
    "click this link", "click to get", "click to remove", "contact us immediately",
    "deal ending soon", "do it now", "do it today", "don't delete", "don't hesitate",
    "don't waste time", "expires today", "final call", "for instant access",
    "get it now", "get started now", "hurry up", "info you requested",
    "information you requested", "limited time", "offer expires", "order now",
    "order today", "please read", "purchase now", "sign up free", "supplies are limited",
    "take action now", "this won't last", "time limited", "top urgent",
    "what are you waiting for", "while supplies last",
    # phishing / security-warning shaped
    "access your account", "account update", "activate now", "change password",
    "click to verify", "confidential information", "confirm your details",
    "data breach", "download now", "final notice", "immediate action required",
    "important update", "improve security", "install now", "last warning",
    "log in now", "new login detected", "password reset", "payment details needed",
    "phishing alert", "secure payment", "security breach", "security update",
    "update account", "verify identity", "warning message",
    # health / pharma / gambling / adult — never in our copy
    "100% natural", "adult content", "bet now", "casino bonus", "certified organic",
    "click to win", "cure for", "diet pill", "doctor recommended", "double blind study",
    "fat burner", "fast weight loss", "free chips", "free spins", "gamble online",
    "get slim", "guaranteed weight loss", "hair growth", "lose weight fast",
    "lottery winner", "medical breakthrough", "miracle cure", "natural remedy",
    "no prescription", "online betting", "online casino", "online gaming",
    "online pharmacy", "pain relief", "poker tournament", "prescription drugs",
    "reverse aging", "risk free bet", "safe and effective", "scientifically proven",
    "secret formula", "slots jackpot", "spin to win", "vip offer", "weight loss",
    "winner announced", "winning numbers", "youthful skin",
]

# HIGH single tokens — unambiguous. Bare `free`/`offer`/`cash` are MED, not here.
HIGH_WORDS = [
    "viagra", "casino", "lottery", "jackpot", "blackjack", "freebie", "cashcashcash",
    "xxx", "guaranteed", "guarantee", "telemarketing",
]

# MED — legit in the right vertical, risky in aggregate. The agent rescues these
# with context; the script only counts them.
MED_WORDS = [
    # promo-adjacent
    "free", "discount", "coupon", "promo", "bargain", "cheap", "giveaway", "prize",
    "refund", "savings",
    # finance vocabulary (legit for a finance/insurance client, spammy elsewhere)
    "bankruptcy", "debt", "earnings", "income", "insurance", "investment", "loans",
    "mortgage", "profits", "refinance",
    # urgency
    "asap", "deadline", "expires", "expiring", "hurry", "immediately", "urgent",
    # hype / AI-tell adjectives — what actually reads as "vendor ad" in 2026
    "amazing", "best in class", "cutting edge", "effortless", "elevate", "empower",
    "exponential", "fantastic", "game changer", "game changing", "groundbreaking",
    "hassle free", "incredible", "industry leading", "insane", "no brainer",
    "revolutionary", "seamless", "skyrocket", "state of the art", "supercharge",
    "transform", "turbocharge", "unbelievable", "unleash", "unlock", "win win",
    "world class",
]

# Stripped from the text BEFORE matching — legit idioms that contain a flagged token.
EXEMPT = ["feel free", "free up", "limited liability", "free of charge to you"]

# Acronyms that legitimately sit back-to-back and must not trip the shouting rule.
# Ambiguous 2-letter English words (IT/US/AR/AP/UP/GO/NO/ON) are deliberately OUT.
# ponytail: an allowlist can't cover every vertical — an unknown industry acronym pair
# ("USDA AMS") will false-positive as shouting. That's the AI pass's job to rescue, not
# a reason to ship a dictionary. Add terms here when a live campaign hits one.
_ACRONYM_OK = {
    # finance
    "IRS", "SEC", "FINRA", "GAAP", "IFRS", "EBITDA", "AICPA", "CPA", "CFP", "ROI",
    "KPI", "ESG", "IPO", "LBO", "NYSE", "KPMG", "PWC", "BDO", "RSM", "EY", "LLC",
    "LLP", "PLLC", "DCAA", "FAR", "GSA", "SBA", "ERC", "HSA", "FSA",
    # travel / DMC
    "DMC", "FIT", "MICE", "OTA", "GDS", "IATA", "ATOL", "ABTA", "ASTA", "USTOA",
    "AAA", "PAX", "FAM", "RFP", "RFQ",
    # freight / logistics / food supply
    "LTL", "FTL", "TSA", "FMCSA", "NVOCC", "TMS", "ELD", "POD",
    "USDA", "AMS", "FDA", "HACCP", "MSC", "ASC", "BAP", "NOAA", "FSMA", "COOL",
    # generic business
    "B2B", "B2C", "CRM", "ERP", "SEO", "SEM", "API", "SLA", "NDA", "CEO", "CFO",
    "COO", "CMO", "CTO", "VP", "HR", "SMB", "SME", "MBA", "USA", "UK", "EU", "AI",
}

_LINK_SHORTENERS = ("bit.ly", "tinyurl.com", "goo.gl", "t.co/", "ow.ly", "buff.ly",
                    "rebrand.ly", "cutt.ly", "is.gd", "shorturl.at", "lnkd.in")

# Emoji + trademark/decorative symbols. Not `isalpha`, so the non-ASCII-letter
# check below never double-counts these.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️™®✅❌]"
)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_SPACED_OUT = re.compile(r"\b(?:[a-z]\s){3,}[a-z]\b", re.I)  # "F R E E", "M O N E Y"
_HTML = re.compile(r"<\s*(?:img|table|font|center|marquee|blink)\b", re.I)

POINTS = {"high": 3, "med": 1}


def _pat(p: str) -> re.Pattern[str]:
    """Word-bounded pattern, but only where the edge char is alphanumeric ("only $")."""
    left = r"\b" if p[0].isalnum() else ""
    right = r"\b" if p[-1].isalnum() else ""
    return re.compile(left + re.escape(p) + right)


_HIGH_PATS = [(p, _pat(p)) for p in HIGH_PHRASES + HIGH_WORDS]
_MED_PATS = [(p, _pat(p)) for p in MED_WORDS]


def _norm(t: str) -> str:
    """Lowercase, straighten quotes, hyphens/dashes -> space, collapse whitespace,
    then delete the exempt idioms so `feel free` can't fire the `free` rule."""
    t = t.lower().replace("’", "'").replace("‘", "'")  # noqa: RUF001 — that IS the job
    t = re.sub(r"[-–—_]+", " ", t)  # noqa: RUF001
    t = re.sub(r"\s+", " ", t)
    for e in EXEMPT:
        t = t.replace(e, " ")
    return t


def lint(text: str, is_subject: bool = False) -> list[dict]:
    """Return findings: [{sev, cat, hit, note}]. Empty = clean."""
    t = text or ""
    low = _norm(t)
    out: list[dict] = []

    def add(sev: str, cat: str, hit: str, note: str = "") -> None:
        out.append({"sev": sev, "cat": cat, "hit": hit, "note": note})

    for p, rx in _HIGH_PATS:
        if rx.search(low):
            add("high", "word", p)
    for p, rx in _MED_PATS:
        if rx.search(low):
            add("med", "context", p, "fine if the vertical justifies it")

    # ---- formatting / structure
    if re.search(r"!!+", t) or t.count("!") > 1:
        add("high", "fmt", "multiple !", "cold email should carry at most one")
    if re.search(r"\$\$+", t):
        add("high", "fmt", "$$$")
    if _EMOJI.search(t):
        add("high", "fmt", "emoji", "reads as marketing blast in B2B cold")
    if _SPACED_OUT.search(t):
        add("high", "fmt", "s p a c e d letters", "classic filter-evasion signature")
    if _HTML.search(t):
        add("high", "fmt", "html/image markup", "first touch must be plain text")
    for run in re.finditer(r"\b[A-Z]{2,}\b(?:\s+\b[A-Z]{2,}\b)+", t):
        if not all(tok in _ACRONYM_OK for tok in run.group(0).split()):
            add("high", "fmt", f"ALL CAPS: {run.group(0)[:40]}")
            break
    if any(c.isalpha() and ord(c) > 127 for c in t):
        add("med", "fmt", "non-ascii letter", "accent/foreign script — fold to ascii")

    links = _URL.findall(t)
    if any(s in u.lower() for u in links for s in _LINK_SHORTENERS):
        add("high", "fmt", "link shortener", "shorteners are a top-weighted spam signal")
    if len(links) > 3:
        add("high", "fmt", f"{len(links)} links", "keep a cold first touch at 0-1")
    elif len(links) > 1:
        add("med", "fmt", f"{len(links)} links", "0-1 is the cold-email norm")

    # ---- subject-only structure (2026: 2-4 words / all-lowercase win on open rate)
    if is_subject:
        s = t.strip()
        if re.match(r"^\s*(re|fwd?)\s*:", s, re.I):
            add("high", "subject", "fake RE:/FW: prefix", "deceptive threading")
        if "!" in s:
            add("med", "subject", "exclamation", "no cold B2B subject needs one")
        if len(s) > 60:
            add("med", "subject", f"{len(s)} chars", "mobile truncates around 33-43")
        words = s.split()
        if len(words) > 7:
            add("med", "subject", f"{len(words)} words", "2-4 words open best")
        titled = [w for w in words[1:] if len(w) > 3 and w[:1].isupper() and not w.isupper()]
        if len(titled) >= 2:
            add("med", "subject", "Title Case", "all-lowercase outperforms it by ~21%")
        if s.endswith("?") and len(words) > 7:
            add("med", "subject", "long question", "reads as clickbait")

    return out


def score(findings: list[dict], is_subject: bool = False) -> int:
    mult = 2 if is_subject else 1
    return sum(POINTS[f["sev"]] for f in findings) * mult


def verdict(total: int) -> str:
    if total == 0:
        return "clean"
    if total <= 3:
        return "review"
    if total <= 7:
        return "rewrite"
    return "block"


_ICON = {"clean": "✅", "review": "🟡", "rewrite": "🟠", "block": "🔴"}


def check(subject: str = "", body: str = "") -> dict:
    """Full copy check. This is what the spam-checker agent consumes via --json."""
    sf, bf = lint(subject, is_subject=True), lint(body)
    total = score(sf, True) + score(bf)
    return {
        "score": total,
        "verdict": verdict(total),
        "subject": {"text": subject, "findings": sf, "score": score(sf, True)},
        "body": {"text": body, "findings": bf, "score": score(bf)},
    }


# ---------------------------------------------------------------------- cli

def _render(res: dict) -> None:
    v = res["verdict"]
    print(f"{_ICON[v]}  {v.upper()}  ·  score {res['score']}")
    for part in ("subject", "body"):
        fs = res[part]["findings"]
        if not res[part]["text"]:
            continue
        print(f"\n## {part} ({res[part]['score']} pts)")
        if not fs:
            print("  clean")
        for f in fs:
            note = f"  — {f['note']}" if f["note"] else ""
            print(f"  [{f['sev']:<4}] {f['cat']:<7} {f['hit']}{note}")


def _selfcheck() -> int:
    assert lint("if I could get you more inbound DMC enquiries,") == []
    assert lint("you've saved operators over $5 million in fees") == []
    assert lint("feel free to ignore this one") == []                     # exempt idiom
    assert lint("we handle DCAA FAR compliance") == []                    # acronym pair
    assert lint("worth a look at your DMC and MICE mix") == []
    assert any(f["hit"] == "risk free" for f in lint("totally risk-free"))  # hyphen norm
    assert any(f["hit"] == "guaranteed" for f in lint("guaranteed results"))
    assert any(f["hit"] == "ALL CAPS: USA TODAY" for f in lint("as seen in USA TODAY"))
    assert any(f["hit"] == "$$$" for f in lint("earn $$$ fast"))
    assert any(f["hit"] == "emoji" for f in lint("quick question 🚀"))
    assert any(f["hit"] == "multiple !" for f in lint("hi there! great news!"))
    assert any(f["hit"] == "s p a c e d letters" for f in lint("get it F R E E now"))
    assert any(f["hit"] == "link shortener" for f in lint("see https://bit.ly/x"))
    assert any("2 links" in f["hit"] for f in lint("http://a.com and http://b.com"))
    assert any(f["hit"] == "non-ascii letter" for f in lint("the go-to for Málaga"))
    assert lint("the go-to for Malaga") == []
    # subject-specific
    assert any("RE:" in f["hit"] for f in lint("Re: our chat", is_subject=True))
    assert any(f["hit"] == "Title Case" for f in lint("Quick Question About Logistics",
                                                      is_subject=True))
    assert lint("quick question on your dmc mix", is_subject=True) == []
    # scoring / verdict
    assert check("quick question", "worth a look?")["verdict"] == "clean"
    assert check("Act Now", "click here for a free trial")["verdict"] == "block"
    assert verdict(score(lint("we saw a discount trend"))) == "review"
    print("spam_lint selfcheck: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="a single line/body to lint")
    ap.add_argument("--subject", default="", help="subject line (hits count double)")
    ap.add_argument("--body", default="", help="email body")
    ap.add_argument("--file", help="draft file; a leading 'Subject: ...' line is split out")
    ap.add_argument("--csv", help="lint a column of a pool CSV")
    ap.add_argument("--col", default="personalization_angle", help="column for --csv")
    ap.add_argument("--check", metavar="EMAIL", help="with --csv: inspect one lead")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--export-words", action="store_true", help="print the word lists as JSON (spam_words.json, for the web UI)")
    a = ap.parse_args()
    if a.export_words:
        print(json.dumps({"high": HIGH_PHRASES + HIGH_WORDS, "med": MED_WORDS, "exempt": EXEMPT,
                          "acronyms": sorted(_ACRONYM_OK), "shorteners": list(_LINK_SHORTENERS)}, indent=1))
        return 0

    if a.selfcheck:
        return _selfcheck()

    if a.csv:
        with open(a.csv, newline="", encoding="utf-8-sig", errors="replace") as fh:
            df = list(csv.DictReader(fh))
        if a.check:
            rows = [r for r in df if (r.get("email") or "").lower() == a.check.lower()]
            if not rows:
                print(f"no row with email {a.check} in {a.csv}", file=sys.stderr)
                return 1
            for r in rows:
                res = check(body=r.get(a.col) or "")
                print(f"{a.check}  ({r.get('company_domain', '')})\n  {r.get(a.col, '')}")
                _render(res)
            return 0
        col = [r.get(a.col) or "" for r in df]
        by_val = {v: lint(v) for v in dict.fromkeys(col)}
        flagged = {v: f for v, f in by_val.items() if f}
        for v, f in flagged.items():
            hits = ", ".join(x["hit"] for x in f)
            print(f"  {_ICON[verdict(score(f))]} {hits}  (x{col.count(v)})\n      {v}")
        rows = sum(1 for v in col if by_val.get(v))
        print(f"\nflagged: {rows}/{len(df)} leads ({100 * rows / max(1, len(df)):.1f}%) · "
              f"{len(flagged)} distinct of {len(by_val)} unique.")
        return 0

    subject, body = a.subject, a.body or a.text or ""
    if a.file:
        raw = Path(a.file).read_text(encoding="utf-8")
        m = re.match(r"\s*subject\s*:\s*(.+?)\n(.*)", raw, re.I | re.S)
        subject, body = (m.group(1).strip(), m.group(2)) if m else (subject, raw)
    if not subject and not body:
        ap.print_help()
        return 1

    res = check(subject, body)
    print(json.dumps(res, indent=2, ensure_ascii=False)) if a.json else _render(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
