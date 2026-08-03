"""Runnable checks: toggle logic (no DNS) + the DNS-failure-is-not-a-verdict rule."""
import deliverability
from deliverability import check_rows, classify_mx


def test_dns_failure_is_not_no_mx():
    """A failed lookup must never render as the verdict 'no-mx' (the 2026 bug)."""
    calls = []

    def fake(rcode, hosts):
        deliverability._mx_lookup = lambda d: (calls.append(d), (rcode, hosts))[1]
        deliverability.time.sleep = lambda s: None

    fake("", [])                                  # timeout / no resolver reached
    assert classify_mx("flowersfoods.com") == "dns-error"
    assert len(calls) == deliverability._MX_ATTEMPTS  # retried, not one-shot
    fake("servfail", [])
    assert classify_mx("flowersfoods.com") == "dns-error"
    fake("noerror", [])                           # NODATA — a real answer
    assert classify_mx("example.com") == "no-mx"
    fake("nxdomain", [])                          # domain doesn't exist
    assert classify_mx("nope.invalid") == "no-mx"
    fake("noerror", ["."])                        # RFC 7505 null MX
    assert classify_mx("nomail.com") == "no-mx"
    fake("noerror", ["flowersfoods-com.mail.protection.outlook.com."])
    assert classify_mx("flowersfoods.com") == "microsoft"
    fake("noerror", ["us-smtp-inbound-1.mimecast.com."])
    assert classify_mx("x.com") == "gateway"
    print("ok — dns failures stay dns-error")


def test_toggles():
    rows = [
        {"email": "ceo@acme.com", "company_domain": "acme.com"},
        {"email": "not-an-email", "company_domain": ""},
        {"email": "x@mailinator.com", "company_domain": ""},
        {"email": "bob@otherco.io", "company_domain": "acme.com"},
        {"email": "", "company_domain": "acme.com"},
    ]

    # mx-only (format + domain OFF): nothing parked, no mismatch flagged.
    r = check_rows([dict(x) for x in rows], "email", "company_domain", 25,
                   do_mx=False, do_format=False, do_domain=False)
    assert all(x["park_reason"] == "" for x in r)
    assert all(x["domain_mismatch"] == "" for x in r)

    # format ON: only the malformed one is parked.
    r = check_rows([dict(x) for x in rows], "email", "company_domain", 25,
                   do_mx=False, do_format=True, do_domain=False)
    assert [x["park_reason"] for x in r] == ["", "bad-email-format", "", "", ""]

    # domain ON: disposable parked, real-vs-acme mismatch flagged (not free/disposable).
    r = check_rows([dict(x) for x in rows], "email", "company_domain", 25,
                   do_mx=False, do_format=False, do_domain=True)
    assert r[2]["park_reason"] == "disposable"
    assert r[3]["domain_mismatch"] == "yes"   # otherco.io != acme.com
    assert r[0]["domain_mismatch"] == ""      # acme == acme
    print("ok")


if __name__ == "__main__":
    test_toggles()
    test_dns_failure_is_not_no_mx()
