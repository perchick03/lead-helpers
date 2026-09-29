"""The ONE file that talks to your sending tool (sequencer).

Every health check reads through `get_sequencer()`. Instantly is the default.

On another tool (Smartlead, EmailBison, Lemlist, ...):
  1. subclass `Sequencer` below and implement its six methods, returning the
     documented shapes (plain dicts, only the keys listed);
  2. return your class from `get_sequencer()`.
Nothing else in the kit changes. `Instantly` at the bottom is a worked example.

Everything here is READ-ONLY. The kit never writes to your sequencer.

Copy syntax: the checks read merge tags as `{{name}}` and spintax as
`{{RANDOM|a|b}}` (Instantly's syntax). If your tool writes `{FIRST_NAME}` or
`{a|b}`, convert the subject/body inside `campaign()` before returning them.

Keys come from the environment or a `.env` file next to this script
(copy `.env.example` to `.env`).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path


def _load_dotenv() -> None:
    """KEY=value lines from ./.env into os.environ (real env vars win). Stdlib only."""
    p = Path(__file__).resolve().with_name(".env")
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and not k.lstrip().startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()


class Sequencer:
    """What the health checks need from a sending tool. Implement all six reads."""

    # Merge tags your tool fills itself, never lead-CSV columns (e.g. firstName,
    # companyName, the sender's signature). copy_check won't ask the CSV for them.
    system_tags: frozenset = frozenset()

    def campaigns(self) -> list[dict]:
        """Every campaign: [{"id": str, "name": str, "active": bool}]."""
        raise NotImplementedError

    def campaign(self, campaign_id: str) -> dict:
        """One campaign:
        {"id": str, "name": str,
         "senders": [inbox emails attached to send this campaign],
         "steps": [[{"subject": str, "body": str}, ...variants of step 1], ...],
         "settings": {"open_tracking": bool, "link_tracking": bool,
                      "allow_risky": bool,      # sends to unverified/risky leads
                      "stop_on_reply": bool,
                      "daily_limit": int | None}}   # campaign-wide emails/day
        Bodies may be HTML. Merge tags + spintax in the Instantly syntax (see top)."""
        raise NotImplementedError

    def accounts(self) -> list[dict]:
        """Every sending inbox:
        [{"email": str, "status": "active" | "paused" | "error",
          "warmup_score": float | None,   # 0-100, None if the tool has none
          "daily_limit": int,             # this inbox's max cold emails/day
          "esp": "google" | "microsoft" | "other"}]   # who hosts the inbox"""
        raise NotImplementedError

    def campaign_stats(self, campaign_id: str) -> dict:
        """All-time totals: {"sent": int, "contacted": int, "bounced": int}."""
        raise NotImplementedError

    def campaign_leads(self, campaign_id: str) -> list[dict]:
        """Every lead: [{"email": str, "bounced": bool,
                         "esp": "google" | "microsoft" | "other" | ""}]
        esp = the recipient's mail host; "" if your tool doesn't report it."""
        raise NotImplementedError

    def inbox_daily_stats(self, emails: list[str], start: date, end: date) -> list[dict]:
        """Per inbox per day in [start, end]:
        [{"date": "YYYY-MM-DD", "email": str, "sent": int, "bounced": int,
          "replies": int}]   # replies = unique replies"""
        raise NotImplementedError


def get_sequencer() -> Sequencer:
    """Swap this line to use another tool: `return MyTool(os.environ["MYTOOL_API_KEY"])`."""
    key = os.environ.get("INSTANTLY_API_KEY")
    if not key:
        raise SystemExit("INSTANTLY_API_KEY not set. Put it in .env (see .env.example) or export it.")
    return Instantly(key)


# ───────────────────────────────────────────────────────────── Instantly (v2)


