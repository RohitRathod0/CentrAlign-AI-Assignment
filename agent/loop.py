"""
WHAT: Orchestrator: owns one run's state machine (PRD section 8):

        PENDING -> PLANNING (-> AWAITING_CLARIFICATION -> PLANNING)*
                -> EXECUTING <-> AWAITING_APPROVAL / AWAITING_CLARIFICATION
                -> VERIFYING -> COMPLETED | back to EXECUTING (feedback) | FAILED
        any state -> FAILED (budget / unrecoverable) | CANCELLED (task cancelled)

      Each EXECUTING step: build context -> executor picks one tool -> policy
      check (block / ask approval) -> registry executes -> record step ->
      on error, RetryPolicy decides: retry same / let LLM adapt / replan /
      ask user / fail -> loop-detector warning -> next step.

WHY:  All control decisions that must be reliable are made here in code, not
      by the LLM: the step budget, retry counts and backoff, when to replan,
      when a human must approve, and whether the run actually succeeded (the
      Verifier's checks). The LLM only proposes the next action.
      Every transition is emitted as an event (PRD D9), so the console, the
      SQLite trace, the CLI and the eval script all observe the same run.
      Human input pauses the run on an asyncio.Future (PRD D12), resolved by
      the API (`POST /api/runs/{id}/respond`) or by the CLI from stdin.
      Nothing here imports from server/, so the CLI can run the agent alone.
"""
import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx

from agent import config
from agent.browser.session import BrowserSession
from agent.context import build_context, render_history
from agent.errors import Decision, Handling, RetryPolicy
from agent.events import EventBus
from agent.executor import Executor
from agent.memory import WorkingMemory
from agent.models import (Action, ErrorKind, HumanRequest, HumanResponse, MemoryFact, Observation,
                          Run, RunOptions, RunStatus, RunSummary, Step, ToolError, VerificationResult)
from agent.planner import Planner
from agent.policy import PolicyEngine
from agent.tools.base import ToolContext
from agent.tools.http_tools import send_request
from agent.tools.registry import ToolRegistry, build_registry
from agent.verifier import Verifier

LOOP_WINDOW = 3
LOOP_WARNING = "\nWARNING: you are repeating the same action without progress. Try a different approach."


def new_run(task: str, options: RunOptions) -> Run:
    """A fresh Run with a short sortable id (used by the CLI, the console API and eval)."""
    now = datetime.now(timezone.utc)
    return Run(id=now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], task=task, options=options,
               created_at=now)


def is_looping(steps: list[Step], window: int = LOOP_WINDOW) -> bool:
    """True when the last `window` steps called the same tool with the same args."""
    if len(steps) < window:
        return False
    last = steps[-window:]
    first = (last[0].action.tool, last[0].action.args)
    return all((s.action.tool, s.action.args) == first for s in last[1:])


