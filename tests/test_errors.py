"""
WHAT: errors.classify maps raw signals to the right ErrorKind; RetryPolicy
      escalates per kind as documented.

WHY:  Classification and escalation drive every recovery decision. They are
      deterministic code precisely so they can be pinned by tests like these.
"""
import httpx

from agent.errors import Handling, RetryPolicy, classify
from agent.models import ErrorKind, ToolError


def test_classify_exceptions():
    blocked = Exception("Locator.click: Timeout 5000ms exceeded.\n <div id=announcement-modal> intercepts pointer events")
    assert classify(exc=blocked).kind == ErrorKind.ELEMENT_BLOCKED
    assert classify(exc=Exception("Locator.click: Timeout 5000ms exceeded.")).kind == ErrorKind.ELEMENT_NOT_FOUND
    assert classify(exc=Exception("Page.goto: Timeout 15000ms exceeded.")).kind == ErrorKind.TRANSIENT
    assert classify(exc=httpx.ConnectError("refused")).kind == ErrorKind.TRANSIENT
    assert classify(exc=Exception("Error: Element is not a <select> element")).kind == ErrorKind.BAD_ARGS


def test_classify_http_status():
    assert classify(http_status=503).kind == ErrorKind.TRANSIENT
    assert classify(http_status=401).kind == ErrorKind.AUTH
    assert classify(http_status=409).kind == ErrorKind.DUPLICATE
    assert classify(http_status=422).kind == ErrorKind.VALIDATION
    assert classify(http_status=200) is None


def test_classify_login_redirects():
    app = "http://localhost:8002"
    assert classify(url=f"{app}/login?expired=1", prev_url=f"{app}/invoices/new").kind == ErrorKind.AUTH
    assert classify(url=f"{app}/login", prev_url=f"{app}/invoices").kind == ErrorKind.AUTH
    # first visit to an app lands on its login page: normal, not an error
    assert classify(url=f"{app}/login", prev_url="http://localhost:8001/inbox") is None
    # typing the password on the expired-login page is not a new auth failure
    assert classify(url=f"{app}/login?expired=1", prev_url=f"{app}/login?expired=1") is None


def test_classify_alerts():
    assert classify(alerts=["Duplicate: invoice X already exists"]).kind == ErrorKind.DUPLICATE
    assert classify(alerts=["Amount: must be greater than 0"]).kind == ErrorKind.VALIDATION


def test_retry_policy_escalation():
    policy = RetryPolicy()
    transient = ToolError(kind=ErrorKind.TRANSIENT, message="503", retryable=True)
    first, second = policy.decide(transient, 1), policy.decide(transient, 2)
    assert first.handling == Handling.RETRY_SAME and second.handling == Handling.RETRY_SAME
    assert second.backoff_s == 2 * first.backoff_s
    assert policy.decide(transient, 3).handling == Handling.LET_LLM_ADAPT
    assert policy.decide(transient, 4).handling == Handling.REPLAN

    validation = ToolError(kind=ErrorKind.VALIDATION, message="bad", retryable=False)
    assert policy.decide(validation, 2).handling == Handling.LET_LLM_ADAPT
    assert policy.decide(validation, 3).handling == Handling.ASK_USER

    auth = ToolError(kind=ErrorKind.AUTH, message="login", retryable=False)
    assert policy.decide(auth, 3).handling == Handling.FAIL
