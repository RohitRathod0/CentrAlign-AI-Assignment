"""
WHAT: Verifier: an independent, read-only mini-agent that runs when the
      executor calls `finish`. It gets a FRESH context (success criteria,
      saved facts, the executor's claim marked as unverified), may only call
      `http_get` against the LedgerOps API, and ends with `submit_verdict`
      (one CriterionCheck per criterion).

WHY:  PRD D5. Self-grading is biased; an agent that just clicked "Save" tends
      to believe it worked. The Verifier:
        - checks through a different channel (REST API) than the executor used
          (browser UI), so a silent UI failure is caught;
        - is read-only by construction: its only data tool performs GET, so it
          cannot "fix" evidence;
        - decides `passed` in code: every check must pass and every criterion
          must be covered. The LLM's own opinion of overall success is not
          used.
      Chaos fault `stale_list` makes the first list read after a create miss
      the new row; the prompt tells the Verifier to re-query before failing.
      Its feedback goes back to the executor if verification fails.
"""
import json
from typing import Awaitable, Callable

from pydantic import ValidationError

from agent import config
from agent.models import CriterionCheck, MemoryFact, Observation, TaskSpec, VerificationResult
from agent.prompts import load_prompt

HTTP_GET_TOOL = {"type": "function", "function": {
    "name": "http_get",
    "description": "Read-only GET request to the LedgerOps REST API.",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "Absolute URL"},
        "params": {"type": "object", "description": "Query parameters"}},
        "required": ["url"]}}}

SUBMIT_TOOL = {"type": "function", "function": {
    "name": "submit_verdict",
    "description": "Report the result: one check per success criterion.",
    "parameters": {"type": "object", "properties": {
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "criterion": {"type": "string"},
            "passed": {"type": "boolean"},
            "evidence": {"type": "string", "description": "What you observed (values, record id)"},
            "method": {"type": "string", "enum": ["api", "ui", "memory"]}},
            "required": ["criterion", "passed", "evidence", "method"]}},
        "feedback": {"type": "string", "description": "What the executor must fix (empty if all passed)"}},
        "required": ["checks", "feedback"]}}}

HttpGet = Callable[[str, dict | None], Awaitable[Observation]]


class Verifier:
    def __init__(self, llm, http_get: HttpGet, emit: Callable[[str, dict], object],
                 max_turns: int = config.VERIFIER_MAX_TURNS):
        self.llm = llm
        self.http_get = http_get
        self.emit = emit
        self.max_turns = max_turns
        self.system = load_prompt("verifier", ledger_url=config.LEDGEROPS_URL)

    async def verify(self, spec: TaskSpec, facts: list[MemoryFact], claim: dict) -> VerificationResult:
        transcript: list[str] = []
        for turn in range(1, self.max_turns + 1):
            reply = await self.llm.tool_call(self.system, self._render(spec, facts, claim, transcript),
                                             [HTTP_GET_TOOL, SUBMIT_TOOL])
            call = reply.tool_call
            if call is None or call.args is None:
                transcript.append("(your last reply was not a valid tool call; call http_get or submit_verdict)")
                continue
            if call.name == "submit_verdict":
                try:
                    checks = [CriterionCheck(**c) for c in call.args.get("checks", [])]
                except (ValidationError, TypeError) as exc:
                    transcript.append(f"submit_verdict rejected: invalid checks ({exc.__class__.__name__}).")
                    continue
                return self._result(spec, checks, str(call.args.get("feedback", "")))
            if call.name == "http_get":
                obs = await self.http_get(str(call.args.get("url", "")), call.args.get("params"))
                self.emit("verifier_step", {"turn": turn, "url": call.args.get("url"),
                                            "params": call.args.get("params"), "ok": obs.ok,
                                            "summary": obs.summary[:500]})
                transcript.append(f"http_get {call.args.get('url')} params={json.dumps(call.args.get('params'))}\n"
                                  f"-> {obs.summary[:1200]}")
            else:
                transcript.append(f"unknown tool '{call.name}'; only http_get and submit_verdict exist")
        return VerificationResult(passed=False, checks=[],
                                  feedback="Verifier could not reach a verdict within its step budget.")

    @staticmethod
    def _result(spec: TaskSpec, checks: list[CriterionCheck], feedback: str) -> VerificationResult:
        passed = bool(checks) and all(c.passed for c in checks) and len(checks) >= len(spec.success_criteria)
        if not passed and len(checks) < len(spec.success_criteria):
            feedback = (feedback + f" (only {len(checks)} of {len(spec.success_criteria)} criteria were checked)").strip()
        return VerificationResult(passed=passed, checks=checks, feedback="" if passed else feedback)

    @staticmethod
    def _render(spec: TaskSpec, facts: list[MemoryFact], claim: dict, transcript: list[str]) -> str:
        parts = [
            "SUCCESS CRITERIA:\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(spec.success_criteria, 1)),
            "FACTS SAVED BY THE EXECUTOR (with sources):\n" +
            ("\n".join(f"- {f.key} = {f.value} [source: {f.source}]" for f in facts) or "(none)"),
            "EXECUTOR'S CLAIM (unverified):\n" + json.dumps(claim, ensure_ascii=False)[:1500],
            "YOUR CALLS SO FAR:\n" + ("\n\n".join(transcript[-6:]) or "(none)"),
            "Call http_get to check, or submit_verdict when every criterion is checked.",
        ]
        return "\n\n".join(parts)