class Orchestrator:
    def __init__(self, run: Run, llm, bus: EventBus, *, registry: ToolRegistry | None = None,
                 browser: BrowserSession | None = None):
        self.run = run
        self.bus = bus
        run_dir = config.ARTIFACTS_DIR / "runs" / run.id
        self.registry = registry or build_registry()
        self.browser = browser or BrowserSession(run.options.headless, run.options.chaos, run_dir)
        self.memory = WorkingMemory(on_save=self._on_fact)
        self.http = httpx.AsyncClient(timeout=20)
        self.ctx = ToolContext(run.id, self.browser, self.memory, self.http, run_dir, run.options.chaos)
        self.policy = PolicyEngine(self.registry, run.options.approval_threshold_inr)
        self.retry_policy = RetryPolicy()
        self.planner = Planner(llm, self.registry.brief())
        self.executor = Executor(llm, self.registry)
        self.verifier = Verifier(llm, self._verifier_get, bus.emit)
        self._pending: tuple[str, asyncio.Future] | None = None
        self._consecutive: dict[ErrorKind, int] = {}

    # ------------------------------------------------------------ public --

    async def run_to_end(self) -> RunSummary:
        try:
            return await self._run()
        except asyncio.CancelledError:
            return self._finish(RunStatus.CANCELLED, "Run cancelled by the user.", [])
        except Exception as exc:
            return self._fail(f"internal error: {type(exc).__name__}: {exc}")
        finally:
            await self.browser.close()
            await self.http.aclose()

    def respond(self, response: HumanResponse) -> bool:
        """Resolve the pending human request. Returns False if nothing matches."""
        if not self._pending or self._pending[0] != response.request_id or self._pending[1].done():
            return False
        self._pending[1].set_result(response)
        return True

    # -------------------------------------------------------- main flow --

    async def _run(self) -> RunSummary:
        await self._plan()
        self._set_status(RunStatus.EXECUTING)
        last_obs: Observation | None = None
        while True:
            if len(self.run.steps) >= self.run.options.max_steps:
                return self._fail("step budget exhausted")
            context = build_context(self.run.task, self.run.spec, self.run.plan, self.memory,
                                    self.run.steps, last_obs, self.browser.last_snapshot)
            action = await self.executor.next_action(context)
            self._track_subgoal(action)
            tool = self.registry.get(action.tool)
            self.bus.emit("action", {"step": len(self.run.steps) + 1, **action.model_dump(),
                                     "risk": tool.risk.value if tool else None})

            if action.tool == "finish":
                outcome = await self._verify(action)
                if isinstance(outcome, RunSummary):
                    return outcome
                last_obs = outcome
                continue
            if action.tool in ("ask_user", "request_approval"):
                last_obs = await self._human_tool(action)
                continue

            obs = await self._act(action)
            if obs.error:
                handled = await self._handle_error(action, obs)
                if isinstance(handled, RunSummary):
                    return handled
                obs = handled
            else:
                self._consecutive.clear()
            if is_looping(self.run.steps):
                obs.summary += LOOP_WARNING
            last_obs = obs

    async def _plan(self) -> None:
        self._set_status(RunStatus.PLANNING)
        clarifications: list[str] = []
        spec, plan = await self.planner.plan(self.run.task, clarifications)
        for _ in range(config.MAX_CLARIFICATION_ROUNDS):
            if not spec.ambiguities:
                break
            question = spec.ambiguities[0]
            response = await self._ask_human("clarification", question)
            answer = response.answer or ""
            clarifications.append(f"Q: {question}\nA: {answer}")
            self.memory.save(f"clarification_{len(clarifications)}", answer, f"user, asked: {question}", 0)
            self._set_status(RunStatus.PLANNING)
            spec, plan = await self.planner.plan(self.run.task, clarifications)
        spec.ambiguities = []    # whatever is still open, the executor can ask about with ask_user
        self.run.spec, self.run.plan = spec, plan
        self.bus.emit("plan", {"spec": spec, "plan": plan, "replan": False})

    async def _act(self, action: Action, attempt: int = 1) -> Observation:
        """Policy check (+ approval) then execute. Always records a Step."""
        started, t0 = datetime.now(timezone.utc), time.monotonic()
        self.ctx.step = len(self.run.steps) + 1
        decision = self.policy.check(action, self.browser.elements)
        if decision.blocked:
            obs = Observation(ok=False, summary=f"Blocked by company policy: {decision.reason}",
                              error=ToolError(kind=ErrorKind.POLICY_BLOCKED, message=decision.reason,
                                              retryable=False))
        elif decision.needs_approval:
            response = await self._ask_human("approval", decision.question, action=action, reason=decision.reason)
            self._set_status(RunStatus.EXECUTING)
            if response.approved:
                self.policy.record_approval(action, self.browser.elements)
                obs = await self.registry.execute(action, self.ctx)
                obs.summary = "Approved by user. " + obs.summary
            else:
                obs = Observation(ok=False, summary=f"User denied: {response.answer or 'no reason given'}. "
                                                    "Do not try this action again.")
        else:
            obs = await self.registry.execute(action, self.ctx)
        self._record(action, obs, attempt, started, t0)
        return obs

    async def _handle_error(self, action: Action, obs: Observation) -> Observation | RunSummary:
        kind = obs.error.kind
        count = self._consecutive.get(kind, 0) + 1
        self._consecutive = {kind: count}         # a different error kind resets the streak
        decision = self.retry_policy.decide(obs.error, count)

        if decision.handling == Handling.RETRY_SAME:
            # Clicks are only replayed when the tool says an identical retry is safe.
            retry_args = obs.data.get("retry_args") or (None if action.tool == "browser_click" else action.args)
            if retry_args is None:
                decision = Decision(Handling.LET_LLM_ADAPT, hint=decision.hint)
            else:
                self._emit_handling(kind, count, decision)
                self.run.counters["retries"] += 1
                await asyncio.sleep(decision.backoff_s)
                retry = action.model_copy(update={"args": retry_args})
                new_obs = await self._act(retry, attempt=count + 1)
                if new_obs.error:
                    return await self._handle_error(retry, new_obs)
                self._consecutive.clear()
                return new_obs

        self._emit_handling(kind, count, decision)
        if decision.handling == Handling.LET_LLM_ADAPT:
            obs.summary += f"\nHINT: {decision.hint}"
            return obs
        if decision.handling == Handling.REPLAN:
            if self.run.counters["replans"] >= config.MAX_REPLANS:
                return self._fail(f"repeated {kind.value} errors after {config.MAX_REPLANS} replans: "
                                  f"{obs.error.message}")
            await self._replan(f"{kind.value} error repeated {count} times: {obs.error.message}")
            obs.summary += "\nThe plan was revised because of repeated failures. Follow the new plan."
            return obs
        if decision.handling == Handling.ASK_USER:
            response = await self._ask_human(
                "clarification", f"I keep hitting a {kind.value} problem: {obs.error.message} "
                                 "How should I proceed?")
            self._set_status(RunStatus.EXECUTING)
            self._consecutive.clear()
            obs.summary += f"\nUSER GUIDANCE: {response.answer}"
            return obs
        return self._fail(f"unrecoverable {kind.value} error: {obs.error.message}")

    async def _replan(self, reason: str) -> None:
        self.run.counters["replans"] += 1
        self.run.plan = await self.planner.replan(self.run.task, self.run.spec, self.run.plan,
                                                  render_history(self.run.steps, recent=4), reason)
        self._consecutive.clear()
        self.bus.emit("plan", {"spec": self.run.spec, "plan": self.run.plan, "replan": True, "reason": reason})

    async def _human_tool(self, action: Action) -> Observation:
        started, t0 = datetime.now(timezone.utc), time.monotonic()
        question = str(action.args.get("question") or action.rationale)
        if action.tool == "ask_user":
            response = await self._ask_human("clarification", question,
                                             options=[str(o) for o in action.args.get("options") or []])
            answer = response.answer or ""
            self.memory.save(f"user_answer_{len(self.run.steps) + 1}", answer,
                             f"user, asked: {question}", len(self.run.steps) + 1)
            obs = Observation(ok=True, summary=f"User answered: {answer}")
        else:
            response = await self._ask_human("approval", question, action=action, reason="requested by the agent")
            obs = Observation(ok=bool(response.approved),
                              summary="Approved by user." if response.approved
                              else f"User denied: {response.answer or 'no reason given'}. Do not do it.")
        self._set_status(RunStatus.EXECUTING)
        self._record(action, obs, 1, started, t0)
        return obs

    async def _verify(self, action: Action) -> RunSummary | Observation:
        started, t0 = datetime.now(timezone.utc), time.monotonic()
        self._set_status(RunStatus.VERIFYING)
        self.run.counters["verifications"] += 1
        result = await self.verifier.verify(self.run.spec, self.memory.facts(), action.args)
        self.run.verification = result
        self.bus.emit("verification", result)
        if result.passed:
            return self._complete(action, result)
        if self.run.counters["verifications"] >= config.MAX_VERIFICATIONS:
            return self._fail("verification failed", result, action)
        obs = Observation(ok=False, summary="VERIFIER: the task is not done yet. " + result.feedback)
        self._record(action, obs, 1, started, t0)
        self._set_status(RunStatus.EXECUTING)
        return obs

    # ------------------------------------------------------ human input --

    async def _ask_human(self, kind: Literal["approval", "clarification"], question: str,
                         options: list[str] | None = None, action: Action | None = None,
                         reason: str = "") -> HumanResponse:
        request = HumanRequest(id=uuid.uuid4().hex[:8], kind=kind, question=question,
                               options=options or [], action=action, reason=reason)
        future = asyncio.get_running_loop().create_future()
        self._pending = (request.id, future)
        self.run.pending_request = request
        self._set_status(RunStatus.AWAITING_APPROVAL if kind == "approval" else RunStatus.AWAITING_CLARIFICATION)
        self.bus.emit(f"{kind}_requested", request)
        try:
            response = await future
        finally:
            self._pending = None
            self.run.pending_request = None
        self.bus.emit("human_response", response)
        return response

    # ----------------------------------------------------------- helpers --

    async def _verifier_get(self, url: str, params: dict | None) -> Observation:
        if not self.policy.url_allowed(url) or not url.startswith("http"):
            return Observation(ok=False, summary=f"{url} is not an allowed absolute URL",
                               error=ToolError(kind=ErrorKind.POLICY_BLOCKED, message="url not allowed",
                                               retryable=False))
        return await send_request(self.ctx, "GET", url, params)

    def _record(self, action: Action, obs: Observation, attempt: int, started: datetime, t0: float) -> None:
        step = Step(index=len(self.run.steps) + 1, action=action, observation=obs, attempt=attempt,
                    started_at=started, duration_ms=int((time.monotonic() - t0) * 1000))
        self.run.steps.append(step)
        self.bus.emit("observation", {"step": step.index, "tool": action.tool, "attempt": attempt,
                                      "duration_ms": step.duration_ms, **obs.model_dump()})

    def _emit_handling(self, kind: ErrorKind, count: int, decision: Decision) -> None:
        self.bus.emit("error_handling", {"kind": kind.value, "consecutive": count,
                                         "handling": decision.handling.value, "backoff_s": decision.backoff_s})

    def _track_subgoal(self, action: Action) -> None:
        """Working on sgN means everything before it is done and sgN is active."""
        ids = [s.id for s in self.run.plan.subgoals]
        if action.subgoal_id not in ids:
            return
        position, changed = ids.index(action.subgoal_id), False
        for i, subgoal in enumerate(self.run.plan.subgoals):
            new = "done" if i < position and subgoal.status in ("pending", "active") else \
                  "active" if i == position and subgoal.status == "pending" else subgoal.status
            if new != subgoal.status:
                subgoal.status, changed = new, True
        if changed:
            self.bus.emit("plan", {"spec": self.run.spec, "plan": self.run.plan, "replan": False})

    def _set_status(self, status: RunStatus) -> None:
        self.run.status = status
        self.bus.emit("status", {"status": status.value})

    def _on_fact(self, fact: MemoryFact) -> None:
        self.run.facts = self.memory.facts()
        self.bus.emit("fact_saved", fact)

    def _screenshots(self, limit: int = 3) -> list[str]:
        shots = [s.observation.screenshot for s in self.run.steps if s.observation.screenshot]
        return [f"screenshot: {path}" for path in shots[-limit:]]

    def _complete(self, action: Action, result: VerificationResult) -> RunSummary:
        for subgoal in self.run.plan.subgoals:
            if subgoal.status in ("pending", "active"):
                subgoal.status = "done"
        self.bus.emit("plan", {"spec": self.run.spec, "plan": self.run.plan, "replan": False})
        evidence = [str(e) for e in action.args.get("evidence") or []]
        evidence += [f"verified via {c.method}: {c.evidence}" for c in result.checks]
        evidence += self._screenshots()
        return self._finish(RunStatus.COMPLETED, str(action.args.get("summary") or "Task completed."), evidence)

    def _fail(self, reason: str, result: VerificationResult | None = None,
              action: Action | None = None) -> RunSummary:
        done = [s.description for s in (self.run.plan.subgoals if self.run.plan else []) if s.status == "done"]
        summary = f"Run failed: {reason}."
        if done:
            summary += " Completed before failing: " + "; ".join(done) + "."
        if action is not None and action.args.get("summary"):
            summary += f" Agent's last report: {action.args['summary']}"
        evidence = [f"{f.key} = {f.value} (source: {f.source})" for f in self.memory.facts()]
        if result is not None:
            evidence += [f"FAILED check: {c.criterion} -> {c.evidence}" for c in result.checks if not c.passed]
        evidence += self._screenshots()
        return self._finish(RunStatus.FAILED, summary, evidence)

    def _finish(self, status: RunStatus, summary: str, evidence: list[str]) -> RunSummary:
        result = RunSummary(status=status, summary=summary, evidence=evidence, facts=self.memory.facts())
        self.run.result = result
        self.run.pending_request = None
        self.run.finished_at = datetime.now(timezone.utc)
        self._set_status(status)
        self.bus.emit("run_completed" if status == RunStatus.COMPLETED else "run_failed", result)
        return result
