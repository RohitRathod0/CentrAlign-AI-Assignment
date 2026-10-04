"""
WHAT: load_prompt(name, **values): reads agent/prompts/<name>.md and fills
      `{placeholder}` markers.

WHY:  Prompts are product logic, so they live as readable Markdown files next
      to the code (planner.md, executor.md, verifier.md) instead of string
      literals buried in Python. Plain `str.replace` is used rather than
      `str.format` because the prompts contain literal JSON braces.
"""
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str, **values: str) -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
