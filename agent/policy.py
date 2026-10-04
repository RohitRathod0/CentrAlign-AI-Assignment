"""
WHAT: PolicyEngine: a deterministic gate in front of every tool call. It returns
      allow / block / needs_approval.
        block           URL outside the allowlist (goto, PDF read, HTTP, link click)
        needs_approval  a WRITE that is effectively CRITICAL (form/API target is
                        /send, /mark-paid, /delete, or method DELETE), or that
                        carries an `amount` above the run's INR approval threshold

WHY:  PRD D7. Safety must not depend on model judgement. The LLM can request
      approval voluntarily (request_approval tool) but cannot skip these rules.
      To know what a click will *do*, the engine looks at the clicked element
      in the latest DOM snapshot: if it is a submit button, the snapshot carries
      the form's action, method and current field values, so "click [17]" is
      judged as "POST /invoices with amount=240000". The rules are generic
      (paths, methods, a field called `amount`), not invoice-specific.

      Approved actions are remembered by fingerprint, so an automatic retry of
      an approved submit (e.g. after a 503) does not ask the human twice.
"""
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from agent import config
from agent.models import Action, RiskLevel
from agent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Literal["allow", "block", "needs_approval"]
    reason: str = ""
    question: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    @property
    def needs_approval(self) -> bool:
        return self.verdict == "needs_approval"


ALLOW = PolicyDecision("allow")


def _parse_amount(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class PolicyEngine:
    def __init__(self, registry: ToolRegistry, threshold_inr: float,
                 allowed_origins: set[str] = config.ALLOWED_ORIGINS,
                 critical_suffixes: tuple[str, ...] = config.CRITICAL_PATH_SUFFIXES):
        self.registry = registry
        self.threshold_inr = threshold_inr
        self.allowed_origins = allowed_origins
        self.critical_suffixes = critical_suffixes
        self._approved: set[str] = set()

    def url_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme and not parsed.netloc:
            return True          # relative URL: stays on the current (allowlisted) app
        return f"{parsed.scheme}://{parsed.netloc}" in self.allowed_origins

    def check(self, action: Action, elements: dict[int, dict]) -> PolicyDecision:
        tool = self.registry.get(action.tool)
        if tool is None:
            return ALLOW         # unknown tool -> registry returns BAD_ARGS
        url = action.args.get("url")
        if isinstance(url, str) and not self.url_allowed(url):
            return PolicyDecision("block", f"{url} is outside the allowlisted company apps")
        element = self._element(action, elements)
        if element and element.get("href") and not self.url_allowed(element["href"]):
            return PolicyDecision("block", f"link to {element['href']} leaves the allowlisted apps")
        if tool.risk == RiskLevel.READ:
            return ALLOW

        payload = self._write_payload(action, element)
        if payload is None or self._fingerprint(payload) in self._approved:
            return ALLOW
        method, path, fields = payload
        reasons = []
        if method == "DELETE" or path.rstrip("/").endswith(self.critical_suffixes):
            reasons.append(f"critical action ({method} {path})")
        amount = _parse_amount(fields.get("amount")) if "amount" in fields else None
        if amount is not None:
            currency = str(fields.get("currency") or "INR").upper()
            if currency != "INR":
                reasons.append(f"amount {amount:,.2f} {currency} cannot be checked against the INR threshold")
            elif amount > self.threshold_inr:
                reasons.append(f"amount {amount:,.2f} INR exceeds the approval threshold "
                               f"of {self.threshold_inr:,.2f} INR")
        if not reasons:
            return ALLOW
        reason = "; ".join(reasons)
        return PolicyDecision("needs_approval", reason,
                              f"Approve this action? {action.rationale or action.tool} ({reason})")

    def record_approval(self, action: Action, elements: dict[int, dict]) -> None:
        payload = self._write_payload(action, self._element(action, elements))
        if payload is not None:
            self._approved.add(self._fingerprint(payload))

    @staticmethod
    def _element(action: Action, elements: dict[int, dict]) -> dict | None:
        if action.tool != "browser_click":
            return None
        try:
            return elements.get(int(action.args.get("index")))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _write_payload(action: Action, element: dict | None) -> tuple[str, str, dict] | None:
        """(method, path, fields) of the write this action would perform, if any."""
        if action.tool == "browser_click" and element and element.get("is_submit") \
                and element.get("form_action"):
            method = (element.get("form_method") or "GET").upper()
            if method == "GET":
                return None      # search/filter forms do not change anything
            return method, urlparse(element["form_action"]).path, element.get("form_values") or {}
        if action.tool == "http_request":
            method = str(action.args.get("method", "GET")).upper()
            if method == "GET":
                return None
            body = action.args.get("json_body")
            return method, urlparse(str(action.args.get("url", ""))).path, body if isinstance(body, dict) else {}
        return None

    @staticmethod
    def _fingerprint(payload: tuple[str, str, dict]) -> str:
        method, path, fields = payload
        return f"{method} {path} " + "&".join(f"{k}={fields[k]}" for k in sorted(fields))
