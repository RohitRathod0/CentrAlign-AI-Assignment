"""
WHAT: Executor.next_action(context) -> Action. One LLM call with every tool
      schema and tool_choice="any", so the model must pick exactly one tool.

WHY:  PRD 8.2 executor contract (ReAct style, PRD D3): decide one action, see
      the result, decide again. The registry adds a required `rationale`
      (shown in the UI) and an optional `subgoal_id` to every tool schema; this
      class strips both off before the args reach a tool handler. A reply
      without a tool call is re-asked once; a second failure becomes an
      invalid Action that the registry turns into a BAD_ARGS observation, so
      the normal error path handles it instead of a crash.
"""
from agent import config
from agent.models import Action
from agent.prompts import load_prompt
from agent.tools.registry import ToolRegistry

NO_TOOL = "no_tool_call"


class Executor:
    def __init__(self, llm, registry: ToolRegistry):
        self.llm = llm
        self.tools = registry.schemas()
        self.system = load_prompt("executor", environment=config.ENVIRONMENT_BRIEF)

    async def next_action(self, context: str) -> Action:
        reply = await self.llm.tool_call(self.system, context, self.tools)
        if reply.tool_call is None:
            reply = await self.llm.tool_call(
                self.system, context + "\n\nYou answered with text only. You MUST call exactly one tool.",
                self.tools)
        call = reply.tool_call
        if call is None:
            return Action(tool=NO_TOOL, args={"text": reply.text[:300]},
                          rationale="model replied without a tool call")
        args = dict(call.args) if call.args is not None else {"_unparsed": call.raw_args[:300]}
        rationale = str(args.pop("rationale", "") or "")
        subgoal_id = args.pop("subgoal_id", None)
        return Action(tool=call.name, args=args, rationale=rationale,
                      subgoal_id=str(subgoal_id) if subgoal_id else None, tool_use_id=call.id)
