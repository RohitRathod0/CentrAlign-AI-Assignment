"""
WHAT: Builds the text the executor sees each step: task, spec, plan with
      statuses, working-memory facts, compacted step history, the last
      observation and the current page snapshot.

WHY:  The executor is stateless per step (one LLM call -> one tool call). Its
      whole "state of mind" is rebuilt from the run state every time, which
      keeps prompts bounded and makes every decision reproducible from the
      event log. Compaction (PRD P1): the last RECENT_STEPS_IN_CONTEXT steps
      are shown in detail; older steps shrink to one line each, and anything
      important from them already lives in working memory as facts (PRD D8).
      This matters on Mistral's free tier, where tokens per minute are limited.
"""
import json

from agent import config
from agent.memory import WorkingMemory
from agent.models import Observation, Plan, Step, TaskSpec

MAX_OLD_LINES = 30


def _args(args: dict) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + "..."


def render_history(steps: list[Step], recent: int = config.RECENT_STEPS_IN_CONTEXT) -> str:
    if not steps:
        return "(no steps yet)"
    old, new = steps[:-recent], steps[-recent:]
    lines = []
    if len(old) > MAX_OLD_LINES:
        lines.append(f"({len(old) - MAX_OLD_LINES} earlier steps omitted)")
        old = old[-MAX_OLD_LINES:]
    for s in old:
        outcome = "ok" if s.observation.ok else f"ERROR {s.observation.error.kind.value}" \
            if s.observation.error else "not ok"
        lines.append(f"#{s.index} {s.action.tool}({_args(s.action.args)}) -> {outcome}")
    for s in new:
        lines.append(f"#{s.index} {s.action.tool}({_args(s.action.args)})"
                     f"{' [retry ' + str(s.attempt) + ']' if s.attempt > 1 else ''} - {s.action.rationale}\n"
                     f"   -> {s.observation.summary[:400]}")
    return "\n".join(lines)


def build_context(task: str, spec: TaskSpec, plan: Plan, memory: WorkingMemory, steps: list[Step],
                  last_observation: Observation | None, page_snapshot: str) -> str:
    parts = [
        f"# TASK\n{task}",
        f"# OBJECTIVE\n{spec.objective}",
        "# SUCCESS CRITERIA\n" + "\n".join(f"- {c}" for c in spec.success_criteria),
    ]
    if spec.constraints:
        parts.append("# CONSTRAINTS\n" + "\n".join(f"- {c}" for c in spec.constraints))
    if spec.known_inputs:
        parts.append("# KNOWN INPUTS\n" + "\n".join(f"- {k}: {v}" for k, v in spec.known_inputs.items()))
    parts += [
        f"# PLAN (v{plan.version})\n" + "\n".join(f"- {s.id} [{s.status}] {s.description}" for s in plan.subgoals),
        "# FACTS IN MEMORY\n" + memory.render(),
        f"# HISTORY ({len(steps)} steps so far)\n" + render_history(steps),
    ]
    if last_observation is not None:
        parts.append("# LAST OBSERVATION\n" + last_observation.summary)
    parts.append("# CURRENT PAGE\n" + (page_snapshot or "(browser not opened yet)"))
    parts.append("Decide the single next action and call exactly one tool.")
    return "\n\n".join(parts)
