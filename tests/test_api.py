"""
WHAT: Console API end to end with a FakeLLM: create a run, wait for it to
      finish, read it back, replay its events over SSE, and reject stray
      responses / cancels.

WHY:  The console is the demo surface; this proves the API, RunManager,
      RunStore and event replay are wired correctly without a real LLM.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent.llm import FakeLLM
from server import main
from server.runs import RunManager
from server.store import RunStore
from tests.test_loop_fake_llm import FINISH, PLAN, SAVE, VERDICT_PASS


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "manager", RunManager(
        store, llm_factory=lambda counters: FakeLLM([PLAN, SAVE, FINISH, VERDICT_PASS], counters)))
    with TestClient(main.app) as c:
        yield c


def test_run_lifecycle_and_event_replay(client):
    created = client.post("/api/runs", json={"task": "save a fact", "options": {"headless": True}}).json()
    run_id = created["id"]
    for _ in range(50):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] == "completed":
            break
        time.sleep(0.1)
    assert run["status"] == "completed" and run["facts"][0]["key"] == "a"
    assert client.get("/api/runs").json()[0]["id"] == run_id

    body = client.get(f"/api/runs/{run_id}/events").text
    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "run_completed"


def test_errors(client):
    assert client.post("/api/runs", json={"task": "  "}).status_code == 400
    assert client.get("/api/runs/nope").status_code == 404
    created = client.post("/api/runs", json={"task": "t", "options": {"headless": True}}).json()
    stray = client.post(f"/api/runs/{created['id']}/respond", json={"request_id": "x", "answer": "y"})
    assert stray.status_code == 409
