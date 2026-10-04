# Worker: Autonomous AI Task Worker

> A narrow but real autonomous worker: plan → act → observe → recover → verify, running against two live mock company apps with deliberate failure injection.

Give Worker one natural-language task, for example *"Find the latest invoice from Acme Corp in the inbox and enter it into LedgerOps"*. It drives a real Chromium browser through a mock email inbox (MailDesk) and a mock finance system (LedgerOps), reads the PDF invoice, fills and submits the form, and recovers from injected failures (503s, expired sessions, blocking modals, slow pages, stale lists). An independent read-only Verifier then checks the result through LedgerOps' REST API. You get a summary with evidence: record ids, source quotes and screenshots.

Submission for CentrAlign AI, AI Engineering Intern.

## Quick start

Requirements: Python 3.11+, a **Mistral API key** (the free tier works, from https://console.mistral.ai).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
make setup                 # pip install -r requirements.txt && playwright install chromium
cp .env.example .env       # then put your key in MISTRAL_API_KEY
make up                    # MailDesk :8001, LedgerOps :8002, console :8000 (honcho + Procfile)
make demo                  # (second terminal) invoice task end to end with chaos on
```

Open http://localhost:8000 for the console: task box, live trace with screenshots, plan, working memory, approval/clarification prompts, verification and result.

### Running on Windows without `make`

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt; python -m playwright install chromium
copy .env.example .env          # add MISTRAL_API_KEY
honcho start                    # starts the three services from the Procfile
# second terminal (venv activated):
curl.exe -X POST http://localhost:8001/__admin/reset; curl.exe -X POST http://localhost:8002/__admin/reset
python -m agent.cli --chaos "Find the latest invoice from Acme Corp in the inbox and enter it into LedgerOps."
python -m pytest -q             # tests (fake LLM, no API key needed)
python -m eval.run_eval         # all scenarios, chaos off + on (needs the key and running apps)
```

## Demo scenarios (same code, different task text)

| # | Task | What it shows |
|---|---|---|
| 1 | Find the latest invoice from Acme Corp in the inbox and enter it into LedgerOps. (chaos on) | Browser + PDF + form entry; recovers from slow inbox, blocking modal, 503 on submit, session expiry; Verifier re-reads a stale list |
| 2 | Find the latest invoice from **Acme** in the inbox and enter it into LedgerOps. | Ambiguity (Acme Corp vs Acme Logistics) → clarification |
| 3 | Enter the Initech invoice from the inbox into LedgerOps. | 2,40,000 INR > threshold → policy engine requires human approval |
| 4 | Enter the Globex Ltd invoice from the inbox into LedgerOps. | Generalization: details are in the email body, not a PDF |

Bonus: asking for Acme's September invoice (INV-2026-1004) hits duplicate detection; the agent must report the existing record (LO-0005) and not create a second one.

`python -m eval.run_eval` runs all four with chaos off and on. It checks ground truth in the LedgerOps database independently of the agent, and checks that humans were asked exactly when they should be. Results go to `eval/results.md`.

## How it works

```text
task ─► Planner ─► TaskSpec (objective, success criteria, ambiguities) + Plan (subgoals)
            │ ambiguities? ─► ask human ─► replan
            ▼
        ┌─► Executor: 1 LLM call → exactly 1 tool call (tool_choice="any")
        │      ▼
        │   Policy engine (code): allowlist? approval needed? ─► ask human
        │      ▼
        │   Tool registry: browser_* / files_read_pdf / http_request / memory_save
        │      ▼
        │   Observation (+ fresh DOM snapshot + screenshot)
        │      ▼
        │   Error classifier → RetryPolicy: retry same | let LLM adapt | replan | ask user | fail
        └──────┘   loop detector, step budget
            ▼ finish()
        Verifier (fresh context, read-only, REST API) ─► pass → COMPLETED
                                                      └► fail → feedback to executor (max 2) → FAILED
```

Every transition is an event (SQLite). The same stream drives the console (SSE), the CLI, the trace and the eval. Full details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Reliability: what each injected fault exercises

| Fault (`X-Chaos: 1`) | What the agent sees | Handling |
|---|---|---|
| `slow_inbox` (4 s) | Navigation in progress | Browser waits on real navigation events (15 s budget), no premature failure |
| `blocking_modal` | Click fails: "intercepts pointer events" → `ELEMENT_BLOCKED`; snapshot shows `!! MODAL DIALOG IS OPEN` | LLM dismisses it, retries |
| `submit_503` | HTTP 503 after submit → `TRANSIENT` | Click tool goes Back and restores the form values; orchestrator retries the identical submit after backoff, with no LLM call |
| `session_expiry` | Redirect to `/login?expired=1` → `AUTH` | Hint: sign in again, check whether the earlier submission was saved before re-entering data |
| `stale_list` | First list read after create omits the row | Verifier re-queries before declaring failure |

## Key decisions

Full table with rejected alternatives: [docs/DECISIONS.md](docs/DECISIONS.md).

- **D1** Own mock apps (no real credentials, controllable failures, ground truth).
- **D2** Indexed DOM snapshot (`[12]<button>Save</button>`) instead of vision.
- **D3** One action per LLM step (ReAct) under a high-level plan.
- **D4** Planner writes success criteria before acting.
- **D5** Separate read-only Verifier with fresh context, different channel (API vs UI); `passed` is computed in code.
- **D6** Typed error taxonomy with per-class retry/escalation policy.
- **D7** Deterministic policy engine for approvals and allowlist.
- **D8** Working memory = explicit facts with sources.
- **D9** Event-sourced run log.
- **D10** Generic tools only (no `enter_invoice` tool).
- **D11** Single LLM provider behind a thin wrapper (fake LLM for tests).
- **D12** asyncio task per run; human input via `asyncio.Future`.
- **D13** Mistral instead of Anthropic (key availability; free tier).

## Models and frameworks

| Layer | Choice |
|---|---|
| LLM | Mistral chat completions API (`LLM_MODEL`, default `mistral-large-latest`), native function calling, `tool_choice="any"`, JSON mode for the planner |
| Browser | Playwright (async Chromium); headed for the demo, headless in tests/eval |
| Backend | FastAPI + Uvicorn, `sse-starlette` |
| Mock apps | FastAPI + Jinja2 server-rendered HTML, SQLite |
| Schemas | Pydantic v2 |
| PDF | pdfplumber (read), reportlab (seed invoices) |
| HTTP | httpx (async) |
| Console | single `index.html`, vanilla JS, `EventSource` |
| Tests | pytest, pytest-asyncio, scripted `FakeLLM` |

## Repository map

```text
mock_env/   common/chaos.py, maildesk/ (inbox, PDFs), ledgerops/ (finance UI + REST API)
agent/      config, models, llm, events, memory, errors, policy, planner, executor,
            verifier, context, loop (orchestrator), cli, prompts/*.md,
            browser/ (session, snapshot), tools/ (registry + generic tools)
server/     main (API + SSE), runs (RunManager), store (SQLite), static/index.html
eval/       scenarios.yaml, run_eval.py
tests/      one file per module, FakeLLM loop tests, API test
```

Every source file starts with a WHAT/WHY header explaining its role, for knowledge transfer.

## Assumptions

The PRD this was built from ends at section 8.2. Wherever it refers to later sections, the smallest choice consistent with the rest of the PRD was made:

- **Error handling (PRD §10)**: the per-kind table in `agent/errors.py` (`RULES`). TRANSIENT retries the same action twice with backoff, then lets the LLM adapt, then replans. VALIDATION and DUPLICATE escalate to asking the user, AUTH to failing. At most 2 replans.
- **Event types (PRD §14.3)**: `status, plan, action, observation, error_handling, fact_saved, approval_requested, clarification_requested, human_response, verifier_step, verification, run_completed, run_failed`.
- **Demo scenarios (PRD §17)**: the four in the table above, derived from the seed data in PRD §6.1.
- Tool names use underscores (`browser_click`) because Mistral function names cannot contain dots.
- Login credentials for the mock apps are part of the environment brief given to the agent (like an employee's own accounts). The LedgerOps API key is injected by the HTTP tool and never shown to the LLM.
- `session_expiry` counts only cookie-authenticated UI requests made in chaos mode. `stale_list` applies to whichever list (UI or API) is read first after a create.
- Policy: an `amount` in a currency other than INR always needs approval (no FX conversion).
- LedgerOps sends `Cache-Control: no-store`, so going Back after a failed submit re-fetches the form like a real finance app.
- Seed "today" is 2026-10-03; LedgerOps rejects due dates before it.

## Limitations

- The Verifier reads LedgerOps through its API only. MailDesk has no API, so a criterion like "an email was sent" can only be checked from saved facts (`method: memory`).
- The DOM snapshot does not see inside iframes, canvas or shadow DOM. Fine for these apps, not for every site.
- Single user. Active runs live in memory: a console restart keeps history (SQLite) but drops runs that are in flight.
- The Mistral free tier allows about 1 request/second, so calls are spaced and a run takes 25-50 LLM calls (about 1-3 minutes).
- Tests reset the same SQLite files the running mock apps use. Do not run tests during a live demo.

## Next steps

- A read-only browser channel for the Verifier, so UI-only systems can be verified too.
- Use the eval results to tune prompts: track steps, LLM calls and recovery rate per fault.
- Persist pending human requests, so runs survive a console restart.
- Per-tool screenshot diffing as stronger evidence of "what changed".
