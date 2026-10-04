"""
WHAT: Each chaos fault (PRD 6.3) fires only with chaos on, exactly once per
      reset, at the documented trigger.

WHY:  The reliability demo depends on faults being reproducible. If a fault
      fired twice or never, the agent's recovery could not be shown on camera
      or compared across eval runs.
"""
import time

from mock_env.maildesk.app import chaos as maildesk_chaos
from tests.conftest import API_KEY, CHAOS

FORM = {"vendor_id": "1", "invoice_number": "INV-2026-1047", "amount": "48250.00",
        "currency": "INR", "due_date": "2026-10-31"}


def test_no_faults_without_chaos(ledgerops_user):
    assert ledgerops_user.post("/invoices", data=FORM, follow_redirects=False).status_code == 302
    assert 'id="announcement-modal"' not in ledgerops_user.get("/invoices/new").text


def test_submit_503_fires_once(ledgerops_user):
    first = ledgerops_user.post("/invoices", data=FORM, headers=CHAOS, follow_redirects=False)
    assert first.status_code == 503 and "Service temporarily unavailable" in first.text
    assert ledgerops_user.post("/invoices", data=FORM, headers=CHAOS, follow_redirects=False).status_code == 302


def test_blocking_modal_once(ledgerops_user):
    assert 'id="announcement-modal"' in ledgerops_user.get("/invoices/new", headers=CHAOS).text
    assert 'id="announcement-modal"' not in ledgerops_user.get("/invoices/new", headers=CHAOS).text


def test_session_expiry_on_sixth_authenticated_request(ledgerops_user):
    for _ in range(5):
        assert ledgerops_user.get("/vendors", headers=CHAOS, follow_redirects=False).status_code == 200
    sixth = ledgerops_user.get("/vendors", headers=CHAOS, follow_redirects=False)
    assert sixth.headers["location"] == "/login?expired=1"
    assert ledgerops_user.get("/vendors", headers=CHAOS, follow_redirects=False).headers["location"] == "/login"


def test_stale_list_after_create(ledgerops):
    body = {"vendor_id": 1, "invoice_number": "INV-2026-1047", "amount": 48250.0, "currency": "INR",
            "due_date": "2026-10-31"}
    headers = {**API_KEY, **CHAOS}
    assert ledgerops.post("/api/invoices", json=body, headers=headers).status_code == 201
    first = ledgerops.get("/api/invoices", params={"invoice_number": "INV-2026-1047"}, headers=headers).json()
    second = ledgerops.get("/api/invoices", params={"invoice_number": "INV-2026-1047"}, headers=headers).json()
    assert first == [] and len(second) == 1


def test_slow_inbox_delays_first_load(maildesk_user):
    started = time.monotonic()
    maildesk_user.get("/inbox", headers=CHAOS)
    assert time.monotonic() - started >= 3.9 and "slow_inbox" in maildesk_chaos.fired
    started = time.monotonic()
    maildesk_user.get("/inbox", headers=CHAOS)
    assert time.monotonic() - started < 2
