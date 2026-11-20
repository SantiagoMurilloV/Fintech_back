"""Client for the company brain (gbrain over MCP HTTP).

The brain is a knowledge graph of how the company works: definitions, rules,
decisions, providers, processes and how they connect. It never holds figures;
numbers always come from the database tools.

One JSON-RPC `tools/call` per request, with a bearer token whose only scope is
`read`, so even a leaked token cannot write. Every failure (not configured,
timeout, auth, malformed answer) returns None: the chat answers without the
brain instead of failing. After a failure the brain is skipped for a minute so
a down service does not add its timeout to every message.

Writes (`write`) are a separate path used only by the lesson sync
(agent/lesson_sync.py): they carry their own token, BRAIN_WRITE_TOKEN, and are
off unless it is set. A write failure pauses writes only, never reads, and a
refusal by the tool (e.g. a revision precondition) is reported back with its
code instead of being treated as the service being down.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import NamedTuple

import httpx

from ..config import (BRAIN_READ_TOKEN, BRAIN_TIMEOUT_SECONDS, BRAIN_URL, BRAIN_WRITE_TIMEOUT_SECONDS,
                      BRAIN_WRITE_TOKEN)

log = logging.getLogger("brain")

COOLDOWN_SECONDS = 60
MAX_TEXT = 4000

_state = {"down_until": 0.0, "writes_down_until": 0.0}

# gbrain refusal codes found in an error body (docs/guides/error-codes.md).
_CODE = re.compile(r"\b(revision_required|revision_conflict|page_not_found|not_found|"
                   r"insufficient_scope|permission_denied|invalid_params)\b")


class Outcome(NamedTuple):
    """The result of one write: `ok`, `refused` (with gbrain's code) or `down`."""
    status: str
    code: str | None = None
    text: str = ""


class _Refused(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        match = _CODE.search(detail)
        self.code = match.group(1) if match else None


def enabled() -> bool:
    return bool(BRAIN_URL and BRAIN_READ_TOKEN)


def ready() -> bool:
    return enabled() and time.monotonic() >= _state["down_until"]


def writes_enabled() -> bool:
    return bool(BRAIN_URL and BRAIN_WRITE_TOKEN)


def writes_ready() -> bool:
    now = time.monotonic()
    return (writes_enabled() and now >= _state["writes_down_until"]
            and now >= _state["down_until"])


def reset() -> None:
    _state["down_until"] = 0.0
    _state["writes_down_until"] = 0.0


def _payload(response: httpx.Response) -> dict:
    """The JSON-RPC message, from a plain JSON body or an event stream."""
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("event stream without data")
    return json.loads(body)


def _rpc(tool: str, arguments: dict, token: str, timeout: float) -> str:
    """One JSON-RPC tools/call; the tool's text. Raises _Refused when the tool
    refuses and any other exception when the service cannot be reached."""
    response = httpx.post(
        f"{BRAIN_URL}/mcp",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}},
        timeout=timeout)
    response.raise_for_status()
    message = _payload(response)
    if message.get("error"):
        raise _Refused(json.dumps(message["error"], ensure_ascii=False)[:500])
    result = message.get("result") or {}
    text = "".join(part.get("text", "") for part in result.get("content") or []
                   if part.get("type") == "text").strip()
    if result.get("isError"):
        raise _Refused(text[:500] or "tool error")
    return text


def call(tool: str, arguments: dict) -> str | None:
    """Run one brain tool; its text answer, or None when the brain cannot answer."""
    if not ready():
        return None
    try:
        text = _rpc(tool, arguments, BRAIN_READ_TOKEN, BRAIN_TIMEOUT_SECONDS)
        return text[:MAX_TEXT] or None
    except Exception as err:  # noqa: BLE001 — the brain helps; its absence never breaks a turn
        _state["down_until"] = time.monotonic() + COOLDOWN_SECONDS
        log.warning("brain %s failed (%s); skipped for %ds", tool, err, COOLDOWN_SECONDS)
        return None


def write(tool: str, arguments: dict) -> Outcome:
    """Run one mutating brain tool with the write token. Never raises.

    `down` covers not configured, cooling down, transport, HTTP and malformed
    answers (and starts the write cooldown); `refused` is the tool saying no,
    with gbrain's error code when one is recognisable.
    """
    if not writes_ready():
        return Outcome("down", None, "not configured or cooling down")
    try:
        return Outcome("ok", None, _rpc(tool, arguments, BRAIN_WRITE_TOKEN,
                                        BRAIN_WRITE_TIMEOUT_SECONDS)[:MAX_TEXT])
    except _Refused as refusal:
        log.info("brain %s refused (%s)", tool, refusal.code or str(refusal)[:120])
        return Outcome("refused", refusal.code, str(refusal))
    except Exception as err:  # noqa: BLE001 — a write is retried on the next sync
        _state["writes_down_until"] = time.monotonic() + COOLDOWN_SECONDS
        log.warning("brain write %s failed (%s); writes paused for %ds", tool, err,
                    COOLDOWN_SECONDS)
        return Outcome("down", None, str(err)[:200])


def status() -> dict:
    remaining = max(0, round(_state["down_until"] - time.monotonic()))
    writes_paused = max(0, round(_state["writes_down_until"] - time.monotonic()))
    return {"enabled": enabled(), "down_seconds": remaining or None,
            "writes_enabled": writes_enabled(), "writes_down_seconds": writes_paused or None}
