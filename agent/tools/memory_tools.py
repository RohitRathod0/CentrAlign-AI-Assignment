"""
WHAT: memory_save: store a fact (key, value, source) in the run's WorkingMemory.

WHY:  PRD D8. Saving a fact is an explicit, traceable action ("I read
      amount 48,250.00 from INV-2026-1047.pdf"), so it shows up in the trace,
      survives context compaction, and becomes evidence in the summary. There
      is no "memory_get" tool: all facts are always in the executor context.
"""
from agent.models import Observation, RiskLevel
from agent.tools.base import Tool, ToolContext


async def memory_save(args: dict, ctx: ToolContext) -> Observation:
    fact = ctx.memory.save(args["key"], args["value"], args["source"], ctx.step)
    return Observation(ok=True, summary=f"Saved fact {fact.key} = {fact.value} (source: {fact.source}).",
                       data=fact.model_dump())


TOOLS = [
    Tool("memory_save", "Remember a fact you found (e.g. invoice_number, amount, due_date, record_id) "
         "with where it came from. Save every value you will need later or report as evidence.",
         {"type": "object", "properties": {
             "key": {"type": "string", "description": "snake_case name, e.g. invoice_amount"},
             "value": {"type": "string"},
             "source": {"type": "string", "description": "Where it was read, e.g. 'MailDesk email #10, PDF INV-2026-1047.pdf'"}},
          "required": ["key", "value", "source"]},
         RiskLevel.READ, memory_save),
]
