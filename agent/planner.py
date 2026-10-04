"""
WHAT: Planner: task -> TaskSpec (objective, success criteria, constraints,
      known inputs, blocking ambiguities) + Plan (ordered subgoals). Also
      `replan` when the orchestrator decides the current plan is not working.

WHY:  PRD D4. Writing explicit success criteria *before* acting does two jobs:
      it keeps the executor aimed at the end state rather than at the next
      click, and it gives the Verifier concrete statements to check. The plan
      is high-level (milestones), while the executor decides actual actions
      step by step (PRD D3), because web state is unknown until observed.
      Output is JSON-mode text parsed into Pydantic models; one retry with the
      parse error is allowed before the run fails.
"""
import json

from agent import config
from agent.models import Plan, Subgoal, TaskSpec
from agent.prompts import load_prompt


class PlannerError(RuntimeError):
    pass


def _parse_spec_and_plan(text: str) -> tuple[TaskSpec, Plan]:
    data = json.loads(text)
    spec = TaskSpec(
        objective=str(data["objective"]),
        success_criteria=[str(c) for c in data["success_criteria"]],
        constraints=[str(c) for c in data.get("constraints") or []],
        known_inputs={str(k): str(v) for k, v in (data.get("known_inputs") or {}).items()},
        ambiguities=[str(a) for a in data.get("ambiguities") or []],
    )
    plan = Plan(subgoals=_parse_subgoals(data))
    if not spec.success_criteria:
        raise ValueError("success_criteria must not be empty")
    return spec, plan


def _parse_subgoals(data: dict) -> list[Subgoal]:
    subgoals = [Subgoal(id=str(s.get("id") or f"sg{i + 1}"), description=str(s["description"]),
                        status=s.get("status", "pending"))
                for i, s in enumerate(data["subgoals"])]
    if not subgoals:
        raise ValueError("subgoals must not be empty")
    return subgoals


class Planner:
    def __init__(self, llm, tools_brief: str):
        self.llm = llm
        self.system = load_prompt("planner", environment=config.ENVIRONMENT_BRIEF, tools=tools_brief)

    async def plan(self, task: str, clarifications: list[str]) -> tuple[TaskSpec, Plan]:
        user = f"TASK:\n{task}"
        if clarifications:
            user += "\n\nCLARIFICATIONS FROM THE USER (final):\n" + "\n".join(clarifications)
        return await self._ask(user, _parse_spec_and_plan)

    async def replan(self, task: str, spec: TaskSpec, plan: Plan, history: str, reason: str) -> Plan:
        current = "\n".join(f"- {s.id} [{s.status}] {s.description}" for s in plan.subgoals)
        user = (f"TASK:\n{task}\n\nOBJECTIVE: {spec.objective}\nSUCCESS CRITERIA:\n"
                + "\n".join(f"- {c}" for c in spec.success_criteria)
                + f"\n\nCURRENT PLAN (v{plan.version}):\n{current}\n\nRECENT HISTORY:\n{history}"
                + f"\n\nTHE PLAN IS NOT WORKING because: {reason}\n\n"
                "Write a revised plan that avoids this failure. Keep subgoals that are already done "
                "with status \"done\". Return ONLY JSON: "
                '{"subgoals": [{"id": "sg1", "description": "...", "status": "done|pending"}]}')
        subgoals = await self._ask(user, lambda text: _parse_subgoals(json.loads(text)))
        return Plan(subgoals=subgoals, version=plan.version + 1)

    async def _ask(self, user: str, parse):
        reply = await self.llm.complete(self.system, user)
        try:
            return parse(reply.text)
        except (ValueError, KeyError, TypeError) as exc:
            retry = await self.llm.complete(
                self.system, f"{user}\n\nYour previous answer was invalid ({exc}). Return ONLY the JSON object.")
            try:
                return parse(retry.text)
            except (ValueError, KeyError, TypeError) as exc2:
                raise PlannerError(f"planner returned invalid JSON twice: {exc2}") from exc2
