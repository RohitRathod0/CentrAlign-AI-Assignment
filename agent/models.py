"""
WHAT: Every Pydantic schema shared by the agent, the console API and the UI
      (PRD section 7, copied as specified).

WHY:  One typed vocabulary for the whole system: the orchestrator produces
      these objects, the event bus serialises them, the API returns them as-is
      and the console renders them. Keeping them in one module means a schema
      change is visible everywhere at once and tests can build them directly.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class RiskLevel(str, Enum):
    READ = "read"            # observe only
    WRITE = "write"          # reversible change inside allowlisted apps
    CRITICAL = "critical"    # external send, payment, delete, overwrite


class ErrorKind(str, Enum):
    TRANSIENT = "transient"              # 5xx, timeout, network
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_BLOCKED = "element_blocked"  # overlay intercepts click
    VALIDATION = "validation"            # form/API rejected input
    AUTH = "auth"                        # redirected to login / 401
    DUPLICATE = "duplicate"              # 409 / duplicate warning
    POLICY_BLOCKED = "policy_blocked"
    BAD_ARGS = "bad_args"                # tool args failed schema
    UNKNOWN = "unknown"


class TaskSpec(BaseModel):
    objective: str
    success_criteria: list[str]
    constraints: list[str] = []
    known_inputs: dict[str, str] = {}
    ambiguities: list[str] = []


class Subgoal(BaseModel):
    id: str
    description: str
    status: Literal["pending", "active", "done", "failed", "skipped"] = "pending"


class Plan(BaseModel):
    subgoals: list[Subgoal]
    version: int = 1


class Action(BaseModel):
    tool: str
    args: dict[str, Any]
    rationale: str
    subgoal_id: str | None = None
    tool_use_id: str | None = None


class ToolError(BaseModel):
    kind: ErrorKind
    message: str
    retryable: bool


class Observation(BaseModel):
    ok: bool
    summary: str                         # short text for the LLM (<= 1500 chars)
    data: dict[str, Any] = {}
    error: ToolError | None = None
    url: str | None = None
    screenshot: str | None = None        # relative artifact path


class Step(BaseModel):
    index: int
    action: Action
    observation: Observation
    attempt: int = 1
    started_at: datetime
    duration_ms: int


class MemoryFact(BaseModel):
    key: str
    value: str
    source: str
    step: int


class HumanRequest(BaseModel):
    id: str
    kind: Literal["approval", "clarification"]
    question: str
    options: list[str] = []
    action: Action | None = None
    reason: str = ""


class HumanResponse(BaseModel):
    request_id: str
    approved: bool | None = None
    answer: str | None = None


class CriterionCheck(BaseModel):
    criterion: str
    passed: bool
    evidence: str
    method: Literal["api", "ui", "memory"]


class VerificationResult(BaseModel):
    passed: bool
    checks: list[CriterionCheck]
    feedback: str = ""


class RunSummary(BaseModel):
    status: RunStatus
    summary: str
    evidence: list[str]
    facts: list[MemoryFact]


class RunOptions(BaseModel):
    max_steps: int = 40
    chaos: bool = False
    headless: bool = False
    approval_threshold_inr: float = 100000


class Run(BaseModel):
    id: str
    task: str
    options: RunOptions
    status: RunStatus = RunStatus.PENDING
    spec: TaskSpec | None = None
    plan: Plan | None = None
    steps: list[Step] = []
    facts: list[MemoryFact] = []
    pending_request: HumanRequest | None = None
    verification: VerificationResult | None = None
    result: RunSummary | None = None
    counters: dict[str, int] = Field(default_factory=lambda: {
        "retries": 0, "replans": 0, "verifications": 0, "llm_calls": 0,
        "input_tokens": 0, "output_tokens": 0})
    created_at: datetime
    finished_at: datetime | None = None


class Event(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    type: str
    data: dict[str, Any]
