"""
WHAT: Console API (port 8000):
        POST /api/runs                  start a run {task, options}
        GET  /api/runs                  recent runs
        GET  /api/runs/{id}             current Run (plan, steps, facts, result)
        GET  /api/runs/{id}/events      SSE: replay stored events, then live ones
        POST /api/runs/{id}/respond     answer an approval / clarification
        POST /api/runs/{id}/cancel      cancel a running run
        GET  /                          console UI (server/static/index.html)
        GET  /artifacts/...             screenshots and downloaded PDFs

WHY:  Observability and the demo surface (PRD P0). The API returns the
      agent's Pydantic models as-is, so the UI renders exactly what the agent
      recorded. SSE (not websockets) is enough for one-way streaming and works
      with the browser's built-in EventSource. Replay-then-live with seq
      de-duplication means refreshing the page never loses or repeats events.

Run: uvicorn server.main:app --port 8000
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent import config
from agent.llm import LLMError
from agent.models import TERMINAL_STATUSES, HumanResponse, Run, RunOptions
from server.runs import RunManager
from server.store import RunStore

STATIC_DIR = Path(__file__).parent / "static"
END_EVENTS = ("run_completed", "run_failed")

app = FastAPI(title="Worker Console")
store = RunStore()
manager = RunManager(store)
config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=config.ARTIFACTS_DIR), name="artifacts")


class CreateRun(BaseModel):
    task: str
    options: RunOptions = RunOptions()


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/runs", response_model=Run)
async def create_run(body: CreateRun):
    if not body.task.strip():
        raise HTTPException(400, "task must not be empty")
    try:
        return manager.start(body.task.strip(), body.options)
    except LLMError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/runs")
async def list_runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}", response_model=Run)
async def get_run(run_id: str):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")

    async def stream():
        bus = manager.bus(run_id)
        queue = bus.subscribe() if bus else None     # subscribe first, then replay: no gap
        try:
            last_seq = 0
            for event in store.events(run_id):
                last_seq = event.seq
                yield {"data": event.model_dump_json()}
            if queue is None or manager.get(run_id).status in TERMINAL_STATUSES:
                return
            while True:
                event = await queue.get()
                if event.seq <= last_seq:
                    continue
                yield {"data": event.model_dump_json()}
                if event.type in END_EVENTS:
                    return
        finally:
            if queue is not None:
                bus.unsubscribe(queue)

    return EventSourceResponse(stream())


@app.post("/api/runs/{run_id}/respond")
async def respond(run_id: str, response: HumanResponse):
    if not manager.respond(run_id, response):
        raise HTTPException(409, "no matching pending request for this run")
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: str):
    if not manager.cancel(run_id):
        raise HTTPException(409, "run is not active")
    return {"ok": True}
