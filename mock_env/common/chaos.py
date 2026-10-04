"""
WHAT: Shared failure injection for the mock apps (PRD section 6.3).
      - ChaosState: per-process record of which faults already fired.
      - ChaosMiddleware: injects the faults that depend only on method + path
        (submit_503, slow_inbox). Faults that need app knowledge
        (session_expiry, blocking_modal, stale_list) call ChaosState from the
        route code.

WHY:  Reliability has to be proven on camera, so failures must be real HTTP
      behaviour the agent observes through the browser, not mocked exceptions.
      Chaos is active when the process runs with CHAOS=1 or when a request
      carries `X-Chaos: 1` (the agent sends that header on chaos runs). Every
      fault fires at most once per reset, so a demo run is reproducible.
"""
import asyncio
import os
from dataclasses import dataclass, field

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse

UNAVAILABLE_HTML = """<!doctype html><html><head><title>503 Service Unavailable</title></head>
<body><h1>Service temporarily unavailable</h1>
<p>The server is busy. Please try again in a moment.</p></body></html>"""


@dataclass
class ChaosState:
    fired: set[str] = field(default_factory=set)
    armed: set[str] = field(default_factory=set)
    counters: dict[str, int] = field(default_factory=dict)

    def active(self, request: Request) -> bool:
        return os.getenv("CHAOS", "0") == "1" or request.headers.get("x-chaos") == "1"

    def fire(self, fault: str, request: Request) -> bool:
        """True exactly once per reset, and only when chaos is active."""
        if not self.active(request) or fault in self.fired:
            return False
        self.fired.add(fault)
        return True

    def arm(self, fault: str, request: Request) -> None:
        """Mark a fault to fire on a later request (e.g. stale_list after a create)."""
        if self.active(request):
            self.armed.add(fault)

    def fire_armed(self, fault: str, request: Request) -> bool:
        if fault in self.armed and self.fire(fault, request):
            self.armed.discard(fault)
            return True
        return False

    def tick(self, counter: str, request: Request) -> int:
        """Count chaos-mode requests of a kind; returns the new count (0 if inactive)."""
        if not self.active(request):
            return 0
        self.counters[counter] = self.counters.get(counter, 0) + 1
        return self.counters[counter]

    def reset(self) -> None:
        self.fired.clear()
        self.armed.clear()
        self.counters.clear()


@dataclass(frozen=True)
class ChaosRule:
    fault: str
    method: str
    path: str
    effect: str              # "503" or "delay"
    delay_s: float = 0.0


class ChaosMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, state: ChaosState, rules: list[ChaosRule]):
        super().__init__(app)
        self.state = state
        self.rules = rules

    async def dispatch(self, request: Request, call_next):
        for rule in self.rules:
            if request.method == rule.method and request.url.path == rule.path \
                    and self.state.fire(rule.fault, request):
                if rule.effect == "503":
                    return HTMLResponse(UNAVAILABLE_HTML, status_code=503)
                await asyncio.sleep(rule.delay_s)
        return await call_next(request)
