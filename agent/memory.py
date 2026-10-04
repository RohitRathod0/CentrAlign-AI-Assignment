"""
WHAT: WorkingMemory: the run's key-value facts, each with its source and the
      step that produced it ("invoice_amount = 48250.00, from PDF
      INV-2026-1047.pdf, step 7").

WHY:  PRD D8. Chat history gets compacted and is noisy; facts are small,
      explicit and always placed in the executor's context, so the agent never
      "forgets" the invoice number it read 15 steps ago. Because each fact
      records where it came from, facts double as evidence in the final
      summary and give the Verifier the claimed values to check against
      LedgerOps.
"""
from typing import Callable

from agent.models import MemoryFact


class WorkingMemory:
    def __init__(self, on_save: Callable[[MemoryFact], None] | None = None):
        self._facts: dict[str, MemoryFact] = {}
        self.on_save = on_save

    def save(self, key: str, value: str, source: str, step: int) -> MemoryFact:
        """Insert or overwrite a fact. Keys are normalised to snake_case-ish lowercase."""
        fact = MemoryFact(key=key.strip().lower().replace(" ", "_"), value=str(value),
                          source=source, step=step)
        self._facts[fact.key] = fact
        if self.on_save:
            self.on_save(fact)
        return fact

    def facts(self) -> list[MemoryFact]:
        return list(self._facts.values())

    def render(self) -> str:
        if not self._facts:
            return "(no facts saved yet)"
        return "\n".join(f"- {f.key} = {f.value}   [source: {f.source}; step {f.step}]"
                         for f in self._facts.values())
