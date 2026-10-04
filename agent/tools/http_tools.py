"""
WHAT: http_request tool plus `send_request`, the shared HTTP function also used
      by the read-only Verifier.

WHY:  Some systems are better read through their API than their UI (exact
      record lookups). The tool injects the LedgerOps API key itself, so the
      key never appears in prompts or LLM output, and adds `X-Chaos: 1` on
      chaos runs so API calls see the same injected faults as the browser.
      Status codes are classified (409 -> DUPLICATE, 422 -> VALIDATION,
      5xx -> TRANSIENT, ...) by the same errors.classify the browser uses.
      The Verifier calls `send_request` directly with method GET only, which
      keeps it read-only by construction.
"""
import json
from urllib.parse import urlparse

import httpx

from agent import config
from agent.errors import classify
from agent.models import Observation, RiskLevel
from agent.tools.base import Tool, ToolContext

LEDGEROPS_PORT = urlparse(config.LEDGEROPS_URL).port


async def send_request(ctx: ToolContext, method: str, url: str, params: dict | None = None,
                       json_body: dict | None = None) -> Observation:
    method = method.upper()
    headers = {}
    if urlparse(url).port == LEDGEROPS_PORT:
        headers["X-API-Key"] = config.LEDGER_API_KEY
    if ctx.chaos:
        headers["X-Chaos"] = "1"
    try:
        resp = await ctx.http.request(method, url, params=params or None,
                                      json=json_body if method != "GET" else None, headers=headers)
    except httpx.HTTPError as exc:
        error = classify(exc=exc)
        return Observation(ok=False, summary=f"{method} {url} failed: {error.message}", error=error, url=url)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:2000]
    error = classify(http_status=resp.status_code, body=resp.text)
    summary = f"{method} {url} -> HTTP {resp.status_code}\n{json.dumps(body, ensure_ascii=False)[:1300]}"
    return Observation(ok=error is None, summary=summary, error=error, url=str(resp.url),
                       data={"status": resp.status_code, "body": body})


async def http_request(args: dict, ctx: ToolContext) -> Observation:
    return await send_request(ctx, args.get("method", "GET"), args["url"],
                              args.get("params"), args.get("json_body"))


TOOLS = [
    Tool("http_request", "Call a company REST API (LedgerOps /api/...). The API key is added automatically.",
         {"type": "object", "properties": {
             "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
             "url": {"type": "string", "description": "Absolute URL"},
             "params": {"type": "object", "description": "Query parameters"},
             "json_body": {"type": "object", "description": "JSON body for POST/PUT/PATCH"}},
          "required": ["method", "url"]},
         RiskLevel.WRITE, http_request),
]
