"""
WHAT: EventBus: numbers every run event, hands it to a persist callback and
      fans it out to live subscribers (SSE streams, the CLI printer).

WHY:  PRD D9, event-sourced run log. The orchestrator never prints or writes
      to the database itself; it only emits events. The same stream then drives
      the console UI (SSE), the SQLite trace (server/store.py), the CLI output
      and eval metrics. `emit` is synchronous on purpose: it can be called from
      anywhere in the loop (including memory callbacks) without awaiting.

Event types emitted by agent/loop.py:
  status, plan, action, observation, error_handling, fact_saved,
  approval_requested, clarification_requested, human_response,
  verifier_step, verification, run_completed, run_failed
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic_core import to_jsonable_python

from agent.models import Event


class EventBus:
    def __init__(self, run_id: str, on_event: Callable[[Event], None] | None = None):
        self.run_id = run_id
        self.on_event = on_event
        self.seq = 0
        self._subscribers: list[asyncio.Queue] = []

    def emit(self, type: str, data: Any) -> Event:
        self.seq += 1
        event = Event(run_id=self.run_id, seq=self.seq, ts=datetime.now(timezone.utc),
                      type=type, data=to_jsonable_python(data))
        if self.on_event:
            self.on_event(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)
