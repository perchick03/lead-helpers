"""One runnable check for the toggle logic — no DNS (do_mx=False)."""
from deliverability import check_rows


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