class Instantly(Sequencer):
    """Instantly API v2. Docs: https://developer.instantly.ai/api/v2"""

    BASE = "https://api.instantly.ai/api/v2"
    system_tags = frozenset({
        "email", "firstName", "lastName", "companyName", "website", "phone",
        "personalization", "sendingAccountFirstName", "sendingAccountLastName",
        "sendingAccountEmail", "accountSignature", "unsubscribeLink", "unsubscribe",
        "signature",
    })
    _STATUS = {1: "active", 2: "paused", 3: "paused"}  # negative codes = connection/send errors
    _PROVIDER = {2: "google", 3: "microsoft"}          # accounts.provider_code
    _LEAD_ESP = {1: "google", 2: "microsoft"}          # leads.esp_code

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        # Explicit User-Agent: Cloudflare in front of the API 403s urllib's default one (error 1010).
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            "User-Agent": "lead-helpers/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * 2 ** attempt)  # rate limit / flaky gateway: back off
                    continue
                hint = " (check INSTANTLY_API_KEY)" if e.code in (401, 403) else ""
                raise RuntimeError(f"Instantly {method} {path} -> HTTP {e.code}{hint}: {e.read()[:200]!r}")
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(5 * 2 ** attempt)

    def _all(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> list[dict]:
        """Every page of a cursor-paginated list. Instantly returns short pages AND
        a cursor on the last page, so the only safe stop is an empty page."""
        out, cursor = [], None
        for _ in range(1000):  # backstop against a runaway cursor
            extra = {"limit": 100, **({"starting_after": cursor} if cursor else {})}
            if method == "GET":
                d = self._call("GET", path, params={**(params or {}), **extra})
            else:
                d = self._call(method, path, body={**(body or {}), **extra})
            items = d if isinstance(d, list) else (d.get("items") or [])
            out += items
            cursor = d.get("next_starting_after") if isinstance(d, dict) else None
            if not cursor or not items:
                break
        return out

    def campaigns(self) -> list[dict]:
        return [{"id": c["id"], "name": c.get("name", ""), "active": c.get("status") == 1}
                for c in self._all("GET", "/campaigns")]

    def campaign(self, campaign_id: str) -> dict:
        c = self._call("GET", f"/campaigns/{campaign_id}")
        senders = list(c.get("email_list") or [])
        if not senders and c.get("email_tag_list"):  # senders attached by tag, not by email
            tags = set(c["email_tag_list"])
            senders = [m["resource_id"] for m in self._all("GET", "/custom-tag-mappings")
                       if m.get("resource_type") == 1 and m.get("tag_id") in tags]
        steps = ((c.get("sequences") or [{}])[0] or {}).get("steps") or []
        return {
            "id": c["id"], "name": c.get("name", ""),
            "senders": senders,
            "steps": [[{"subject": v.get("subject") or "", "body": v.get("body") or ""}
                       for v in s.get("variants") or []] for s in steps],
            "settings": {
                "open_tracking": bool(c.get("open_tracking")),
                "link_tracking": bool(c.get("link_tracking")),
                "allow_risky": bool(c.get("allow_risky_contacts")),
                "stop_on_reply": bool(c.get("stop_on_reply")),
                "daily_limit": c.get("daily_limit"),
            },
        }

    def accounts(self) -> list[dict]:
        return [{"email": a["email"],
                 "status": self._STATUS.get(a.get("status"), "error" if (a.get("status") or 0) < 0 else "paused"),
                 "warmup_score": a.get("stat_warmup_score"),
                 "daily_limit": int(a.get("daily_limit") or 0),
                 "esp": self._PROVIDER.get(a.get("provider_code"), "other")}
                for a in self._all("GET", "/accounts") if a.get("email")]

    def campaign_stats(self, campaign_id: str) -> dict:
        rows = self._call("GET", "/campaigns/analytics")
        rows = rows if isinstance(rows, list) else rows.get("items") or []
        r = next((x for x in rows if x.get("campaign_id") == campaign_id), {})
        return {"sent": r.get("emails_sent_count") or 0, "contacted": r.get("contacted_count") or 0,
                "bounced": r.get("bounced_count") or 0}

    def campaign_leads(self, campaign_id: str) -> list[dict]:
        # POST /leads/list — the filter field is `campaign`, NOT `campaign_id`
        # (an unknown field is silently ignored and you get every lead in the workspace).
        return [{"email": le.get("email", ""), "bounced": le.get("status") == -1,
                 "esp": self._LEAD_ESP.get(le.get("esp_code"), "other")}
                for le in self._all("POST", "/leads/list", body={"campaign": campaign_id})]

    def inbox_daily_stats(self, emails: list[str], start: date, end: date) -> list[dict]:
        out = []
        for i in range(0, len(emails), 25):  # the endpoint 413s without an emails filter; batch it
            rows = self._call("GET", "/accounts/analytics/daily", params={
                "emails": ",".join(emails[i:i + 25]),
                "start_date": start.isoformat(), "end_date": end.isoformat()})
            out += [{"date": r.get("date", ""), "email": r.get("email_account", ""),
                     "sent": int(r.get("sent") or 0), "bounced": int(r.get("bounced") or 0),
                     "replies": int(r.get("unique_replies") or 0)}
                    for r in (rows if isinstance(rows, list) else rows.get("items") or [])]
        return out
