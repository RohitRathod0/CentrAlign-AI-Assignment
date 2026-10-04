"""
WHAT: LedgerOps behaviour: UI create flow, validation, duplicate warning,
      REST API status codes and auth, mark-paid.

WHY:  LedgerOps is the ground truth for verification and eval; if its rules
      drift (e.g. duplicates silently accepted), every agent result built on
      it is meaningless.
"""
from tests.conftest import API_KEY

VALID = {"vendor_id": "1", "invoice_number": "INV-2026-1047", "amount": "48,250.00",
         "currency": "INR", "due_date": "2026-10-31", "notes": "test"}


def test_ui_requires_login(ledgerops):
    resp = ledgerops.get("/invoices", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/login"


def test_ui_create_redirects_to_detail_with_banner(ledgerops_user):
    resp = ledgerops_user.post("/invoices", data=VALID)
    assert "/invoices/6?created=1" in str(resp.url)
    assert "Invoice LO-0006 created" in resp.text


def test_ui_validation_errors_are_alerts(ledgerops_user):
    resp = ledgerops_user.post("/invoices", data={**VALID, "invoice_number": "bad no", "amount": "-5",
                                                  "due_date": "2026-01-01"})
    assert resp.text.count('class="field-error" role="alert"') == 3
    assert ledgerops_user.get("/api/invoices", params={"invoice_number": "bad no"}, headers=API_KEY).json() == []


def test_ui_duplicate_shows_warning_and_does_not_save(ledgerops_user):
    resp = ledgerops_user.post("/invoices", data={**VALID, "invoice_number": "INV-2026-1004"})
    assert 'id="duplicate-warning"' in resp.text and "LO-0005" in resp.text
    assert len(ledgerops_user.get("/api/invoices", params={"invoice_number": "INV-2026-1004"},
                                  headers=API_KEY).json()) == 1


def test_api_auth_create_duplicate_validation(ledgerops):
    body = {"vendor_id": 1, "invoice_number": "INV-2026-1047", "amount": 48250.0, "currency": "INR",
            "due_date": "2026-10-31"}
    assert ledgerops.post("/api/invoices", json=body).status_code == 401
    created = ledgerops.post("/api/invoices", json=body, headers=API_KEY)
    assert created.status_code == 201 and created.json()["record_id"] == "LO-0006"
    dup = ledgerops.post("/api/invoices", json=body, headers=API_KEY)
    assert dup.status_code == 409 and dup.json() == {"error": "duplicate", "existing_id": 6}
    bad = ledgerops.post("/api/invoices", json={**body, "invoice_number": "X", "vendor_id": 99}, headers=API_KEY)
    assert bad.status_code == 422 and set(bad.json()["fields"]) == {"invoice_number", "vendor_id"}


def test_api_get_and_filters(ledgerops):
    assert [v["name"] for v in ledgerops.get("/api/vendors", headers=API_KEY).json()][:2] == ["Acme Corp", "Acme Logistics"]
    unpaid = ledgerops.get("/api/invoices", params={"status": "unpaid"}, headers=API_KEY).json()
    assert [i["invoice_number"] for i in unpaid] == ["INV-2026-1004"]
    assert ledgerops.get("/api/invoices/999", headers=API_KEY).status_code == 404


def test_mark_paid(ledgerops_user):
    resp = ledgerops_user.post("/invoices/5/mark-paid")
    assert "marked as paid" in resp.text
    assert ledgerops_user.get("/api/invoices/5", headers=API_KEY).json()["status"] == "paid"
