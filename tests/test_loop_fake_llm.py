"""
WHAT: Orchestrator behaviour with a scripted FakeLLM (no network, no browser):
      happy path, pre-execution clarification, policy approval denial, error
      hints, verification failure limit, step budget, loop detection.

WHY:  The state machine (PRD section 8) is the core of autonomy and
      reliability. Scripting the LLM makes each branch deterministic, so a
      change to the loop that breaks recovery or safety is caught without
      spending API calls.
"""
import asyncio

from agent.events import EventBus
from agent.llm import FakeLLM
from agent.loop import Orchestrator, is_looping, new_run
from agent.models import Action, HumanRequest, HumanResponse, Observation, RunOptions, RunStatus, Step

PLAN = {"objective": "Record a fact", "success_criteria": ["fact saved"], "constraints": [],
        "known_inputs": {}, "ambiguities": [], "subgoals": [{"id": "sg1", "description": "save"}]}
VERDICT_PASS = {"tool": "submit_verdict", "args": {"checks": [
    {"criterion": "fact saved", "passed": True, "evidence": "seen", "method": "memory"}], "feedback": ""}}
VERDICT_FAIL = {"tool": "submit_verdict", "args": {"checks": [
    {"criterion": "fact saved", "passed": False, "evidence": "missing", "method": "api"}], "feedback": "not found"}}
SAVE = {"tool": "memory_save", "args": {"key": "a", "value": "1", "source": "test", "subgoal_id": "sg1"}}
FINISH = {"tool": "finish", "args": {"summary": "done", "evidence": ["a=1"]}}


async def run_script(script, answer=None, **options):
    """Run to the end; `answer(request)` produces HumanResponses. Returns (run, summary, events)."""
    run = new_run("test task", RunOptions(headless=True, **options))
    bus = EventBus(run.id)
    seen, queue = [], bus.subscribe()
    orchestrator = Orchestrator(run, FakeLLM(list(script), run.counters), bus)
    worker = asyncio.create_task(orchestrator.run_to_end())
    while not worker.done() or not queue.empty():
        try:
            ev = await asyncio.wait_for(queue.get(), 0.2)
        except asyncio.TimeoutError:
            continue
        seen.append(ev)
        if ev.type.endswith("_requested"):
            orchestrator.respond(answer(HumanRequest(**ev.data)))
    return run, await worker, seen


async def test_happy_path_completes_with_evidence():
    run, summary, events = await run_script([PLAN, SAVE, FINISH, VERDICT_PASS])
    assert summary.status == RunStatus.COMPLETED
    assert run.plan.subgoals[0].status == "done" and run.facts[0].key == "a"
    assert "a=1" in summary.evidence and any(e.startswith("verified via memory") for e in summary.evidence)
    assert [e.data["status"] for e in events if e.type == "status"] == ["planning", "executing", "verifying", "completed"]
    assert run.counters["llm_calls"] == 4


async def test_planning_ambiguity_asks_user_and_replans():
    ambiguous = {**PLAN, "ambiguities": ["Which Acme: Acme Corp or Acme Logistics?"]}
    run, summary, events = await run_script(
        [ambiguous, PLAN, SAVE, FINISH, VERDICT_PASS],
        answer=lambda req: HumanResponse(request_id=req.id, answer="Acme Corp"))
    assert summary.status == RunStatus.COMPLETED
    assert any(e.type == "clarification_requested" for e in events)
    assert next(f for f in run.facts if f.key == "clarification_1").value == "Acme Corp"


async def test_policy_requires_approval_and_denial_is_respected():
    big = {"tool": "http_request", "args": {"method": "POST", "url": "http://localhost:8002/api/invoices",
                                            "json_body": {"amount": 240000, "currency": "INR"}}}
    run, summary, events = await run_script(
        [PLAN, big, FINISH, VERDICT_PASS],
        answer=lambda req: HumanResponse(request_id=req.id, approved=False, answer="too big"))
    request = next(e for e in events if e.type == "approval_requested")
    assert "exceeds the approval threshold" in request.data["reason"]
    assert run.steps[0].observation.summary.startswith("User denied: too big")
    assert summary.status == RunStatus.COMPLETED


async def test_bad_tool_gets_hint_and_blocked_url_is_policy_error():
    script = [PLAN, {"tool": "teleport", "args": {}},
              {"tool": "browser_goto", "args": {"url": "https://gmail.com"}}, FINISH, VERDICT_PASS]
    run, summary, _ = await run_script(script)
    assert "HINT:" in run.steps[0].observation.summary
    assert run.steps[1].observation.error.kind.value == "policy_blocked"


async def test_verification_fails_twice_then_run_fails():
    run, summary, _ = await run_script([PLAN, FINISH, VERDICT_FAIL, FINISH, VERDICT_FAIL])
    assert summary.status == RunStatus.FAILED and run.counters["verifications"] == 2
    assert "VERIFIER" in run.steps[0].observation.summary
    assert any(e.startswith("FAILED check") for e in summary.evidence)


async def test_step_budget_and_loop_warning():
    run, summary, _ = await run_script([PLAN, SAVE, SAVE, SAVE, SAVE], max_steps=4)
    assert summary.status == RunStatus.FAILED and "step budget" in summary.summary
    assert "WARNING: you are repeating" in run.steps[2].observation.summary


def test_is_looping_needs_identical_calls():
    def step(i, args):
        return Step(index=i, action=Action(tool="browser_click", args=args, rationale=""),
                    observation=Observation(ok=True, summary=""), started_at="2026-10-03T00:00:00Z", duration_ms=1)
    assert is_looping([step(1, {"index": 3}), step(2, {"index": 3}), step(3, {"index": 3})])
    assert not is_looping([step(1, {"index": 3}), step(2, {"index": 4}), step(3, {"index": 3})])
