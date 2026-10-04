"""
WHAT: Command-line runner: `python -m agent.cli "<task>" [--chaos] [--headless]`.
      Prints the live event stream and answers approval/clarification
      requests from stdin.

WHY:  PRD P0. Fastest way to debug the agent: no server, no UI, the same
      Orchestrator and events. It also proves that agent/ is independent of
      server/. Human requests are answered with input() in a worker thread so
      the event loop (and the browser) keeps running while waiting.
"""
import argparse
import asyncio
import json
import sys

from agent import config
from agent.events import EventBus
from agent.llm import MistralLLM
from agent.loop import Orchestrator, new_run
from agent.models import Event, HumanRequest, HumanResponse, RunOptions

END_EVENTS = ("run_completed", "run_failed")


def format_event(ev: Event) -> str | None:
    d = ev.data
    if ev.type == "status":
        return f"== {d['status'].upper()}"
    if ev.type == "plan":
        lines = [f"PLAN v{d['plan']['version']}" + (f" (replanned: {d.get('reason')})" if d.get("replan") else "")]
        if d["plan"]["version"] == 1 and not d.get("replan"):
            lines += [f"  goal: {d['spec']['objective']}"] + [f"  criterion: {c}" for c in d["spec"]["success_criteria"]]
        lines += [f"  [{s['status']:7}] {s['id']}: {s['description']}" for s in d["plan"]["subgoals"]]
        return "\n".join(lines)
    if ev.type == "action":
        return f"-> #{d['step']} {d['tool']} {json.dumps(d['args'], ensure_ascii=False)[:160]}  ({d['rationale']})"
    if ev.type == "observation":
        mark = "ok " if d["ok"] else "ERR"
        return f"   {mark} {d['summary'][:300]}"
    if ev.type == "error_handling":
        return f"   ~ {d['kind']} x{d['consecutive']} -> {d['handling']}" + \
               (f" (backoff {d['backoff_s']:.1f}s)" if d["backoff_s"] else "")
    if ev.type == "fact_saved":
        return f"   + fact {d['key']} = {d['value']}  [{d['source']}]"
    if ev.type == "verifier_step":
        return f"   [verifier] GET {d['url']} {d.get('params') or ''} -> {'ok' if d['ok'] else 'error'}"
    if ev.type == "verification":
        lines = [f"VERIFICATION {'PASSED' if d['passed'] else 'FAILED'}"]
        lines += [f"  {'PASS' if c['passed'] else 'FAIL'} [{c['method']}] {c['criterion']} -> {c['evidence']}"
                  for c in d["checks"]]
        if d.get("feedback"):
            lines.append(f"  feedback: {d['feedback']}")
        return "\n".join(lines)
    if ev.type in END_EVENTS:
        lines = [f"\n### {d['status'].upper()}: {d['summary']}"] + [f"  - {e}" for e in d["evidence"]]
        return "\n".join(lines)
    return None


async def ask_stdin(request: HumanRequest) -> HumanResponse:
    if request.kind == "approval":
        print(f"\n?? APPROVAL NEEDED: {request.question}")
        answer = (await asyncio.to_thread(input, "   approve? [y/N] ")).strip().lower()
        return HumanResponse(request_id=request.id, approved=answer.startswith("y"),
                             answer=None if answer.startswith("y") else "denied from CLI")
    print(f"\n?? QUESTION: {request.question}")
    for i, option in enumerate(request.options, 1):
        print(f"   {i}. {option}")
    answer = (await asyncio.to_thread(input, "   answer: ")).strip()
    if answer.isdigit() and 1 <= int(answer) <= len(request.options):
        answer = request.options[int(answer) - 1]
    return HumanResponse(request_id=request.id, answer=answer)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Worker agent on one task.")
    parser.add_argument("task")
    parser.add_argument("--chaos", action="store_true", help="inject failures in the mock apps for this run")
    parser.add_argument("--headless", action="store_true", default=config.HEADLESS)
    parser.add_argument("--max-steps", type=int, default=config.MAX_STEPS)
    args = parser.parse_args()

    run = new_run(args.task, RunOptions(max_steps=args.max_steps, chaos=args.chaos or config.CHAOS,
                                        headless=args.headless))
    bus = EventBus(run.id)
    events = bus.subscribe()
    orchestrator = Orchestrator(run, MistralLLM(run.counters), bus)
    print(f"Run {run.id}: {run.task}")
    worker = asyncio.create_task(orchestrator.run_to_end())
    while True:
        ev = await events.get()
        line = format_event(ev)
        if line:
            print(line, flush=True)
        if ev.type in ("approval_requested", "clarification_requested"):
            orchestrator.respond(await ask_stdin(HumanRequest(**ev.data)))
        if ev.type in END_EVENTS:
            break
    summary = await worker
    c = run.counters
    print(f"\nsteps={len(run.steps)} retries={c['retries']} replans={c['replans']} "
          f"llm_calls={c['llm_calls']} tokens={c['input_tokens']}+{c['output_tokens']}")
    return 0 if summary.status.value == "completed" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
