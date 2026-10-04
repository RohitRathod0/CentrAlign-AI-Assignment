"""
WHAT: Tool registry contract (schemas, argument validation, never-raise
      execution) and the HTTP tool against an in-process LedgerOps.

WHY:  The registry is the boundary where LLM output becomes real actions.
      Malformed calls must turn into BAD_ARGS observations the agent can
      recover from, and HTTP outcomes must be classified correctly.
"""
import httpx

from agent.memory import WorkingMemory
from agent.models import Action, ErrorKind
from agent.tools.base import ToolContext
from agent.tools.registry import build_registry, validate_args
from mock_env.ledgerops.app import app as ledgerops_app


def make_ctx(tmp_path, http=None):
    return ToolContext("t", browser=None, memory=WorkingMemory(), http=http, artifacts_dir=tmp_path, chaos=False)


def test_schemas_inject_agent_fields():
    schemas = {s["function"]["name"]: s["function"] for s in build_registry().schemas()}
    assert {"browser_click", "files_read_pdf", "http_request", "memory_save", "finish"} <= set(schemas)
    click = schemas["browser_click"]["parameters"]
    assert click["required"] == ["index", "rationale"] and "subgoal_id" in click["properties"]


def test_validate_args():
    schema = {"properties": {"index": {"type": "integer"}, "text": {"type": "string"}}, "required": ["index"]}
    assert validate_args(schema, {"index": "7"}) == ({"index": 7}, None)
    assert "missing" in validate_args(schema, {})[1]
    assert "unknown argument" in validate_args(schema, {"index": 1, "foo": 2})[1]
    assert "must be of type string" in validate_args(schema, {"index": 1, "text": 5})[1]


async def test_execute_never_raises_on_bad_calls(tmp_path):
    registry, ctx = build_registry(), make_ctx(tmp_path)
    for action in (Action(tool="teleport", args={}, rationale=""),
                   Action(tool="finish", args={"summary": "x", "evidence": []}, rationale=""),
                   Action(tool="memory_save", args={"_unparsed": "{oops"}, rationale=""),
                   Action(tool="memory_save", args={"key": "k"}, rationale="")):
        obs = await registry.execute(action, ctx)
        assert not obs.ok and obs.error.kind == ErrorKind.BAD_ARGS


async def test_memory_save_records_step(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.step = 4
    obs = await build_registry().execute(
        Action(tool="memory_save", args={"key": "amount", "value": "48250.00", "source": "PDF"}, rationale=""), ctx)
    assert obs.ok and ctx.memory.facts()[0].step == 4


async def test_http_request_adds_key_and_classifies(tmp_path):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=ledgerops_app))
    await http.post("http://localhost:8002/__admin/reset")
    registry, ctx = build_registry(), make_ctx(tmp_path, http)
    ok = await registry.execute(Action(tool="http_request", rationale="", args={
        "method": "GET", "url": "http://localhost:8002/api/invoices", "params": {"invoice_number": "INV-2026-1004"}}), ctx)
    assert ok.ok and ok.data["body"][0]["record_id"] == "LO-0005"
    dup = await registry.execute(Action(tool="http_request", rationale="", args={
        "method": "POST", "url": "http://localhost:8002/api/invoices",
        "json_body": {"vendor_id": 1, "invoice_number": "INV-2026-1004", "amount": 1, "currency": "INR",
                      "due_date": "2026-10-31"}}), ctx)
    assert dup.error.kind == ErrorKind.DUPLICATE
    await http.aclose()
