// Browser port of the Python checks. Pure logic, no DOM, so `node test_checks.js` runs it.
// Mirrors: spam_lint.py (words come from spam_words.json), copy_check.py (tags), inbox_health.py,
// trends.py, deliv_lint.py. Change a threshold there → change it here.
(function (root) {
  const pc = (x) => (x * 100).toFixed(1) + "%";
  const sum = (a, k) => a.reduce((s, r) => s + (r[k] || 0), 0);

  // ── Instantly, through the /api/instantly proxy (mirror of sequencer.py `Instantly`) ──
  function instantly(key, fetchFn = root.fetch && root.fetch.bind(root), endpoint = "/api/instantly") {
    async function call(method, path, params, body) {
      for (let attempt = 0; ; attempt++) {
        const r = await fetchFn(endpoint, {
          method: "POST", headers: { "content-type": "application/json", "x-api-key": key },
          body: JSON.stringify({ method, path, params, body }),
        });
        if ([429, 500, 502, 503, 504].includes(r.status) && attempt < 3) {
          await new Promise((ok) => setTimeout(ok, 3000 * 2 ** attempt));
          continue;
        }
        const text = await r.text();
        if (!r.ok) {
          const hint = r.status === 401 || r.status === 403 ? " — check the API key" : "";
          throw new Error(`Instantly ${path} → HTTP ${r.status}${hint}: ${text.slice(0, 160)}`);
        }
        return JSON.parse(text);
      }
    }
    // Cursor pages; Instantly sends a cursor on the last page too, so an empty page is the only safe stop.
    async function all(method, path, params, body) {
      const out = [];
      let cursor = null;
      for (let i = 0; i < 1000; i++) {
        const extra = { limit: 100, ...(cursor ? { starting_after: cursor } : {}) };
        const d = method === "GET" ? await call("GET", path, { ...params, ...extra }) : await call(method, path, null, { ...body, ...extra });
        const items = Array.isArray(d) ? d : d.items || [];
        out.push(...items);
        cursor = Array.isArray(d) ? null : d.next_starting_after;
        if (!cursor || !items.length) break;
      }
      return out;
    }
    const PROVIDER = { 2: "google", 3: "microsoft" }, LEAD_ESP = { 1: "google", 2: "microsoft" };
    return {
      campaigns: async () => (await all("GET", "/campaigns")).map((c) => ({ id: c.id, name: c.name || "", active: c.status === 1 })),
      async campaign(id) {
        const c = await call("GET", `/campaigns/${id}`);
        let senders = c.email_list || [];
        if (!senders.length && (c.email_tag_list || []).length) {
          const tags = new Set(c.email_tag_list);
          senders = (await all("GET", "/custom-tag-mappings")).filter((m) => m.resource_type === 1 && tags.has(m.tag_id)).map((m) => m.resource_id);
        }
        const steps = ((((c.sequences || [{}])[0]) || {}).steps) || [];
        return {
          id: c.id, name: c.name || "", senders,
          steps: steps.map((s) => (s.variants || []).map((v) => ({ subject: v.subject || "", body: v.body || "" }))),
          settings: { open_tracking: !!c.open_tracking, link_tracking: !!c.link_tracking, allow_risky: !!c.allow_risky_contacts,
            stop_on_reply: !!c.stop_on_reply, daily_limit: c.daily_limit ?? null },
        };
      },
      async accounts() {
        return (await all("GET", "/accounts")).filter((a) => a.email).map((a) => ({
          email: a.email, status: a.status === 1 ? "active" : (a.status || 0) < 0 ? "error" : "paused",
          warmup_score: a.stat_warmup_score ?? null, daily_limit: parseInt(a.daily_limit || 0, 10), esp: PROVIDER[a.provider_code] || "other" }));
      },
      async campaignStats(id) {
        const rows = await call("GET", "/campaigns/analytics");
        const r = (Array.isArray(rows) ? rows : rows.items || []).find((x) => x.campaign_id === id) || {};
        return { sent: r.emails_sent_count || 0, contacted: r.contacted_count || 0, bounced: r.bounced_count || 0 };
      },
      async campaignLeads(id) {
        return (await all("POST", "/leads/list", null, { campaign: id })).map((l) => ({ email: l.email || "", bounced: l.status === -1, esp: LEAD_ESP[l.esp_code] || "other" }));
      },
      async inboxDaily(emails, start, end) {
        const out = [];
        for (let i = 0; i < emails.length; i += 25) { // the endpoint 413s without an emails filter
          const rows = await call("GET", "/accounts/analytics/daily", { emails: emails.slice(i, i + 25).join(","), start_date: start, end_date: end });
          for (const r of Array.isArray(rows) ? rows : rows.items || [])
            out.push({ date: r.date || "", email: r.email_account || "", sent: +r.sent || 0, bounced: +r.bounced || 0, replies: +r.unique_replies || 0 });
        }
        return out;
      },
      systemTags: new Set(["email", "firstName", "lastName", "companyName", "website", "phone", "personalization", "sendingAccountFirstName",
        "sendingAccountLastName", "sendingAccountEmail", "accountSignature", "unsubscribeLink", "unsubscribe", "signature"]),
    };
  }

  // ── spam lint (spam_lint.py) ──
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pat = (p) => new RegExp((/^\w/.test(p) ? "\\b" : "") + esc(p) + (/\w$/.test(p) ? "\\b" : ""));
  const EMOJI = /[\u{1F000}-\u{1FAFF}☀-➿⬀-⯿️™®✅❌]/u;
  const URL_RE = /(?:https?:\/\/|www\.)\S+/gi;

  function makeSpam(words) {
    const high = words.high.map((p) => [p, pat(p)]), med = words.med.map((p) => [p, pat(p)]);
    const ok = new Set(words.acronyms);
    const norm = (t) => {
      t = t.toLowerCase().replace(/[’‘]/g, "'").replace(/[-–—_]+/g, " ").replace(/\s+/g, " ");
      for (const e of words.exempt) t = t.split(e).join(" ");
      return t;
    };
    function lint(text, isSubject = false) {
      const t = text || "", low = norm(t), out = [];
      const add = (sev, cat, hit, note = "") => out.push({ sev, cat, hit, note });
      for (const [p, rx] of high) if (rx.test(low)) add("high", "word", p);
      for (const [p, rx] of med) if (rx.test(low)) add("med", "context", p, "fine if the vertical justifies it");
      if (/!!+/.test(t) || (t.match(/!/g) || []).length > 1) add("high", "fmt", "multiple !", "cold email should carry at most one");
      if (/\$\$+/.test(t)) add("high", "fmt", "$$$");
      if (EMOJI.test(t)) add("high", "fmt", "emoji", "reads as marketing blast in B2B cold");
      if (/\b(?:[a-z]\s){3,}[a-z]\b/i.test(t)) add("high", "fmt", "s p a c e d letters", "classic filter-evasion signature");
      if (/<\s*(?:img|table|font|center|marquee|blink)\b/i.test(t)) add("high", "fmt", "html/image markup", "first touch must be plain text");
      for (const m of t.matchAll(/\b[A-Z]{2,}\b(?:\s+\b[A-Z]{2,}\b)+/g))
        if (!m[0].split(/\s+/).every((w) => ok.has(w))) { add("high", "fmt", "ALL CAPS: " + m[0].slice(0, 40)); break; }
      if ([...t].some((c) => /\p{L}/u.test(c) && c.charCodeAt(0) > 127)) add("med", "fmt", "non-ascii letter", "accent/foreign script — fold to ascii");
      const links = t.match(URL_RE) || [];
      if (links.some((u) => words.shorteners.some((s) => u.toLowerCase().includes(s)))) add("high", "fmt", "link shortener", "shorteners are a top-weighted spam signal");
      if (links.length > 3) add("high", "fmt", links.length + " links", "keep a cold first touch at 0-1");
      else if (links.length > 1) add("med", "fmt", links.length + " links", "0-1 is the cold-email norm");
      if (isSubject) {
        const s = t.trim(), w = s.split(/\s+/).filter(Boolean);
        if (/^\s*(re|fwd?)\s*:/i.test(s)) add("high", "subject", "fake RE:/FW: prefix", "deceptive threading");
        if (s.includes("!")) add("med", "subject", "exclamation", "no cold B2B subject needs one");
        if (s.length > 60) add("med", "subject", s.length + " chars", "mobile truncates around 33-43");
        if (w.length > 7) add("med", "subject", w.length + " words", "2-4 words open best");
        if (w.slice(1).filter((x) => x.length > 3 && /^[A-Z]/.test(x) && x !== x.toUpperCase()).length >= 2) add("med", "subject", "Title Case", "all-lowercase outperforms it by ~21%");
        if (s.endsWith("?") && w.length > 7) add("med", "subject", "long question", "reads as clickbait");
      }
      return out;
    }
    const score = (f, isSubject) => f.reduce((s, x) => s + (x.sev === "high" ? 3 : 1), 0) * (isSubject ? 2 : 1);
    const verdict = (n) => (n === 0 ? "clean" : n <= 3 ? "review" : n <= 7 ? "rewrite" : "block");
    function check(subject, body) {
      const sf = lint(subject, true), bf = lint(body), total = score(sf, true) + score(bf, false);
      return { score: total, verdict: verdict(total), subject: sf, body: bf };
    }
    return { lint, check };
  }

  // ── copy vs lead columns (copy_check.py) ──
  const TAG_RE = /\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g, RANDOM_RE = /\{\{\s*RANDOM\s*\|([^}]+)\}\}/g;
  const snake = (s) => s.replace(/(?<!^)(?=[A-Z])/g, "_").toLowerCase();
  const stripHtml = (h) => h.replace(/<(?:br|\/p|\/div|p|div)\b[^>]*>/gi, "\n").replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#39;|&apos;/g, "'").replace(/&quot;/g, '"')
    .replace(/\n{3,}/g, "\n\n").trim();
  const tagsUsed = (text) => new Set([...text.replace(RANDOM_RE, (_, o) => o).matchAll(TAG_RE)].map((m) => m[1]).filter((t) => t !== "RANDOM"));
  const resolves = (tag, cols, sys) => sys.has(tag) || [tag, tag.toLowerCase(), snake(tag)].some((c) => cols.has(c));
  const render = (text, lead) => text.replace(TAG_RE, (_, k) => [k, k.toLowerCase(), snake(k)].map((x) => lead[x]).find(Boolean) || `[${k}?]`)
    .replace(RANDOM_RE, (_, o) => o.split("|")[0]);

  // Copy findings: [{sev: red|orange|yellow|pass, id, msg}]. `campaign.steps` or a single pasted variant.
  function copyCheck(steps, rows, columns, sys, spam) {
    const f = [], add = (sev, id, msg) => f.push({ sev, id, msg });
    const cols = new Set(columns), allUsed = new Set(), samples = [];
    steps.forEach((variants, si) => variants.forEach((v, vi) => {
      const label = `step ${si + 1} variant ${String.fromCharCode(65 + vi)}`, body = stripHtml(v.body);
      const used = new Set([...tagsUsed(v.subject), ...tagsUsed(body)]);
      used.forEach((t) => allUsed.add(t));
      if (!body.trim()) add("red", "empty-body", `${label}: empty body`);
      if (!v.subject.trim() && si === 0) add("red", "empty-subject", `${label}: empty subject on the first email`);
      if (rows.length) { const bad = [...used].filter((t) => !resolves(t, cols, sys)).sort(); if (bad.length) add("red", "missing-column", `${label}: tags with no CSV column: ${bad.join(", ")}`); }
      const dashes = ["—", "–"].filter((d) => v.subject.includes(d) || body.includes(d));
      if (dashes.length) add("red", "dashes", `${label}: em/en-dash in copy`);
      const spins = (v.subject + body).match(RANDOM_RE) || [];
      if (spins.length < 2) add("yellow", "spintax", `${label}: ${spins.length} spintax block(s), under 2`);
      const sr = spam.check(v.subject, body);
      add(sr.verdict === "clean" ? "pass" : sr.verdict === "review" ? "yellow" : sr.verdict === "rewrite" ? "orange" : "red", "spam",
        `${label}: spam ${sr.verdict} (score ${sr.score})` + [...sr.subject.map((x) => "subject: " + x.hit), ...sr.body.map((x) => x.hit)].slice(0, 8).map((x) => `\n    · ${x}`).join(""));
      if (rows.length && !samples.length) samples.push({ label, subject: render(v.subject, rows[0]), body: render(body, rows[0]), to: rows[0].email || "?" });
    }));
    const usedCols = [...new Set([...allUsed].flatMap((t) => [t, t.toLowerCase(), snake(t)]).filter((c) => cols.has(c)))];
    for (const c of usedCols) {
      const empty = rows.filter((r) => !(r[c] || "").trim()).length;
      if (empty) add("red", "blank-values", `'${c}' empty in ${empty}/${rows.length} rows — renders blank in the copy`);
      const dash = rows.find((r) => /[—–]/.test(r[c] || ""));
      if (dash) add("red", "dash-values", `em/en-dash in '${c}' values, e.g. '${dash[c]}'`);
      const hits = {};
      for (const v of new Set(rows.map((r) => r[c] || "")))
        for (const x of spam.lint(v)) if (x.cat === "word" || x.cat === "context") (hits[x.hit] ||= []).push(v);
      const list = Object.entries(hits);
      if (list.length) add("yellow", "spam-values", `spam words in '${c}' values: ` + list.slice(0, 6).map(([w, vs]) => `${w} (${vs.length}: '${vs[0]}')`).join(", "));
    }
    return { findings: f, samples };
  }

  // ── inbox health (inbox_health.py) ──
  const dom = (e) => e.split("@").pop().toLowerCase();
  function rollup(rows, fleet, lastDay) {
    const per = {}, mk = () => ({ inboxes: 0, capacity: 0, sent: 0, bounced: 0, replies: 0, sent_last_day: 0 });
    for (const a of fleet) { const m = (per[dom(a.email)] ||= mk()); m.inboxes++; m.capacity += a.daily_limit || 0; }
    for (const r of rows) {
      if (!r.email.includes("@")) continue;
      const m = (per[dom(r.email)] ||= mk());
      m.sent += r.sent; m.bounced += r.bounced; m.replies += r.replies;
      if (lastDay && r.date === lastDay) m.sent_last_day += r.sent;
    }
    return per;
  }
  const lastSendDay = (rows) => rows.filter((r) => r.sent > 0 && r.date).map((r) => r.date).sort().pop() || null;
  function classifyDomain(m, active = true) {
    const { sent, bounced, replies } = m, bnc = sent ? bounced / sent : 0, rpl = sent ? replies / sent : 0;
    let bv, action = null;
    if (bnc > 0.05 && sent >= 150 && bounced >= 5) { bv = "RED"; action = "recover"; }
    else if (bnc > 0.05) { bv = "NOTE"; action = "watch-lowvol"; }
    else if (bnc >= 0.02) { bv = "NOTE"; action = "watch"; }
    else bv = "OK";
    const rv = sent < 150 ? "n/a" : rpl >= 0.026 ? "great" : rpl >= 0.01 ? "look-into" : rpl >= 0.008 ? "low" : "worry";
    return { ...m, active, bounce_pct: bnc, reply_pct: rpl, bounce_verdict: bv, reply_verdict: rv, action: active ? action : null };
  }
  function allocation(domains) {
    const v = Object.values(domains), act = v.filter((m) => m.active), cap = sum(act, "capacity"), used = sum(v, "sent_last_day");
    return { active_domains: act.length, active_capacity: cap, parked_domains: v.length - act.length, sent_last_day: used, utilisation: cap ? used / cap : 0 };
  }

  // ── trends (trends.py) ──
  const T = { WINDOW: 7, MIN_SENT: 150, MIN_DOMAIN_SENT: 100, BOUNCE_MIN: 0.02, BOUNCE_JUMP: 0.01, BOUNCE_RED: 0.05, REPLY_DROP: 0.6, REPLY_FLOOR: 0.01, X: 2 };
  const agg = (rows) => { const s = sum(rows, "sent"), b = sum(rows, "bounced"), p = sum(rows, "replies"); return { sent: s, bounced: b, replies: p, bounce: s ? b / s : 0, reply: s ? p / s : 0 }; };
  const addDays = (iso, n) => new Date(Date.parse(iso + "T00:00:00Z") + n * 864e5).toISOString().slice(0, 10);
  function trends(rows) {
    const days = [...new Set(rows.filter((r) => r.sent > 0).map((r) => r.date))].sort();
    if (days.length < 2) return { window: null, findings: [], note: "not enough history yet (need sending days on record)" };
    const end = days[days.length - 1], cut = addDays(end, -T.WINDOW), cut2 = addDays(end, -2 * T.WINDOW);
    const recent = rows.filter((r) => r.date > cut), prior = rows.filter((r) => r.date > cut2 && r.date <= cut);
    const f = [], add = (sev, id, group, msg) => f.push({ sev, id, group, msg });
    const keyed = {};
    for (const [tag, src] of [["recent", recent], ["prior", prior]])
      for (const r of src) for (const k of [["fleet", "fleet"], ["esp", r.esp], ["domain", r.domain]]) ((keyed[k.join("|")] ||= { kind: k[0], key: k[1], recent: [], prior: [] })[tag]).push(r);
    const stat = Object.values(keyed).map((g) => ({ ...g, rc: agg(g.recent), pr: agg(g.prior) })).sort((a, b) => (a.kind + a.key < b.kind + b.key ? -1 : 1));
    for (const { kind, key, rc, pr } of stat) {
      const label = kind === "fleet" ? key : `${kind} ${key}`, floor = kind === "domain" ? T.MIN_DOMAIN_SENT : T.MIN_SENT;
      if (rc.sent >= floor && pr.sent >= floor && rc.bounce >= T.BOUNCE_MIN && rc.bounce - pr.bounce >= T.BOUNCE_JUMP)
        add(rc.bounce > T.BOUNCE_RED ? "red" : "orange", "bounce-up", label, `${label}: bounce ${pc(pr.bounce)} → ${pc(rc.bounce)} (${rc.bounced}/${rc.sent} sent this week)`);
      if (pr.sent >= T.MIN_SENT && rc.sent >= T.MIN_SENT && pr.reply >= T.REPLY_FLOOR && rc.reply < T.REPLY_DROP * pr.reply)
        add("orange", "reply-down", label, `${label}: reply ${pc(pr.reply)} → ${pc(rc.reply)}`);
      if (pr.sent >= T.MIN_SENT && rc.sent <= pr.sent * 0.5) add("orange", "volume-drop", label, `${label}: sent ${pr.sent} → ${rc.sent} — paused or disconnected inboxes?`);
    }
    for (const { kind, key, rc } of stat) {
      if (kind !== "domain" || rc.sent < T.MIN_DOMAIN_SENT) continue;
      const rest = agg(recent.filter((r) => r.domain !== key));
      if (rc.bounce >= T.BOUNCE_MIN + T.BOUNCE_JUMP && rc.bounce >= T.X * Math.max(rest.bounce, 0.005))
        add(rc.bounce > T.BOUNCE_RED ? "red" : "orange", "domain-outlier", `domain ${key}`, `${key}: bounce ${pc(rc.bounce)} vs ${pc(rest.bounce)} for the rest of the fleet`);
    }
    const e = Object.fromEntries(stat.filter((s) => s.kind === "esp" && s.rc.sent >= T.MIN_SENT).map((s) => [s.key, s.rc])), names = Object.keys(e).sort();
    for (const a of names) for (const b of names) {
      if (a >= b) continue;
      let [hi, lo] = e[a].bounce >= e[b].bounce ? [a, b] : [b, a];
      if (e[hi].bounce >= T.BOUNCE_MIN + T.BOUNCE_JUMP && e[hi].bounce >= T.X * Math.max(e[lo].bounce, 0.005))
        add("orange", "esp-gap", `esp ${hi}`, `${lo} inboxes deliver (${pc(e[lo].bounce)} bounce) while ${hi} inboxes don't (${pc(e[hi].bounce)}) — ${hi} recipients or ${hi}-hosted senders are the problem`);
      [hi, lo] = e[a].reply >= e[b].reply ? [a, b] : [b, a];
      if (e[lo].reply * T.X <= e[hi].reply && e[hi].reply >= T.REPLY_FLOOR)
        add("yellow", "esp-gap", `esp ${lo}`, `${lo} inboxes get ${pc(e[lo].reply)} replies vs ${pc(e[hi].reply)} on ${hi} — likely landing in spam on ${lo}`);
    }
    return { window: { recent: [cut, end], prior: [cut2, cut], days_on_record: days.length }, findings: f };
  }

  // ── infra lint (deliv_lint.py) ──
  const BUCKETS = ["microsoft", "google", "gateway", "other", "no-mx"];
  function projectedBounce(target, fleet, lowWarm) {
    const nonMs = Object.entries(fleet).filter(([e]) => e !== "microsoft").reduce((s, [, v]) => s + v, 0);
    const c = { "no-mx": target["no-mx"] || 0, gateway: (target.gateway || 0) * 0.4, "cross-to-m365": (target.microsoft || 0) * nonMs * 0.04, unwarmed: lowWarm ? 0.01 : 0 };
    c.total = Object.values(c).reduce((s, v) => s + v, 0);
    return c;
  }
  // target = {share, leads_by_bucket, unresolved, unresolved_domains} from the MX sweep, or null
  function infraLint(camp, fleet, target, stats, leads) {
    const f = [], add = (sev, id, msg) => f.push({ sev, id, msg });
    const n = fleet.length || 1, fshare = {};
    for (const a of fleet) fshare[a.esp] = (fshare[a.esp] || 0) + 1 / n;
    const ts = target ? target.share : {};
    const low = fleet.filter((a) => a.status === "active" && typeof a.warmup_score === "number" && a.warmup_score < 90);
    if (target) {
      let mix = BUCKETS.map((b) => `${b}=${pc(ts[b] || 0)}`).join("  ");
      if (target.unresolved) {
        mix += `  (+${target.unresolved} leads unresolved, excluded)`;
        add("yellow", "dns-unresolved", `${target.unresolved} leads on ${target.unresolved_domains.length} domains never resolved — NOT counted as no-mx. Re-run to settle them`);
      }
      add("info", "target-mx-mix", mix);
      const gw = target.leads_by_bucket.gateway || 0;
      add((ts.gateway || 0) > 0.02 ? "red" : "pass", "gateway-recipients", gw ? `${gw} leads (${pc(ts.gateway)}) behind a secure gateway — strip or segment them` : "no gateway-fronted recipients");
    } else add("skip", "target-mx-mix", "no lead CSV — recipient MX checks skipped");
    if (fleet.length && target) {
      const pb = projectedBounce(ts, fshare, low.length > 0);
      add(pb.total > 0.03 ? "red" : "pass", "projected-bounce-rate", `≈ ${pc(pb.total)}${pb.total > 0.05 ? " — STOP, do not send" : ""}  [no-mx ${pc(pb["no-mx"])} + gateway ${pc(pb.gateway)} + cross→M365 ${pc(pb["cross-to-m365"])} + unwarmed ${pc(pb.unwarmed)}] (estimate)`);
    } else add("skip", "projected-bounce-rate", "needs both a resolved fleet and a lead CSV");
    if (stats && stats.sent) {
      const rate = stats.contacted ? stats.bounced / stats.contacted : 0;
      add(rate > 0.03 ? "red" : rate > 0.02 ? "orange" : "pass", "live-bounce-rate", `≈ ${pc(rate)} ACTUAL (${stats.bounced} bounced / ${stats.contacted} contacted)`);
      const pool = leads || [], r = {};
      for (const e of ["google", "microsoft"]) { const t = pool.filter((l) => l.esp === e); if (t.length >= 100) r[e] = t.filter((l) => l.bounced).length / t.length; }
      const ks = Object.keys(r);
      if (ks.length === 2) {
        const [hi, lo] = r[ks[0]] >= r[ks[1]] ? ks : [ks[1], ks[0]], gap = r[hi] >= 0.03 && r[hi] >= 2 * Math.max(r[lo], 0.005);
        add(gap ? "orange" : "info", "bounce-esp-gap", `bounce by recipient ESP: ${hi} ${pc(r[hi])} vs ${lo} ${pc(r[lo])}` + (gap ? ` — the fleet delivers to ${lo} but not ${hi}` : ""));
      }
    }
    if (fleet.length) {
      const dead = fleet.filter((a) => a.status === "error").map((a) => a.email);
      add(dead.length ? "orange" : "pass", "dead-inboxes", dead.length ? `${dead.length} inboxes in connection/send error: ${dead.slice(0, 5).join(", ")}` : "no dead inboxes in the fleet");
      add(low.length ? "orange" : "pass", "low-warmup-score", low.length ? `${low.length} active inboxes warmup<90: ${low.slice(0, 5).map((a) => a.email + "=" + a.warmup_score).join(", ")}` : "all active inboxes warmup≥90 (or no score reported)");
      const per = {}; for (const a of fleet) per[dom(a.email)] = (per[dom(a.email)] || 0) + 1;
      const over = Object.entries(per).filter(([, c]) => c > 3);
      add(over.length ? "orange" : "pass", "inboxes-per-domain", over.length ? `${over.length} domains over 3 inboxes: ${over.slice(0, 5).map(([d, c]) => `${d}=${c}`).join(", ")}` : "all domains ≤3 inboxes");
    }
    const s = camp && camp.settings;
    if (s) {
      const track = ["open", "link"].filter((k) => s[k + "_tracking"]);
      add(track.length ? "orange" : "pass", "tracking-off", track.length ? `${track.join("+")} tracking ON (adds a pixel / rewrites links — turn OFF for cold)` : "open+link tracking OFF");
      const h = [];
      if (s.allow_risky) h.push(["orange", "allow-risky ON (sends to unverified leads → bounces)"]);
      if (Number.isInteger(s.daily_limit) && fleet.length && s.daily_limit / fleet.length > 50) h.push(["orange", `daily limit ${s.daily_limit} over ${fleet.length} inboxes = ${Math.floor(s.daily_limit / fleet.length)}/inbox/day (>50)`]);
      if (!s.stop_on_reply) h.push(["yellow", "stop-on-reply OFF"]);
      add(h.some(([v]) => v === "orange") ? "orange" : h.length ? "yellow" : "pass", "list-hygiene-flags", h.length ? h.map(([, m]) => m).join(" · ") : "risky OFF, stop-on-reply ON");
    }
    return f;
  }
  const infraVerdict = (f) => (f.some((x) => x.sev === "red") ? "DO NOT SEND" : f.filter((x) => x.sev === "orange").length > 1 ? "FIX FIRST" : "INFRA OK");

  // ── csv ──
  const csvCell = (v) => (/[",\n\r]/.test(String(v ?? "")) ? `"${String(v).replace(/"/g, '""')}"` : String(v ?? ""));
  const toCsv = (rows, cols) => [cols.join(","), ...rows.map((r) => cols.map((c) => csvCell(r[c])).join(","))].join("\r\n");
  const HIST_COLS = ["date", "email", "domain", "esp", "sent", "bounced", "replies"];
  // merge history rows; the later row wins on (date, email), like trends.py load()
  const mergeHistory = (...sets) => { const m = new Map(); for (const s of sets) for (const r of s) m.set(r.date + "|" + r.email, r); return [...m.values()].sort((a, b) => (a.date + a.email < b.date + b.email ? -1 : 1)); };

  root.Checks = { instantly, makeSpam, tagsUsed, resolves, render, stripHtml, snake, copyCheck, rollup, lastSendDay, classifyDomain, allocation, trends, infraLint, infraVerdict, projectedBounce, toCsv, mergeHistory, HIST_COLS, dom };
  if (typeof module !== "undefined") module.exports = root.Checks;
})(typeof window !== "undefined" ? window : globalThis);
