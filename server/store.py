"""
WHAT: RunStore: SQLite persistence for the console (artifacts/runs.db).
        runs    latest snapshot of each Run (JSON), for GET /api/runs/{id}
        events  every event of every run, in order, for SSE replay
        facts   every saved working-memory fact, queryable across runs

WHY:  PRD D9, event-sourced run log. The event table is the source of truth
      for what happened: a browser that connects late (or after a server
      restart) replays the run from it, and the eval script/README numbers
      come from it. Run snapshots are a convenience so the API can return the
      current Run without replaying events. stdlib sqlite3 is enough for a
      single-user prototype (PRD 4).
"""
import json
import sqlite3
from pathlib import Path

from agent import config
from agent.models import Event, Run

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, task TEXT, status TEXT, created_at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS events (run_id TEXT, seq INTEGER, ts TEXT, type TEXT, data TEXT,
                                   PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS facts (run_id TEXT, key TEXT, value TEXT, source TEXT, step INTEGER);
"""


class RunStore:
    def __init__(self, path: Path = config.RUNS_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)

    def save_run(self, run: Run) -> None:
        self.conn.execute("INSERT OR REPLACE INTO runs (id, task, status, created_at, data) VALUES (?, ?, ?, ?, ?)",
                          (run.id, run.task, run.status.value, run.created_at.isoformat(), run.model_dump_json()))
        self.conn.commit()

    def add_event(self, event: Event) -> None:
        self.conn.execute("INSERT OR REPLACE INTO events (run_id, seq, ts, type, data) VALUES (?, ?, ?, ?, ?)",
                          (event.run_id, event.seq, event.ts.isoformat(), event.type, json.dumps(event.data)))
        if event.type == "fact_saved":
            d = event.data
            self.conn.execute("INSERT INTO facts (run_id, key, value, source, step) VALUES (?, ?, ?, ?, ?)",
                              (event.run_id, d["key"], d["value"], d["source"], d["step"]))
        self.conn.commit()

    def get_run(self, run_id: str) -> Run | None:
        row = self.conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute("SELECT id, task, status, created_at FROM runs ORDER BY created_at DESC LIMIT ?",
                                 (limit,)).fetchall()
        return [{"id": r[0], "task": r[1], "status": r[2], "created_at": r[3]} for r in rows]

    def events(self, run_id: str) -> list[Event]:
        rows = self.conn.execute("SELECT run_id, seq, ts, type, data FROM events WHERE run_id = ? ORDER BY seq",
                                 (run_id,)).fetchall()
        return [Event(run_id=r[0], seq=r[1], ts=r[2], type=r[3], data=json.loads(r[4])) for r in rows]
