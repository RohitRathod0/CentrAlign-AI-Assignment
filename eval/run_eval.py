"""
WHAT: Eval runner (PRD P1): runs every scenario in eval/scenarios.yaml with
      chaos off and on, answers human requests automatically, then checks the
      ground truth itself through the LedgerOps API and prints a results
      table (also written to eval/results.md).

      python -m eval.run_eval                 # all scenarios, chaos off + on
      python -m eval.run_eval --chaos on --only acme_latest_invoice

WHY:  "Completed" from the agent and "passed" from its Verifier are claims;
      the eval checks the database independently of both and also checks
      that humans were asked exactly when they should be (approval above the
      threshold, clarification for "Acme"). Each scenario starts from a reset
      environment (/__admin/reset), so results are reproducible and
      comparable across chaos modes. Requires both mock apps running.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx
import yaml

from agent import config
from agent.events import EventBus
from agent.llm import MistralLLM
from agent.loop import Orchestrator, new_run
from agent.models import HumanRequest, HumanResponse, Run, RunOptions

EVAL_DIR = Path(__file__).parent


async def reset_environment() -> None:
    async with httpx.AsyncClient() as client:
        for base in (config.MAILDESK_URL, config.LEDGEROPS_URL):
            (await client.post(f"{base}/__admin/reset")).raise_for_status()


async def ground_truth(expect: dict) -> tuple[bool, str]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{config.LEDGEROPS_URL}/api/invoices",
                                params={"invoice_number": expect["invoice_number"]},
                                headers={"X-API-Key": config.LEDGER_API_KEY})
    rows = resp.json()
    if len(rows) != 1:
        return False, f"{len(rows)} records for {expect['invoice_number']}"
    wrong = [f"{k}={rows[0].get(k)!r}" for k, v in expect.items() if rows[0].get(k) != v]
    return not wrong, ("mismatch: " + ", ".join(wrong)) if wrong else rows[0]["record_id"]


async def run_scenario(scenario: dict, chaos: bool) -> dict:
    await reset_environment()
    run: Run = new_run(scenario["task"], RunOptions(chaos=chaos, headless=True))
    bus = EventBus(run.id)
    events = bus.subscribe()
    orchestrator = Orchestrator(run, MistralLLM(run.counters), bus)
    answers = list(scenario.get("answers", []))
    asked = {"approval": 0, "clarification": 0}
    started = time.monotonic()
    worker = asyncio.create_task(orchestrator.run_to_end())
    while not worker.done() or not events.empty():
        try:
            ev = await asyncio.wait_for(events.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if ev.type in ("approval_requested", "clarification_requested"):
            req = HumanRequest(**ev.data)
            asked[req.kind] += 1
            if req.kind == "approval":
                orchestrator.respond(HumanResponse(request_id=req.id, approved=scenario.get("approve", True)))
            else:
                answer = answers.pop(0) if answers else "Use your best judgement."
                orchestrator.respond(HumanResponse(request_id=req.id, answer=answer))
    summary = await worker
    truth_ok, truth_detail = await ground_truth(scenario["expect"])
    humans_ok = (asked["approval"] > 0) == scenario.get("expect_approval", False) and \
                (asked["clarification"] > 0) == scenario.get("expect_clarification", False)
    c = run.counters
    return {"scenario": scenario["name"], "chaos": "on" if chaos else "off", "status": summary.status.value,
            "verified": bool(run.verification and run.verification.passed), "ground_truth": truth_ok,
            "detail": truth_detail, "humans_ok": humans_ok, "steps": len(run.steps), "retries": c["retries"],
            "replans": c["replans"], "llm_calls": c["llm_calls"],
            "tokens": c["input_tokens"] + c["output_tokens"], "seconds": round(time.monotonic() - started)}


def to_markdown(rows: list[dict]) -> str:
    cols = list(rows[0].keys())
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    passed = sum(r["status"] == "completed" and r["ground_truth"] and r["humans_ok"] for r in rows)
    return "\n".join(lines) + f"\n\n{passed}/{len(rows)} runs fully correct (completed + ground truth + human gates).\n"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chaos", choices=["off", "on", "both"], default="both")
    parser.add_argument("--only", help="run only the scenario with this name")
    args = parser.parse_args()
    scenarios = yaml.safe_load((EVAL_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["name"] == args.only]
    modes = {"off": [False], "on": [True], "both": [False, True]}[args.chaos]
    rows = []
    for scenario in scenarios:
        for chaos in modes:
            print(f"running {scenario['name']} (chaos {'on' if chaos else 'off'}) ...", flush=True)
            rows.append(await run_scenario(scenario, chaos))
            print(f"  -> {rows[-1]}", flush=True)
    report = to_markdown(rows)
    (EVAL_DIR / "results.md").write_text(report, encoding="utf-8")
    print("\n" + report)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
