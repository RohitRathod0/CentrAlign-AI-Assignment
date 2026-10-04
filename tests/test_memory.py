"""
WHAT: WorkingMemory saves, normalises keys, overwrites, renders and notifies.

WHY:  Facts are what the executor relies on across many steps and what the
      Verifier and the summary use as evidence (PRD D8).
"""
from agent.memory import WorkingMemory


def test_save_overwrite_render_and_callback():
    seen = []
    memory = WorkingMemory(on_save=seen.append)
    memory.save("Invoice Amount", "48250.00", "PDF INV-2026-1047.pdf", 6)
    memory.save("invoice_amount", "48,250.00", "PDF INV-2026-1047.pdf (re-read)", 9)
    facts = memory.facts()
    assert len(facts) == 1 and facts[0].key == "invoice_amount" and facts[0].step == 9
    assert "invoice_amount = 48,250.00" in memory.render()
    assert [f.step for f in seen] == [6, 9]


def test_empty_render():
    assert WorkingMemory().render() == "(no facts saved yet)"
