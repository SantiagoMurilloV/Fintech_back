"""Single client for the chat model: Claude first, DeepSeek as the fallback.

Every call to the model goes through here: the planner choosing a tool, the
narrator introducing a result and the advisor answering a question. One place
decides the provider, the model, the timeout and what happens when it fails —
and `chat` never raises, so a model that is down degrades the answer instead
of breaking the turn.

Providers: with ANTHROPIC_API_KEY and DEEPSEEK_API_KEY both set, Claude
(CLAUDE_MODEL) answers and DeepSeek (DEEPSEEK_MODEL) takes over, in the same
call, whenever Claude fails. A circuit breaker keeps a failing Claude out of
the way so every message does not pay for a doomed attempt:

- No credit, bad key or no permission: Claude is set aside for 30 minutes.
- Outages, overload or rate limits: after 3 failures in a row, 2 minutes.

Then Claude is tried again on its own. With a single key, that provider alone;
with none, the agent answers with its deterministic summaries only.
"""
from __future__ import annotations

import json
import logging
import re
import time

from ..config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
)

log = logging.getLogger("llm")

TIMEOUT_SECONDS = 60
JSON_RULE = ("\n\nRespond with a single JSON object and nothing else: no prose, "
             "no markdown fences.")

HARD_DOWN_SECONDS = 30 * 60      # credit, key or permission: will not fix itself soon
SOFT_DOWN_SECONDS = 2 * 60       # outage or overload: try again shortly
SOFT_FAILURES = 3
_HARD_ERROR = re.compile(r"credit|billing|balance|quota|insufficient|api.?key|"
                         r"authenticat|permission|unauthori[sz]ed|forbidden", re.I)

_claude = None
if ANTHROPIC_API_KEY:
    import anthropic

    _claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=TIMEOUT_SECONDS)

_deepseek = None
if DEEPSEEK_API_KEY:
    from openai import OpenAI

    _deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL,
                       timeout=TIMEOUT_SECONDS)

_breaker = {"down_until": 0.0, "failures": 0, "reason": ""}


# ------------------------------------------------------------- circuit breaker

def claude_ready() -> bool:
    """Claude is configured and not set aside by the breaker."""
    return _claude is not None and time.monotonic() >= _breaker["down_until"]


def _is_hard(err: Exception) -> bool:
    status = getattr(err, "status_code", None)
    return status in (401, 403) or bool(_HARD_ERROR.search(str(err)))


def claude_failed(err: Exception) -> None:
    """Record a Claude failure; open the breaker when it will not pass soon."""
    if _is_hard(err):
        _breaker.update(down_until=time.monotonic() + HARD_DOWN_SECONDS, failures=0,
                        reason=str(err)[:160])
        log.error("Claude set aside for %d min: %s", HARD_DOWN_SECONDS // 60, err)
        return
    _breaker["failures"] += 1
    if _breaker["failures"] >= SOFT_FAILURES:
        _breaker.update(down_until=time.monotonic() + SOFT_DOWN_SECONDS, failures=0,
                        reason=str(err)[:160])
        log.warning("Claude set aside for %d min after %d failures: %s",
                    SOFT_DOWN_SECONDS // 60, SOFT_FAILURES, err)


def claude_ok() -> None:
    _breaker.update(failures=0, reason="")


def reset() -> None:
    """Close the breaker (tests, or an operator who fixed the key)."""
    _breaker.update(down_until=0.0, failures=0, reason="")


# ----------------------------------------------------------------- status

def is_available() -> bool:
    return _claude is not None or _deepseek is not None


def provider() -> str | None:
    """The provider answering right now: «anthropic», «deepseek» or None."""
    if claude_ready():
        return "anthropic"
    if _deepseek is not None:
        return "deepseek"
    return "anthropic" if _claude is not None else None


def status() -> dict:
    """For the health check: who answers, who backs up, and why Claude is out."""
    remaining = max(0, round(_breaker["down_until"] - time.monotonic()))
    return {
        "primary": "anthropic" if _claude is not None else ("deepseek" if _deepseek else None),
        "fallback": "deepseek" if (_claude is not None and _deepseek is not None) else None,
        "answering": provider(),
        "claude_model": CLAUDE_MODEL if _claude is not None else None,
        "deepseek_model": DEEPSEEK_MODEL if _deepseek is not None else None,
        "claude_down_seconds": remaining or None,
        "claude_down_reason": (_breaker["reason"] or None) if remaining else None,
    }


# ----------------------------------------------------------------- calls

def _first_json(text: str) -> str | None:
    """The JSON object inside a reply, so callers can json.loads what they get."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        json.loads(match.group(0))
    except ValueError:
        return None
    return match.group(0)


def _merge_turns(messages: list[dict]) -> list[dict]:
    """Claude needs turns that alternate and start with the user: merge repeats."""
    merged: list[dict] = []
    for message in messages:
        role = "assistant" if message.get("role") == "assistant" else "user"
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged or [{"role": "user", "content": "."}]


def _ask_claude(system: str, messages: list[dict], max_tokens: int, json_object: bool) -> str:
    # The Anthropic SDK (2.x) takes no sampling parameters: `temperature` is
    # DeepSeek's only. Sending it made every call fail.
    response = _claude.messages.create(
        model=CLAUDE_MODEL, max_tokens=max_tokens,
        system=system + (JSON_RULE if json_object else ""),
        messages=_merge_turns(messages))
    return "".join(block.text for block in response.content
                   if getattr(block, "type", "") == "text").strip()


def _ask_deepseek(system: str, messages: list[dict], temperature: float, max_tokens: int,
                  json_object: bool) -> str:
    response = _deepseek.chat.completions.create(
        model=DEEPSEEK_MODEL, temperature=temperature, max_tokens=max_tokens,
        **({"response_format": {"type": "json_object"}} if json_object else {}),
        messages=[{"role": "system", "content": system}, *messages])
    return (response.choices[0].message.content or "").strip()


def chat(system: str, messages: list[dict], *, temperature: float = 0.2,
         max_tokens: int = 400, json_object: bool = False, caller: str = "llm") -> str | None:
    """Ask the model with a conversation: prior turns plus the current message.

    `messages` are {"role": "user" | "assistant", "content": str}, oldest
    first. With `json_object` the reply is the JSON object alone. Claude
    answers when it is up; DeepSeek when Claude fails or is set aside. Returns
    None when no provider could answer.
    """
    text = None
    if claude_ready():
        try:
            text = _ask_claude(system, messages, max_tokens, json_object)
            claude_ok()
        except Exception as err:  # noqa: BLE001 — a model failure must not break the turn
            claude_failed(err)
            log.warning("%s: Claude failed (%s)%s", caller, err,
                        "; falling back to DeepSeek" if _deepseek is not None else "")
    if text is None and _deepseek is not None:
        try:
            text = _ask_deepseek(system, messages, temperature, max_tokens, json_object)
        except Exception as err:  # noqa: BLE001
            log.warning("%s: DeepSeek failed (%s)", caller, err)
            return None
    if text is None:
        return None
    if json_object:
        return _first_json(text)
    return text or None


def complete(system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 400,
             json_object: bool = False, caller: str = "llm") -> str | None:
    """Ask the model once, without conversation history."""
    return chat(system, [{"role": "user", "content": user}], temperature=temperature,
                max_tokens=max_tokens, json_object=json_object, caller=caller)
