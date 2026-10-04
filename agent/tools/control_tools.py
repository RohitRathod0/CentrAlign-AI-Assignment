"""
WHAT: Schemas of the three control tools the LLM can call:
        ask_user          ask a clarifying question (optionally with choices)
        request_approval  ask a human to approve something it judges risky
        finish            claim the task is done, with summary + evidence

WHY:  They are tools so the model expresses "I need a human" and "I'm done" in
      the same structured way as any action, but they have no handler: they
      change the run's state (pause for a human, start verification), which
      only the orchestrator (agent/loop.py) is allowed to do. Calling
      `finish` does NOT complete the run; it triggers the independent
      Verifier (PRD D5).
"""
from agent.models import RiskLevel
from agent.tools.base import Tool

TOOLS = [
    Tool("ask_user", "Ask the user a question when the task is ambiguous or you are stuck. "
         "The run pauses until they answer.",
         {"type": "object", "properties": {
             "question": {"type": "string"},
             "options": {"type": "array", "items": {"type": "string"}, "description": "Optional choices"}},
          "required": ["question"]},
         RiskLevel.READ),
    Tool("request_approval", "Ask a human to approve a risky step before you take it.",
         {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
         RiskLevel.READ),
    Tool("finish", "Call when every success criterion is met (or provably cannot be). An independent "
         "verifier will check the result.",
         {"type": "object", "properties": {
             "summary": {"type": "string", "description": "2-4 sentences for the user: what was done"},
             "evidence": {"type": "array", "items": {"type": "string"},
                          "description": "Record ids, source quotes, values entered"}},
          "required": ["summary", "evidence"]},
         RiskLevel.READ),
]
