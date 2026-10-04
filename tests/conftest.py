"""
WHAT: Shared fixtures: in-process TestClients for MailDesk and LedgerOps,
      reset to seed state (and chaos state cleared) before each test, plus
      logged-in variants.

WHY:  Mock-app tests run in-process (no ports, no browser), so they are fast
      and deterministic. Note: they use the same SQLite files as `make up`,
      and reset them, so do not run tests in the middle of a live demo.
"""
import pytest
from fastapi.testclient import TestClient

from mock_env.ledgerops.app import app as ledgerops_app
from mock_env.maildesk.app import app as maildesk_app

API_KEY = {"X-API-Key": "dev-ledger-key"}
CHAOS = {"X-Chaos": "1"}


@pytest.fixture
def maildesk():
    client = TestClient(maildesk_app)
    client.post("/__admin/reset")
    return client


@pytest.fixture
def ledgerops():
    client = TestClient(ledgerops_app)
    client.post("/__admin/reset")
    return client


@pytest.fixture
def maildesk_user(maildesk):
    maildesk.post("/login", data={"email": "alex@acme-internal.test", "password": "demo123"})
    return maildesk


@pytest.fixture
def ledgerops_user(ledgerops):
    ledgerops.post("/login", data={"email": "finance@acme-internal.test", "password": "ledger123"})
    return ledgerops
