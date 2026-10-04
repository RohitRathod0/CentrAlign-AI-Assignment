# Design decisions

| # | Decision | Rejected alternative | Why |
| --- | --- | --- | --- |
| D1 | Build our own mock company apps | Automate real sites (Gmail etc.) | Brief forbids real credentials; we control failure injection and have ground truth for verification |
| D2 | Observe via indexed DOM snapshot (`[12] <button> "Submit"`) | Screenshots + vision coordinates | Cheaper, deterministic, far more reliable click targeting; screenshots kept only as evidence |
| D3 | One action per LLM step (ReAct-style) with a high-level plan | Generate the full action script up front | Web state is unknown until observed; per-step decisions let the agent react to failures |
| D4 | Planner outputs explicit success criteria before acting | Let the executor decide when it is done | Criteria give the Verifier something concrete to check and keep the agent focused on the end goal |
| D5 | Verifier is a separate read-only agent with fresh context, checking via a different channel (REST API) | Executor self-reports success | Self-grading is biased; a read-only verifier cannot "fix" evidence and catches silent UI failures |
| D6 | Typed error taxonomy with per-class policy | Generic "retry 3 times" | Transient errors deserve backoff, validation errors need a different input, auth errors need re-login; blind retries waste steps |
| D7 | Deterministic policy engine for approvals (code, not prompt) | Ask the LLM whether something is risky | Safety must not depend on model judgement; rules are auditable |
| D8 | Working memory as explicit key-value facts with source | Rely on chat history | Facts survive context compaction and become evidence in the summary |
| D9 | Event-sourced run log (every step is an event in SQLite) | Print logs | Same events drive UI, debugging, eval metrics and demo replay |
| D10 | Tools are generic (`click`, `type`, `http_request`), not task-specific (`enter_invoice`) | Hand-coded workflow tools | Generalization: new tasks need no code changes |
| D11 | Single LLM provider, thin wrapper `agent/llm.py` | Multi-provider abstraction | No future-proofing; wrapper exists only so tests can inject a fake |
| D12 | asyncio task per run, pause via `asyncio.Future` for human input | Celery/queues | Single-user prototype; zero infra |
| D13 | Mistral chat API (called with httpx) instead of Anthropic | Anthropic API as in the original spec | Only a free-tier Mistral key is available. Mistral supports native function calling with `tool_choice="any"` (exactly one tool call) and JSON mode, so the executor contract is unchanged. The wrapper spaces calls (about 1 req/s) and retries 429/5xx so rate limits are not mistaken for agent failures |

## Implementation choices that follow from these

- **Policy sees what a click submits (D2 + D7).** The snapshot records each submit button's form action, method and current field values. A click is therefore judged as, for example, "POST /invoices with amount=240000", not as "click [17]".
- **Verifier `passed` is computed in code (D5).** All checks must pass and every success criterion must be covered. The LLM only reports per-criterion observations.
- **Identical retries only when safe (D6).** The orchestrator replays an action without an LLM call only for TRANSIENT errors, and replays a click only when the tool returns `retry_args` (for example, after a 503 it restored the form and found the same, unobstructed submit button).
- **Page errors are classified generically (D10).** The classifier uses HTTP status, unexpected login redirects within the same app, and ARIA `role="alert"` messages, never app-specific selectors.
