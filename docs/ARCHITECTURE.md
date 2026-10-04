# Architecture

## System (three processes)

```text
                 +------------------------------------------------------+
  User --------> |  Console UI (static HTML/JS, served by FastAPI)      |
  (browser)      +-----------------------+------------------------------+
                          REST + SSE     |
                 +-----------------------v------------------------------+
                 |  Console API  (server/)            port 8000         |
                 |  POST /api/runs  GET /api/runs/{id}/events (SSE)     |
                 |  POST /api/runs/{id}/respond                         |
                 +-----------------------+------------------------------+
                                         | starts asyncio task
   +-------------------------------------v--------------------------------------+
   |  Orchestrator (agent/loop.py)  -- run state machine, budgets, event bus    |
   |                                                                            |
   |   Planner ----> Executor <----> Tool Registry ----> Policy Engine          |
   |   (goal,        (one action      |  browser_*   (Playwright)              |
   |    criteria,     per step via    |  files_read_pdf                         |
   |    subgoals)     tool calling)   |  http_request                           |
   |       ^              |           |  memory_save, ask_user,                 |
   |       | replan       v           |  request_approval, finish               |
   |       +------ Error Classifier / Retry / Loop detector                     |
   |                      |                                                     |
   |                      v on finish()                                         |
   |               Verifier (read-only tools, fresh context)                    |
   |                                                                            |
   |   Working Memory (facts)   Run Store (SQLite: runs, events, facts)         |
   |   Artifacts dir (screenshots, downloaded files)                            |
   +--------------+-------------------------------+-----------------------------+
                  | Playwright (Chromium)         | httpx
        +---------v---------+           +---------v------------------+
        | MailDesk  :8001   |           | LedgerOps :8002            |
        | inbox UI, PDFs    |           | finance UI + REST /api/*   |
        +-------------------+           +----------------------------+
                 (both honour X-Chaos: 1 / CHAOS=1 failure injection)
```

| Component | Responsibility | File |
| --- | --- | --- |
| Console API | Create runs, stream events, accept approvals/clarifications, serve UI and artifacts | `server/main.py` |
| RunManager | Run lifecycle, persist events, route responses/cancel | `server/runs.py` |
| Orchestrator | State machine, step budget, retry/replan counters; emits every event | `agent/loop.py` |
| Planner | Task → `TaskSpec` + `Plan`; replans | `agent/planner.py` |
| Executor | One tool call per step via LLM tool calling | `agent/executor.py` |
| Context builder | Executor prompt context + history compaction | `agent/context.py` |
| Tool Registry | Tool schemas (+ `rationale`, `subgoal_id`), validation, dispatch → `Observation` | `agent/tools/registry.py` |
| Policy Engine | Allowlist block, approval for critical/over-threshold writes | `agent/policy.py` |
| Error Classifier | Exception/HTTP/page signals → `ErrorKind`; `RetryPolicy` escalation | `agent/errors.py` |
| Verifier | Read-only mini-agent, `CriterionCheck` per criterion | `agent/verifier.py` |
| Working Memory | Facts with source + step | `agent/memory.py` |
| Run Store | SQLite runs/events/facts | `server/store.py` |
| Browser Session | Chromium per run, navigation tracking, snapshot, screenshots | `agent/browser/` |
| LLM wrapper | Mistral API (`complete`, `tool_call`), rate limiting, `FakeLLM` | `agent/llm.py` |

## Data models

All in `agent/models.py` (Pydantic v2, as in PRD §7): `Run`, `RunOptions`, `RunStatus`, `TaskSpec`, `Plan`/`Subgoal`, `Action`, `Observation`/`ToolError`/`ErrorKind`, `Step`, `MemoryFact`, `HumanRequest`/`HumanResponse`, `CriterionCheck`/`VerificationResult`, `RunSummary`, `Event`. The API returns them as-is.

## Run state machine

```text
 PENDING ──> PLANNING ──(ambiguities)──> AWAITING_CLARIFICATION ──answer──> PLANNING
                │
                v
            EXECUTING <────────────────────────────────────────────┐
             │  │  │                                                   │
             │  │  └─ask_user──> AWAITING_CLARIFICATION ──answer───────┤
             │  └────policy───> AWAITING_APPROVAL ──approved/denied──┤
             │                                                       │
             └─finish()──> VERIFYING ──fail (attempt < 2)──feedback─┘
                               │
                               ├─pass──> COMPLETED
                               └─fail (attempt = 2)──> FAILED (with partial summary)

 Any state ── step budget exhausted / unrecoverable error ──> FAILED
 Any state ── POST /cancel ──> CANCELLED
```

One EXECUTING step:

1. `context.build_context(...)`: task, criteria, plan with statuses, facts, compacted history, last observation, current page snapshot.
2. `executor.next_action`: one Mistral call, `tool_choice="any"` → `Action` (rationale and subgoal stripped from args).
3. `finish` → Verifier. `ask_user` / `request_approval` → pause on a Future.
4. `policy.check(action, browser.elements)` → block / approval / allow.
5. `registry.execute` → `Observation` (never raises; browser tools re-snapshot and screenshot).
6. On error: `RetryPolicy.decide(error, consecutive)` → handling (table below).
7. Loop detector: the same tool + args 3 times appends a warning.

## Error handling

| ErrorKind | Typical signal | Handling sequence |
| --- | --- | --- |
| TRANSIENT | 5xx, network, navigation timeout | retry same ×2 (backoff 1.5 s, 3 s) → LLM adapts ×1 → replan |
| ELEMENT_NOT_FOUND | index not on page, element never visible | LLM adapts ×3 → replan |
| ELEMENT_BLOCKED | "intercepts pointer events" | LLM adapts ×3 (dismiss overlay) → replan |
| VALIDATION | `role="alert"` after submit, 400/422 | LLM adapts ×2 → ask user |
| AUTH | unexpected same-app login redirect, `expired=1`, 401/403 | LLM adapts ×2 (re-login) → fail |
| DUPLICATE | 409, "already exists" alert | LLM adapts ×2 (report existing) → ask user |
| POLICY_BLOCKED | allowlist violation | LLM adapts ×2 → fail |
| BAD_ARGS | schema validation, unknown tool, text-only reply | LLM adapts ×3 → replan |
| UNKNOWN | anything else | LLM adapts ×3 → replan |

At most 2 replans per run. A step that succeeds, or an error of a different kind, resets the streak.

## Events

`status, plan, action, observation, error_handling, fact_saved, approval_requested, clarification_requested, human_response, verifier_step, verification, run_completed, run_failed`. Each is persisted before fan-out, so SSE replay plus live streaming is gap-free (deduplicated by `seq`).
