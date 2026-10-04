"""
WHAT: ToolRegistry: declares every tool, turns them into Mistral function
      schemas, validates arguments, dispatches to handlers and normalises every
      outcome into an Observation. `execute` never raises.

WHY:  One choke point between "the LLM said X" and "X happened":
        - Every schema gets two required/optional agent fields injected:
          `rationale` (one sentence shown in the UI, PRD 8.2) and `subgoal_id`
          (which plan step this action serves). They are stripped by the
          executor before dispatch, so handlers never see them.
        - Bad or unparseable arguments become a BAD_ARGS observation the LLM
          can learn from, instead of an exception that kills the run.
        - Any exception from a handler is classified (agent/errors.py) so the
          retry policy can react to it.
      Tools are generic (click, type, http_request), never task-specific
      (PRD D10).
"""
from typing import Any

from agent.errors import classify
from agent.models import Action, ErrorKind, Observation, ToolError
from agent.tools.base import Tool, ToolContext

SUMMARY_LIMIT = 1500
AGENT_FIELDS = {
    "rationale": {"type": "string",
                  "description": "One short sentence: why you take this action (shown to the user)."},
    "subgoal_id": {"type": "string",
                   "description": "Id of the plan subgoal this action works on, e.g. 'sg2'."},
}
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def validate_args(schema: dict, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Checks required keys and basic types; coerces numeric strings for integer/number."""
    props = schema.get("properties", {})
    clean: dict[str, Any] = {}
    for key in schema.get("required", []):
        if key not in args or args[key] in (None, ""):
            return args, f"missing required argument '{key}'"
    for key, value in args.items():
        if key not in props:
            return args, f"unknown argument '{key}' (allowed: {', '.join(props)})"
        expected = props[key].get("type")
        if expected in ("integer", "number") and isinstance(value, str):
            try:
                value = int(value) if expected == "integer" else float(value)
            except ValueError:
                return args, f"argument '{key}' must be a {expected}"
        if expected and value is not None and not isinstance(value, _TYPES[expected]):
            return args, f"argument '{key}' must be of type {expected}"
        clean[key] = value
    return clean, None


def _bad_args(message: str) -> Observation:
    return Observation(ok=False, summary=f"Invalid tool call: {message}",
                       error=ToolError(kind=ErrorKind.BAD_ARGS, message=message, retryable=False))


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        out = []
        for t in self._tools.values():
            params = {
                "type": "object",
                "properties": {**t.parameters.get("properties", {}), **AGENT_FIELDS},
                "required": [*t.parameters.get("required", []), "rationale"],
            }
            out.append({"type": "function",
                        "function": {"name": t.name, "description": t.description, "parameters": params}})
        return out

    def brief(self) -> str:
        """One line per tool, for the planner (it plans, it does not call tools)."""
        return "\n".join(f"- {t.name}({', '.join(t.parameters.get('properties', {}))}): {t.description}"
                         for t in self._tools.values())

    async def execute(self, action: Action, ctx: ToolContext) -> Observation:
        tool = self.get(action.tool)
        if tool is None:
            return _bad_args(f"unknown tool '{action.tool}'. Available tools: {', '.join(self._tools)}")
        if tool.handler is None:
            return _bad_args(f"'{tool.name}' is handled by the orchestrator, not executed directly")
        if "_unparsed" in action.args:
            return _bad_args("tool arguments were not valid JSON")
        args, error = validate_args(tool.parameters, action.args)
        if error:
            return _bad_args(error)
        try:
            obs = await tool.handler(args, ctx)
        except Exception as exc:   # tools must never crash the run
            err = classify(exc=exc)
            obs = Observation(ok=False, summary=f"{tool.name} failed [{err.kind.value}]: {err.message}",
                              error=err)
        obs.summary = obs.summary[:SUMMARY_LIMIT]
        return obs


def build_registry() -> ToolRegistry:
    from agent.tools import browser_tools, control_tools, file_tools, http_tools, memory_tools
    return ToolRegistry([*browser_tools.TOOLS, *file_tools.TOOLS, *http_tools.TOOLS,
                         *memory_tools.TOOLS, *control_tools.TOOLS])
