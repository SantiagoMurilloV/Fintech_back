"""Read-only client for the company brain (gbrain over MCP HTTP).

The brain is a knowledge graph of how the company works: definitions, rules,
decisions, providers, processes and how they connect. It never holds figures;
numbers always come from the database tools.

One JSON-RPC `tools/call` per request, with a bearer token whose only scope is
`read`, so even a leaked token cannot write. Every failure (not configured,
timeout, auth, malformed answer) returns None: the chat answers without the
brain instead of failing. After a failure the brain is skipped for a minute so
a down service does not add its timeout to every message.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from ..config import BRAIN_READ_TOKEN, BRAIN_TIMEOUT_SECONDS, BRAIN_URL

log = logging.getLogger("brain")

COOLDOWN_SECONDS = 60
MAX_TEXT = 4000

_state = {"down_until": 0.0}


def enabled() -> bool:
    return bool(BRAIN_URL and BRAIN_READ_TOKEN)


def ready() -> bool:
    return enabled() and time.monotonic() >= _state["down_until"]


def reset() -> None:
    _state["down_until"] = 0.0


def _payload(response: httpx.Response) -> dict:
    """The JSON-RPC message, from a plain JSON body or an event stream."""
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("event stream without data")
    return json.loads(body)


def call(tool: str, arguments: dict) -> str | None:
    """Run one brain tool; its text answer, or None when the brain cannot answer."""
    if not ready():
        return None
    try:
        response = httpx.post(
            f"{BRAIN_URL}/mcp",
            headers={"Authorization": f"Bearer {BRAIN_READ_TOKEN}",
                     "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}},
            timeout=BRAIN_TIMEOUT_SECONDS)
        response.raise_for_status()
        message = _payload(response)
        if message.get("error"):
            raise ValueError(str(message["error"])[:200])
        result = message.get("result") or {}
        if result.get("isError"):
            raise ValueError("tool error")
        text = "".join(part.get("text", "") for part in result.get("content") or []
                       if part.get("type") == "text").strip()
        return text[:MAX_TEXT] or None
    except Exception as err:  # noqa: BLE001 — the brain helps; its absence never breaks a turn
        _state["down_until"] = time.monotonic() + COOLDOWN_SECONDS
        log.warning("brain %s failed (%s); skipped for %ds", tool, err, COOLDOWN_SECONDS)
        return None


def status() -> dict:
    remaining = max(0, round(_state["down_until"] - time.monotonic()))
    return {"enabled": enabled(), "down_seconds": remaining or None}
