"""
WHAT: RunManager: starts each run as an asyncio task (PRD D12), keeps the
      live ones (Run + EventBus + Orchestrator + task) in memory, routes human
      responses and cancellations to them, and persists every event and run
      snapshot through RunStore.

WHY:  It is the only bridge between HTTP and the agent. The Orchestrator
      knows nothing about FastAPI; the manager wires its EventBus to SQLite
      (persist-before-fan-out, so SSE can replay without gaps) and exposes
      plain methods the API handlers call. The LLM is created through
      `llm_factory`, so tests can swap in FakeLLM without patching internals.
"""
import asyncio
from dataclasses import dataclass
from typing import Callable

from agent.events import EventBus
from agent.llm import MistralLLM
from agent.loop import Orchestrator, new_run
from agent.models import Event, HumanResponse, Run, RunOptions
from server.store import RunStore


@dataclass
class ActiveRun:
    run: Run
    bus: EventBus
    orchestrator: Orchestrator
    task: asyncio.Task


class RunManager:
    def __init__(self, store: RunStore, llm_factory: Callable[[dict], object] = MistralLLM):
        self.store = store
        self.llm_factory = llm_factory
        self.active: dict[str, ActiveRun] = {}

    def start(self, task: str, options: RunOptions) -> Run:
        run = new_run(task, options)
        llm = self.llm_factory(run.counters)          # raises LLMError if no API key
        bus = EventBus(run.id, on_event=lambda event: self._persist(run, event))
        orchestrator = Orchestrator(run, llm, bus)
        self.store.save_run(run)
        self.active[run.id] = ActiveRun(run, bus, orchestrator,
                                        asyncio.create_task(orchestrator.run_to_end()))
        return run

    def _persist(self, run: Run, event: Event) -> None:
        self.store.add_event(event)
        self.store.save_run(run)

    def get(self, run_id: str) -> Run | None:
        active = self.active.get(run_id)
        return active.run if active else self.store.get_run(run_id)

    def bus(self, run_id: str) -> EventBus | None:
        active = self.active.get(run_id)
        return active.bus if active else None

    def respond(self, run_id: str, response: HumanResponse) -> bool:
        active = self.active.get(run_id)
        return bool(active) and active.orchestrator.respond(response)

    def cancel(self, run_id: str) -> bool:
        active = self.active.get(run_id)
        if not active or active.task.done():
            return False
        active.task.cancel()
        return True
