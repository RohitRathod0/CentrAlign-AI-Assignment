"""
WHAT: The typed error taxonomy in action:
        classify(...)  -> turns a raw failure signal (exception, HTTP status,
                          unexpected redirect to a login page, role="alert"
                          messages on the page) into a ToolError with an ErrorKind.
        RetryPolicy    -> decides, per ErrorKind and how many times in a row it
                          happened, what the orchestrator does next.

WHY:  PRD D6. "Retry 3 times" wastes steps and hides problems. Each class of
      failure has a different correct response:
        TRANSIENT          retry the same action with backoff (no LLM call)
        ELEMENT_BLOCKED    let the LLM see the overlay and dismiss it
        VALIDATION         the LLM must change the input; then ask the user
        AUTH               log in again and resume the subgoal
        DUPLICATE          stop creating; report the existing record
      When the same kind keeps happening, escalate: replan, ask the user, or
      fail. Classification is deterministic code, never an LLM judgement, so
      the behaviour is testable (tests/test_errors.py).

      Page classification is generic: it relies on standard signals
      (HTTP status, login redirects, ARIA role="alert"), not on selectors of
      one specific app, so it keeps working for new tasks and apps.
"""
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

import httpx

from agent.models import ErrorKind, ToolError

DUPLICATE_WORDS = ("duplicate", "already exists")


def _is_login(url: str | None) -> bool:
    return bool(url) and urlparse(url).path.rstrip("/").endswith("/login")


def _kicked_to_login(url: str, prev_url: str | None, target_url: str | None) -> bool:
    """Landed on a login page from inside the same app without asking for it.
    Arriving at an app's login page from elsewhere is just the normal sign-in step,
    and actions taken while already on the login page (typing the password) are not
    new auth failures."""
    if "expired=1" in url and url != prev_url:
        return True
    if not _is_login(url) or _is_login(prev_url) or _is_login(target_url) or not prev_url:
        return False
    return urlparse(url).netloc == urlparse(prev_url).netloc


def classify(*, exc: BaseException | None = None, http_status: int | None = None,
             body: str = "", url: str | None = None, prev_url: str | None = None,
             target_url: str | None = None, alerts: list[str] | tuple = ()) -> ToolError | None:
    """Return a ToolError describing the failure, or None if the outcome looks fine."""
    if exc is not None:
        msg = str(exc)
        low = msg.lower()
        if "intercepts pointer events" in low:
            return ToolError(kind=ErrorKind.ELEMENT_BLOCKED, retryable=False,
                             message="Click was blocked: another element (e.g. a modal overlay) "
                                     "covers the target.")
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or "net::err" in low \
                or ("timeout" in low and ("goto" in low or "navigat" in low)):
            return ToolError(kind=ErrorKind.TRANSIENT, retryable=True, message=msg[:300])
        if "timeout" in low:
            return ToolError(kind=ErrorKind.ELEMENT_NOT_FOUND, retryable=False,
                             message="Element did not become visible/enabled in time: " + msg[:200])
        if isinstance(exc, (ValueError, TypeError, KeyError)) or "is not a <select>" in low \
                or "not an <input>" in low:
            return ToolError(kind=ErrorKind.BAD_ARGS, retryable=False, message=msg[:300])
        return ToolError(kind=ErrorKind.UNKNOWN, retryable=False, message=msg[:300])

    if http_status is not None and http_status >= 400:
        snippet = f"HTTP {http_status}: {body[:200]}"
        if http_status >= 500:
            return ToolError(kind=ErrorKind.TRANSIENT, retryable=True, message=snippet)
        if http_status in (401, 403):
            return ToolError(kind=ErrorKind.AUTH, retryable=False, message=snippet)
        if http_status == 409:
            return ToolError(kind=ErrorKind.DUPLICATE, retryable=False, message=snippet)
        if http_status in (400, 422):
            return ToolError(kind=ErrorKind.VALIDATION, retryable=False, message=snippet)
        return ToolError(kind=ErrorKind.UNKNOWN, retryable=False, message=snippet)

    if url and _kicked_to_login(url, prev_url, target_url):
        return ToolError(kind=ErrorKind.AUTH, retryable=False,
                         message="Redirected to the login page: the session expired or you are not signed in.")

    if alerts:
        text = " | ".join(alerts)[:400]
        if any(w in text.lower() for w in DUPLICATE_WORDS):
            return ToolError(kind=ErrorKind.DUPLICATE, retryable=False, message=text)
        return ToolError(kind=ErrorKind.VALIDATION, retryable=False, message=text)
    return None


class Handling(str, Enum):
    RETRY_SAME = "retry_same"
    LET_LLM_ADAPT = "let_llm_adapt"
    REPLAN = "replan"
    ASK_USER = "ask_user"
    FAIL = "fail"


@dataclass(frozen=True)
class Rule:
    retry_same: int       # automatic identical retries first (with backoff)
    adapt: int            # then this many attempts where the LLM sees the error + hint
    then: Handling        # escalation once both are used up
    hint: str


@dataclass(frozen=True)
class Decision:
    handling: Handling
    backoff_s: float = 0.0
    hint: str = ""


RULES: dict[ErrorKind, Rule] = {
    ErrorKind.TRANSIENT: Rule(2, 1, Handling.REPLAN,
        "Temporary server/network failure (already retried automatically). Check the current "
        "page before repeating anything; a submission may or may not have been saved."),
    ErrorKind.ELEMENT_NOT_FOUND: Rule(0, 3, Handling.REPLAN,
        "That element is not on the current page. Use indices from the CURRENT PAGE snapshot "
        "only; indices change after every page change."),
    ErrorKind.ELEMENT_BLOCKED: Rule(0, 3, Handling.REPLAN,
        "Something covers the element, usually a modal dialog. Dismiss/close it first, then retry."),
    ErrorKind.VALIDATION: Rule(0, 2, Handling.ASK_USER,
        "The app rejected the input. Read the error messages, fix the values and do not resubmit "
        "identical values."),
    ErrorKind.AUTH: Rule(0, 2, Handling.FAIL,
        "You are signed out (session expired or never signed in). Sign in again with the "
        "credentials from the environment brief, then resume. Before re-entering data, check "
        "whether an earlier submission was already saved."),
    ErrorKind.DUPLICATE: Rule(0, 2, Handling.ASK_USER,
        "This record already exists. Do not create it again; open or look up the existing record "
        "and report it."),
    ErrorKind.POLICY_BLOCKED: Rule(0, 2, Handling.FAIL,
        "Company policy blocks that action. Choose a different approach inside the allowed apps."),
    ErrorKind.BAD_ARGS: Rule(0, 3, Handling.REPLAN,
        "The tool call was invalid. Check the tool's required parameters and types."),
    ErrorKind.UNKNOWN: Rule(0, 3, Handling.REPLAN,
        "Unexpected error. Look at the current page and try a different approach."),
}

BACKOFF_BASE_S = 1.5


class RetryPolicy:
    def __init__(self, rules: dict[ErrorKind, Rule] = RULES):
        self.rules = rules

    def decide(self, error: ToolError, consecutive: int) -> Decision:
        """`consecutive` = how many times in a row this ErrorKind occurred (1 = first time)."""
        rule = self.rules[error.kind]
        if consecutive <= rule.retry_same:
            return Decision(Handling.RETRY_SAME, BACKOFF_BASE_S * 2 ** (consecutive - 1), rule.hint)
        if consecutive <= rule.retry_same + rule.adapt:
            return Decision(Handling.LET_LLM_ADAPT, 0.0, rule.hint)
        return Decision(rule.then, 0.0, rule.hint)
