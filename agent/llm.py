"""
WHAT: Thin LLM wrapper (PRD D11) with two calls:
        complete(system, user)          -> JSON text   (planner)
        tool_call(system, user, tools)  -> exactly one tool call (executor, verifier)
      plus FakeLLM, a scripted stand-in used by tests and loop debugging.

WHY:  The PRD specifies Anthropic; this build uses Mistral because that is the
      key available (free tier). Mistral's chat API supports native function
      calling with `tool_choice="any"`, which is the same "must call exactly one
      tool" guarantee the executor contract (PRD 8.2) needs, so nothing above
      this file changes. We call the REST endpoint with httpx (already a
      dependency) instead of the SDK to keep the surface small and stable.

      Free-tier specifics handled here, not in the agent:
        - ~1 request/second: calls are spaced by LLM_MIN_INTERVAL_S.
        - 429 / 5xx / timeouts: retried with exponential backoff (honours
          Retry-After), so a rate-limit is not mistaken for an agent failure.
        - Function names must match ^[a-zA-Z0-9_-]+$, hence `browser_click`
          rather than `browser.click` in the tool registry.

      Each instance writes usage into the run's `counters` dict
      (llm_calls, input_tokens, output_tokens), which the console displays.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent import config

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MAX_ATTEMPTS = 5

# Next time slot at which any LLM call may start (shared: the rate limit is per key).
_next_slot = 0.0


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] | None      # None when the model's arguments were not valid JSON
    raw_args: str
    id: str | None


@dataclass
class LLMReply:
    text: str = ""
    tool_call: ToolCall | None = None


def _count(counters: dict[str, int], input_tokens: int, output_tokens: int) -> None:
    counters["llm_calls"] = counters.get("llm_calls", 0) + 1
    counters["input_tokens"] = counters.get("input_tokens", 0) + input_tokens
    counters["output_tokens"] = counters.get("output_tokens", 0) + output_tokens


def _parse_tool_call(message: dict) -> ToolCall | None:
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0].get("function", {})
    raw = fn.get("arguments", "{}")
    if isinstance(raw, dict):
        return ToolCall(fn.get("name", ""), raw, json.dumps(raw), calls[0].get("id"))
    try:
        args = json.loads(raw) if raw else {}
        args = args if isinstance(args, dict) else None
    except json.JSONDecodeError:
        args = None
    return ToolCall(fn.get("name", ""), args, raw, calls[0].get("id"))


class MistralLLM:
    def __init__(self, counters: dict[str, int], api_key: str = "", model: str = ""):
        self.api_key = api_key or config.MISTRAL_API_KEY
        self.model = model or config.LLM_MODEL
        self.counters = counters
        if not self.api_key:
            raise LLMError("MISTRAL_API_KEY is not set. Copy .env.example to .env and add your key.")

    async def complete(self, system: str, user: str) -> LLMReply:
        """Free-form completion constrained to a JSON object (planner output)."""
        return await self._chat({
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        })

    async def tool_call(self, system: str, user: str, tools: list[dict]) -> LLMReply:
        """Forces exactly one tool call (tool_choice=any, no parallel calls)."""
        return await self._chat({
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": tools,
            "tool_choice": "any",
            "parallel_tool_calls": False,
        })

    async def _chat(self, payload: dict) -> LLMReply:
        global _next_slot
        payload = {"model": self.model, "temperature": 0.1, **payload}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error = ""
        async with httpx.AsyncClient(timeout=90) as client:
            for attempt in range(MAX_ATTEMPTS):
                # Reserve the next free slot synchronously, then wait for it.
                now = time.monotonic()
                slot = max(now, _next_slot)
                _next_slot = slot + config.LLM_MIN_INTERVAL_S
                await asyncio.sleep(slot - now)
                try:
                    resp = await client.post(MISTRAL_CHAT_URL, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"network error: {exc}"
                    await asyncio.sleep(2 ** attempt)
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    retry_after = resp.headers.get("retry-after")
                    await asyncio.sleep(float(retry_after) if retry_after else 2 ** (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"Mistral API error {resp.status_code}: {resp.text[:500]}")
                body = resp.json()
                usage = body.get("usage", {})
                _count(self.counters, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                message = body["choices"][0]["message"]
                content = message.get("content") or ""
                if isinstance(content, list):   # Mistral may return content chunks
                    content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                return LLMReply(text=content, tool_call=_parse_tool_call(message))
        raise LLMError(f"Mistral API unavailable after {MAX_ATTEMPTS} attempts ({last_error})")


@dataclass
class FakeLLM:
    """Scripted LLM for tests. Each script item answers one call, in order:
         {"tool": name, "args": {...}}  -> a tool call (rationale added if missing)
         str                            -> a text reply (for complete(), usually JSON)
         dict without "tool"            -> JSON-dumped text reply
    """
    script: list[Any]
    counters: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)   # (system, user) seen

    def _next(self, system: str, user: str) -> LLMReply:
        self.calls.append((system, user))
        if not self.script:
            raise LLMError("FakeLLM script exhausted")
        item = self.script.pop(0)
        _count(self.counters, len(user) // 4, 50)
        if isinstance(item, dict) and "tool" in item:
            args = {"rationale": "scripted", **item.get("args", {})}
            return LLMReply(tool_call=ToolCall(item["tool"], args, json.dumps(args), f"call_{len(self.calls)}"))
        return LLMReply(text=item if isinstance(item, str) else json.dumps(item))

    async def complete(self, system: str, user: str) -> LLMReply:
        return self._next(system, user)

    async def tool_call(self, system: str, user: str, tools: list[dict]) -> LLMReply:
        return self._next(system, user)
