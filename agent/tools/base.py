"""
WHAT: The two types every tool is built from:
        Tool         name + JSON schema + risk level + async handler
        ToolContext  what a handler may touch during one run (browser, memory,
                     HTTP client, artifacts folder, chaos flag, current step)

WHY:  Tools are plain data plus one function, so the registry can generate the
      LLM tool schemas, the policy engine can read the risk level, and tests
      can call a handler directly with a hand-made context. Control tools
      (ask_user, request_approval, finish) have `handler=None` because they
      change the run state, which only the orchestrator may do.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from agent.memory import WorkingMemory
from agent.models import Observation, RiskLevel

if TYPE_CHECKING:
    from agent.browser.session import BrowserSession


@dataclass
class ToolContext:
    run_id: str
    browser: "BrowserSession"
    memory: WorkingMemory
    http: httpx.AsyncClient
    artifacts_dir: Path          # artifacts/runs/<run_id>
    chaos: bool
    step: int = 0


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[Observation]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON schema of type "object"
    risk: RiskLevel
    handler: Handler | None = None
