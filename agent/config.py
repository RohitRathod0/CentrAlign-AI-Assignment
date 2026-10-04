"""
WHAT: Single place for environment settings, budgets and the URL allowlist.

WHY:  Every tunable number (step budget, retry limits, timeouts) and every
      environment fact (app URLs, logins) lives here so the rest of the agent
      stays task-agnostic (PRD D10) and there is one file to read to know how a
      run is bounded. Values come from `.env` so the same code runs in tests
      (headless, fake LLM) and in the demo (headed, Mistral).

      The LLM provider is Mistral instead of Anthropic (free tier). Only the
      key/model names change; see agent/llm.py.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- LLM -------------------------------------------------------------------
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "mistral-large-latest")
LLM_MIN_INTERVAL_S = float(os.getenv("LLM_MIN_INTERVAL_S", "1.2"))

# --- Runtime flags ---------------------------------------------------------
HEADLESS = os.getenv("HEADLESS", "0") == "1"
CHAOS = os.getenv("CHAOS", "0") == "1"
LEDGER_API_KEY = os.getenv("LEDGER_API_KEY", "dev-ledger-key")

# --- Paths -----------------------------------------------------------------
ARTIFACTS_DIR = ROOT / "artifacts"
RUNS_DB = ROOT / "artifacts" / "runs.db"

# --- The simulated company -------------------------------------------------
MAILDESK_URL = "http://localhost:8001"
LEDGEROPS_URL = "http://localhost:8002"

# Domain allowlist enforced by the policy engine (PRD D7). Both spellings of
# localhost are allowed because LLMs use them interchangeably.
ALLOWED_ORIGINS = {
    "http://localhost:8001", "http://127.0.0.1:8001",
    "http://localhost:8002", "http://127.0.0.1:8002",
}

# Form/API paths whose submission is CRITICAL (external send, payment, delete)
# and therefore always needs human approval.
CRITICAL_PATH_SUFFIXES = ("/send", "/mark-paid", "/delete")

# What a new employee would be told on day one. Rendered into planner and
# executor prompts; contains no task-specific workflow.
ENVIRONMENT_BRIEF = f"""\
Company apps you can use:
- MailDesk ({MAILDESK_URL}): the company email inbox. Web UI only (no API).
  Login: alex@acme-internal.test / demo123
- LedgerOps ({LEDGEROPS_URL}): the internal finance system (vendors, invoices).
  Web UI login: finance@acme-internal.test / ledger123
  REST API under {LEDGEROPS_URL}/api (GET /api/vendors, GET /api/invoices,
  GET /api/invoices/{{id}}); the http_request tool adds the API key for you.
Employees enter data through the LedgerOps web UI; the API is for lookups.
Amounts are in INR unless stated otherwise. Today's date is 2026-10-03."""

# --- Budgets (PRD section 8) -----------------------------------------------
MAX_STEPS = 40
MAX_VERIFICATIONS = 2
MAX_REPLANS = 2
MAX_CLARIFICATION_ROUNDS = 3
VERIFIER_MAX_TURNS = 8
RECENT_STEPS_IN_CONTEXT = 6       # older steps are compacted to one line each

# --- Browser timeouts (ms) -------------------------------------------------
NAV_TIMEOUT_MS = 15_000           # > slow_inbox delay (4 s)
ACTION_TIMEOUT_MS = 5_000         # short so a blocked click fails fast
