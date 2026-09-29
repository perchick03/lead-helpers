// node test_checks.js — same fixtures as the Python selftests, so the JS port can't drift silently.
const assert = require("assert"), fs = require("fs"), C = require("./js/checks.js");
const words = JSON.parse(fs.readFileSync(__dirname + "/spam_words.json", "utf8"));
const spam = C.makeSpam(words), hits = (t, s) => spam.lint(t, s).map((f) => f.hit);

// spam_lint.py _selfcheck
assert.deepStrictEqual(spam.lint("if I could get you more inbound DMC enquiries,"), []);
assert.deepStrictEqual(spam.lint("feel free to ignore this one"), []);
assert.deepStrictEqual(spam.lint("we handle DCAA FAR compliance"), []);
assert(hits("totally risk-free").includes("risk free"));
assert(hits("as seen in USA TODAY").includes("ALL CAPS: USA TODAY"));
assert(hits("earn $$$ fast").includes("$$$") && hits("quick question 🚀").includes("emoji"));
assert(hits("hi there! great news!").includes("multiple !") && hits("get it F R E E now").includes("s p a c e d letters"));
assert(hits("see https://bit.ly/x").includes("link shortener") && hits("http://a.com and http://b.com").includes("2 links"));
assert(hits("the go-to for Málaga").includes("non-ascii letter") && !hits("the go-to for Malaga").length);
assert(hits("Re: our chat", true).includes("fake RE:/FW: prefix") && hits("Quick Question About Logistics", true).includes("Title Case"));
assert.deepStrictEqual(spam.lint("quick question on your dmc mix", true), []);
assert.strictEqual(spam.check("quick question", "worth a look?").verdict, "clean");
assert.strictEqual(spam.check("Act Now", "click here for a free trial").verdict, "block");

// copy vs columns
const sys = new Set(["firstName"]);
const cc = C.copyCheck([[{ subject: "quick q", body: "<p>Hi {{firstName}}, {{RANDOM|a|b}} {{company_name}} {{ghost}}</p>" }]],
  [{ email: "x@y.com", company_name: "Acme Discount Tours" }, { email: "z@y.com", company_name: "" }], ["email", "company_name"], sys, spam);
const ids = cc.findings.map((f) => f.id);
assert(ids.includes("missing-column") && ids.includes("blank-values") && ids.includes("spam-values") && ids.includes("spintax"));
assert(cc.samples[0].body.includes("Acme Discount Tours") && cc.samples[0].body.includes("[ghost?]"));

// trends.py selftest
const mk = (ago, email, esp, sent, bounced, replies) => ({ date: new Date(Date.parse("2026-09-28") - ago * 864e5).toISOString().slice(0, 10), email, domain: email.split("@")[1], esp, sent, bounced, replies });
const rows = [];
for (let i = 0; i < 14; i++) { const n = i < 7;
  rows.push(mk(i, "a@good.com", "google", 30, 0, 1), mk(i, "b@good.com", "google", 30, 0, 1),
    mk(i, "c@burn.com", "microsoft", 30, n ? 5 : 0, n ? 0 : 1), mk(i, "d@ms.com", "microsoft", 30, n ? 4 : 0, 0)); }
const ids2 = new Set(C.trends(rows).findings.map((x) => x.id + "|" + x.group));
for (const k of ["bounce-up|domain burn.com", "domain-outlier|domain burn.com", "esp-gap|esp microsoft", "reply-down|domain burn.com"]) assert(ids2.has(k), k);
assert(![...ids2].some((k) => k.endsWith("domain good.com")));
assert.deepStrictEqual(C.trends(Array.from({ length: 14 }, (_, i) => mk(i, "a@x.com", "google", 30, 0, 1))).findings, []);
assert(C.trends(Array.from({ length: 14 }, (_, i) => mk(i, "a@x.com", "google", i < 7 ? 5 : 30, 0, 0))).findings.some((x) => x.id === "volume-drop"));
assert.strictEqual(C.trends([mk(0, "a@x.com", "google", 30, 0, 0)]).window, null);
assert.strictEqual(C.mergeHistory([mk(0, "a@x.com", "google", 10, 0, 0)], [mk(0, "a@x.com", "google", 12, 1, 0)]).length, 1);

// inbox rollup / classify (inbox_health.py selftest)
assert.strictEqual(C.classifyDomain({ sent: 400, bounced: 30, replies: 8 }).action, "recover");
assert.strictEqual(C.classifyDomain({ sent: 32, bounced: 3, replies: 2 }).action, "watch-lowvol");
assert.strictEqual(C.classifyDomain({ sent: 400, bounced: 30, replies: 8 }, false).action, null);
const fleet = ["a@d1.com", "b@d1.com", "c@d2.com", "x@idle.com"].map((email) => ({ email, daily_limit: 30 }));
const ru = C.rollup([{ date: "2026-07-28", email: "a@d1.com", sent: 10, bounced: 1, replies: 0 }], fleet, "2026-07-28");
assert(ru["d1.com"].capacity === 60 && ru["idle.com"].sent === 0 && ru["d1.com"].sent_last_day === 10);

// deliv_lint.py selftest
const g = [0, 1, 2].map((i) => ({ email: `a${i}@s.com`, status: "active", warmup_score: 99, daily_limit: 30, esp: "google" }));
const camp = { settings: { open_tracking: true, link_tracking: false, allow_risky: false, stop_on_reply: true, daily_limit: 90 } };
const target = { share: { gateway: 5 / 98, microsoft: 90 / 98, "no-mx": 3 / 98 }, leads_by_bucket: { gateway: 5 }, unresolved: 2, unresolved_domains: ["flaky.com"] };
let f = Object.fromEntries(C.infraLint(camp, g, target, null, null).map((x) => [x.id, x]));
assert(f["gateway-recipients"].sev === "red" && f["projected-bounce-rate"].sev === "red" && f["tracking-off"].sev === "orange" && f["list-hygiene-flags"].sev === "pass");
const lop = [...Array(150)].map((_, i) => ({ email: `g${i}@a.com`, bounced: false, esp: "google" })).concat([...Array(150)].map((_, i) => ({ email: `m${i}@b.com`, bounced: i < 30, esp: "microsoft" })));
f = Object.fromEntries(C.infraLint(camp, g, null, { sent: 1000, contacted: 1000, bounced: 40 }, lop).map((x) => [x.id, x]));
assert(f["live-bounce-rate"].sev === "red" && f["bounce-esp-gap"].sev === "orange" && f["bounce-esp-gap"].msg.includes("microsoft"));
assert.strictEqual(C.toCsv([{ a: 'x,"y"', b: 1 }], ["a", "b"]), 'a,b\r\n"x,""y""",1');

// proxy client: paginates until an empty page
(async () => {
  const pages = [{ items: [{ id: "1", status: 1 }], next_starting_after: "c1" }, { items: [], next_starting_after: "c2" }];
  const api = C.instantly("k", async () => ({ ok: true, status: 200, text: async () => JSON.stringify(pages.shift()) }));
  assert.strictEqual((await api.campaigns()).length, 1);
  console.log("checks.js: OK");
})();
