"""
WHAT: PolicyEngine decisions: allowlist blocking, approval for amounts above
      the threshold and for critical paths, read-only actions always allowed,
      approvals remembered.

WHY:  Safety rules are code (PRD D7); these tests are the specification of
      what the agent may never do without a human.
"""
from agent.models import Action
from agent.policy import PolicyEngine
from agent.tools.registry import build_registry

SUBMIT = {"index": 10, "tag": "button", "is_submit": True, "form_method": "POST",
          "form_action": "http://localhost:8002/invoices",
          "form_values": {"vendor_id": "4", "amount": "240000.00", "currency": "INR"}}


def act(tool, **args):
    return Action(tool=tool, args=args, rationale="test")


def engine():
    return PolicyEngine(build_registry(), threshold_inr=100000)


def test_blocks_urls_outside_allowlist():
    policy = engine()
    assert policy.check(act("browser_goto", url="https://mail.google.com"), {}).blocked
    assert policy.check(act("http_request", method="GET", url="http://evil.test/api"), {}).blocked
    link = {3: {"index": 3, "tag": "a", "href": "https://example.com/phish"}}
    assert policy.check(act("browser_click", index=3), link).blocked
    assert not policy.check(act("browser_goto", url="http://localhost:8001/inbox"), {}).blocked


def test_amount_above_threshold_needs_approval_then_is_remembered():
    policy = engine()
    decision = policy.check(act("browser_click", index=10), {10: SUBMIT})
    assert decision.needs_approval and "240,000.00 INR exceeds" in decision.reason
    policy.record_approval(act("browser_click", index=10), {10: SUBMIT})
    assert policy.check(act("browser_click", index=10), {10: SUBMIT}).verdict == "allow"


def test_amount_below_threshold_and_get_forms_are_allowed():
    policy = engine()
    small = {**SUBMIT, "form_values": {"amount": "48250.00", "currency": "INR"}}
    search = {**SUBMIT, "form_method": "GET", "form_values": {"q": "acme"}}
    assert policy.check(act("browser_click", index=10), {10: small}).verdict == "allow"
    assert policy.check(act("browser_click", index=10), {10: search}).verdict == "allow"


def test_critical_paths_and_methods_need_approval():
    policy = engine()
    mark_paid = {**SUBMIT, "form_action": "http://localhost:8002/invoices/5/mark-paid", "form_values": {}}
    assert policy.check(act("browser_click", index=10), {10: mark_paid}).needs_approval
    send = {**SUBMIT, "form_action": "http://localhost:8001/send", "form_values": {"to": "x@y.z"}}
    assert policy.check(act("browser_click", index=10), {10: send}).needs_approval
    assert policy.check(act("http_request", method="DELETE", url="http://localhost:8002/api/invoices/5"), {}).needs_approval
    api_big = act("http_request", method="POST", url="http://localhost:8002/api/invoices",
                  json_body={"amount": 240000, "currency": "INR"})
    assert policy.check(api_big, {}).needs_approval


def test_read_tools_never_need_approval():
    policy = engine()
    assert policy.check(act("memory_save", key="a", value="1", source="s"), {}).verdict == "allow"
    assert policy.check(act("files_read_pdf", url="http://localhost:8001/attachments/5"), {}).verdict == "allow"
