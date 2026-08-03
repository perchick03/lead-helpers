#!/usr/bin/env python3
"""Free email-deliverability checks for a lead CSV — standalone, zero pip deps.

Runs the three FREE ($0, no API key) checks, faithfully ported from the
production lead pipeline (leads.pipeline.stages):

  1. bad-email-format — park non-empty emails that fail a basic format regex.
  2. mx               — resolve each unique recipient domain's MX once (DNS, via
                        `dig`); tag provider (microsoft|google|other) and PARK
                        domains with no MX (incl. RFC 7505 null-MX) or sitting
                        behind a Secure Email Gateway (Mimecast, Proofpoint,
                        Barracuda, …) — near-impossible to cold-inbox.
  3. domain-check     — PARK disposable mailbox domains; FLAG (keep) an
                        email↔company-domain mismatch.

Paid stages (verify / crawl / research) are intentionally excluded.

DEPENDENCIES
  • Python 3.9+  (standard library only — no pip install needed)
  • `dig`        (DNS lookup tool, used for MX). Preinstalled on macOS.
                 Debian/Ubuntu: `apt-get install dnsutils`.
                 Skip the MX check entirely with --no-mx if dig is unavailable.

USAGE
  python deliverability.py leads.csv
  python deliverability.py leads.csv -o checked.csv --split
  python deliverability.py leads.csv --email-col email --domain-col company_domain
  python deliverability.py leads.csv --dry-run        # summary only, write nothing

OUTPUT
  Appends three columns to every row and writes <input>_checked.csv:
    park_reason     — "" if it passes, else: bad-email-format | no-mx | gateway | disposable
    mx              — provider for survivors: microsoft | google | other | dns-error
                      ("" if parked/unchecked). dns-error = the resolver never
                      answered; the row is kept, not parked — re-run those.
    domain_mismatch — "yes" if the email domain doesn't match company_domain (non-blocking flag)
  With --split also writes <input>_clean.csv (park_reason == "") and <input>_parked.csv.
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 1. bad-email-format
# ─────────────────────────────────────────────────────────────────────────────
_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def is_bad_format(email: str) -> bool:
    """A non-empty email that fails the basic format regex. Empty → not bad here."""
    return bool(email) and _EMAIL_PATTERN.match(email) is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. mx — MX tag + reachability park (DNS via `dig`)
# ─────────────────────────────────────────────────────────────────────────────
# MX-host substrings that mean "behind a secure email gateway": near-impossible
# to cold-inbox, so park. Checked before the microsoft/google buckets.
# NOTE: "pphosted" is Proofpoint Enterprise; "ppe-hosted" is Proofpoint *Essentials*
# (SMB tier, *.ppe-hosted.com) — a distinct substring "pphosted" does NOT match,
# so both are listed. "mxthunder" is the MXThunder/SpamHero gateway.
_GATEWAY = ("mimecast", "pphosted", "ppe-hosted", "proofpoint", "barracuda",
            "iphmx", "ironport", "cisco", "messagelabs", "symanteccloud",
            "fortimail", "securence", "mxthunder")
_MX_TIMEOUT_S = 8
_MX_WORKERS = 12
_MX_ATTEMPTS = 3


def _mx_lookup(domain: str) -> tuple[str, list[str]]:
    """One `dig` run → (rcode, mx_hosts). rcode '' means the LOOKUP failed.

    `dig +short` was the old bug: it prints nothing (exit 0) for BOTH 'no MX
    record' and a timeout/SERVFAIL, so a rate-limited resolver silently parked
    live domains as dead. Asking for the header instead makes the difference
    visible — NOERROR/NXDOMAIN are answers, everything else is a failure.
    """
    try:
        out = subprocess.run(
            ["dig", "+noall", "+comment", "+answer", "MX", domain],
            capture_output=True, text=True, timeout=_MX_TIMEOUT_S, check=False,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return "", []
    m = re.search(r"status:\s*(\w+)", out)
    if not m:
        return "", []                       # no header → timed out / no resolver reached
    hosts = [f[-1] for f in (ln.split() for ln in out.splitlines())
             if len(f) >= 5 and f[3] == "mx"]
    return m.group(1), hosts


def classify_mx(domain: str) -> str:
    """Map a domain to: no-mx | gateway | microsoft | google | other | dns-error.

    dns-error is NOT a verdict — the resolver never answered. Those rows are
    left unparked so a DNS hiccup can't kill a live lead; re-run to resolve.
    """
    for attempt in range(_MX_ATTEMPTS):
        rcode, hosts = _mx_lookup(domain)
        if rcode in ("noerror", "nxdomain"):
            break
        if attempt + 1 < _MX_ATTEMPTS:
            time.sleep(0.4 * (attempt + 1))  # back off — the resolver is rate-limiting us
    else:
        return "dns-error"
    # No MX at all (NXDOMAIN / NODATA), or null MX (RFC 7505: a single '.' host).
    if not hosts or all(h == "." for h in hosts):
        return "no-mx"
    mx_hosts = " ".join(hosts)
    if any(g in mx_hosts for g in _GATEWAY):
        return "gateway"
    if "protection.outlook" in mx_hosts or "outlook.com" in mx_hosts:
        return "microsoft"
    if "aspmx.l.google" in mx_hosts or "googlemail" in mx_hosts or "google.com" in mx_hosts:
        return "google"
    return "other"


# ─────────────────────────────────────────────────────────────────────────────
# 3. domain-check — disposable park + email↔company-domain mismatch flag
# ─────────────────────────────────────────────────────────────────────────────
_DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "trashmail.com", "yopmail.com", "getnada.com", "throwawaymail.com", "sharklasers.com",
}

FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "me.com", "mac.com", "protonmail.com",
    "proton.me", "yandex.com", "mail.com", "gmx.com", "gmx.net",
})

_MIN_CONTAINS_STEM_LEN = 4
_MIN_FUZZY_STEM_LEN = 7
_MAX_FUZZY_DISTANCE = 2


def _norm_domain(d: str) -> str:
    d = d.strip().lower()
    return d[4:] if d.startswith("www.") else d


def _stem(d: str) -> str:
    """Alphanumeric-only lowercase stem of the domain root (TLD stripped) — so
    `gbg.com`/`gbg.net` share a stem and `montague-inn` == `montagueinn`."""
    root = d.rsplit(".", 1)[0] if "." in d else d
    return re.sub(r"[^a-z0-9]", "", root.lower())


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(cur[j] + 1, prev[j + 1] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def domain_matches(email_domain: str, company_domain: str) -> bool:
    e, c = _norm_domain(email_domain), _norm_domain(company_domain)
    if not e or not c:
        return False
    if e == c or e.endswith("." + c) or c.endswith("." + e):
        return True
    es, cs = _stem(e), _stem(c)
    if es == cs:
        return True
    shorter, longer = (es, cs) if len(es) <= len(cs) else (cs, es)
    if len(shorter) >= _MIN_CONTAINS_STEM_LEN and shorter in longer:
        return True
    if (len(es) >= _MIN_FUZZY_STEM_LEN and len(cs) >= _MIN_FUZZY_STEM_LEN
            and _levenshtein(es, cs) <= _MAX_FUZZY_DISTANCE):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────
def _email_domain(email: str) -> str:
    return email.split("@", 1)[1] if "@" in email else ""


def check_rows(rows: list[dict], email_col: str, domain_col: str, workers: int,
               do_mx: bool, do_format: bool = True, do_domain: bool = True) -> list[dict]:
    """Annotate each row in place with park_reason / mx / domain_mismatch.

    Each check is independently toggleable. The UI runs mx-only by default and
    lets the user opt into format + domain; the CLI keeps all three on.
    """
    emails = [(r.get(email_col) or "").strip().lower() for r in rows]
    cdoms = [(r.get(domain_col) or "").strip().lower() if domain_col else "" for r in rows]
    edoms = [_email_domain(e) for e in emails]

    # MX: one lookup per UNIQUE recipient domain. When the format check is on,
    # skip clearly-malformed emails (their domain is junk anyway).
    verdict: dict[str, str] = {}
    if do_mx:
        uniq = sorted({d for e, d in zip(emails, edoms)
                       if d and not (do_format and is_bad_format(e))})
        if uniq:
            # honour --workers as a CEILING: 25+ parallel digs is what got the
            # resolver to rate-limit us in the first place.
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(uniq)))) as ex:
                verdict = dict(zip(uniq, ex.map(classify_mx, uniq)))

    for r, email, edom, cdom in zip(rows, emails, edoms, cdoms):
        park, mx_tag, mismatch = "", "", ""
        if do_format and is_bad_format(email):         # (1) bad format wins, skips the rest
            park = "bad-email-format"
        else:
            if do_mx:                                  # (2) mx verdict
                v = verdict.get(edom, "")
                if v in ("no-mx", "gateway"):
                    park = v
                else:
                    mx_tag = v                         # microsoft|google|other ('' if unchecked)
            if do_domain:
                if edom in _DISPOSABLE:                # (3a) disposable → park
                    park = park or "disposable"
                # (3b) mismatch flag — non-blocking, only when both domains present,
                # email isn't a free-mail provider, and not disposable.
                if (email and cdom and edom and edom not in FREE_EMAIL_DOMAINS
                        and edom not in _DISPOSABLE and not domain_matches(edom, cdom)):
                    mismatch = "yes"
        r["park_reason"], r["mx"], r["domain_mismatch"] = park, mx_tag, mismatch
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Free deliverability checks for a lead CSV.")
    ap.add_argument("csv", help="input CSV (must have an email column)")
    ap.add_argument("-o", "--output", help="output CSV (default: <input>_checked.csv)")
    ap.add_argument("--email-col", default="email")
    ap.add_argument("--domain-col", default="company_domain",
                    help="company-domain column for the mismatch flag ('' to skip)")
    ap.add_argument("--workers", type=int, default=_MX_WORKERS)
    ap.add_argument("--no-mx", action="store_true", help="skip the DNS/MX check (no `dig` needed)")
    ap.add_argument("--no-format", action="store_true", help="skip the bad-email-format check")
    ap.add_argument("--no-domain", action="store_true",
                    help="skip the disposable park + company-domain mismatch flag")
    ap.add_argument("--split", action="store_true",
                    help="also write <input>_clean.csv and <input>_parked.csv")
    ap.add_argument("--dry-run", action="store_true", help="print summary only; write nothing")
    args = ap.parse_args()

    src = Path(args.csv)
    if not src.exists():
        raise SystemExit(f"CSV not found: {src}")
    do_mx = not args.no_mx
    if do_mx and shutil.which("dig") is None:
        raise SystemExit("`dig` not on PATH — install dnsutils, or pass --no-mx to skip the MX check")

    with src.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if args.email_col not in fieldnames:
        raise SystemExit(f"email column {args.email_col!r} not in CSV. Columns: {fieldnames}")
    domain_col = args.domain_col if args.domain_col in fieldnames else ""

    print(f"rows: {len(rows)} | mx-check: {'on' if do_mx else 'off'} | "
          f"mismatch-flag: {'on' if domain_col else 'off (no company_domain col)'}")
    rows = check_rows(rows, args.email_col, domain_col, args.workers, do_mx,
                      do_format=not args.no_format, do_domain=not args.no_domain)

    parked = [r for r in rows if r["park_reason"]]
    clean = [r for r in rows if not r["park_reason"]]
    print(f"\n=== park reasons ===")
    for reason, n in Counter(r["park_reason"] for r in parked).most_common():
        print(f"  {reason:18s} {n}")
    print(f"  {'(passes)':18s} {len(clean)}")
    if do_mx:
        print("\n=== mx provider (survivors) ===")
        for prov, n in Counter(r["mx"] for r in clean if r["mx"]).most_common():
            print(f"  {prov:18s} {n}")
        n_err = sum(1 for r in rows if r["mx"] == "dns-error")
        if n_err:
            print(f"\n!! {n_err} domains never resolved (dns-error) — kept, NOT parked.")
            print("   The resolver was failing, not the domain. Re-run to settle them.")
    n_mismatch = sum(1 for r in rows if r["domain_mismatch"])
    print(f"\ndomain_mismatch flagged (non-blocking): {n_mismatch}")
    print(f"\nTOTAL: {len(clean)} pass / {len(parked)} parked of {len(rows)}")

    if args.dry_run:
        print("\n[dry-run] no files written")
        return

    out = Path(args.output) if args.output else src.with_name(f"{src.stem}_checked.csv")
    out_fields = fieldnames + [c for c in ("park_reason", "mx", "domain_mismatch")
                               if c not in fieldnames]

    def _write(path: Path, data: list[dict]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=out_fields)
            w.writeheader()
            w.writerows(data)
        print(f"[saved] {path}  ({len(data)} rows)")

    _write(out, rows)
    if args.split:                                     # siblings of the OUTPUT, not the input
        _write(out.parent / f"{src.stem}_clean.csv", clean)
        _write(out.parent / f"{src.stem}_parked.csv", parked)


if __name__ == "__main__":
    main()
