"""Single client for the chat model: Claude (Anthropic), DeepSeek as fallback.

Every call to the model goes through here: the planner choosing a tool, the
narrator introducing a result and the advisor answering a question. One place
decides the provider, the model, the timeout and what happens when it is not
configured — and `chat` never raises, so a model that is down degrades the
answer instead of breaking the turn.

Provider: Anthropic when ANTHROPIC_API_KEY is set (model CHAT_MODEL, by
default claude-fable-5-1); otherwise DeepSeek's OpenAI-compatible API when
DEEPSEEK_API_KEY is set; otherwise none, and the agent answers with its
deterministic summaries only.
"""
from __future__ import annotations

import json
import logging
import re

from ..config import (
    ANTHROPIC_API_KEY, CHAT_MODEL, CHAT_PROVIDER, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
)

log = logging.getLogger("llm")

TIMEOUT_SECONDS = 60
JSON_RULE = ("\n\nRespond with a single JSON object and nothing else: no prose, "
             "no markdown fences.")

_client = None
if CHAT_PROVIDER == "anthropic":
    import anthropic

    _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=TIMEOUT_SECONDS)
elif CHAT_PROVIDER == "deepseek":
    from openai import OpenAI

    _client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL,
                     timeout=TIMEOUT_SECONDS)


def is_available() -> bool:
    return _client is not None


def provider() -> str | None:
    """«anthropic», «deepseek» or None — for the health check and logs."""
    return CHAT_PROVIDER if _client is not None else None


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


def chat(system: str, messages: list[dict], *, temperature: float = 0.2,
         max_tokens: int = 400, json_object: bool = False, caller: str = "llm") -> str | None:
    """Ask the model with a conversation: prior turns plus the current message.

    `messages` are {"role": "user" | "assistant", "content": str}, oldest
    first. With `json_object` the reply is the JSON object alone. Returns None
    when the model is unavailable or fails.
    """
    if _client is None:
        return None
    try:
        if CHAT_PROVIDER == "anthropic":
            # The Anthropic SDK (2.x) takes no sampling parameters: `temperature`
            # is DeepSeek's only. Sending it made every call fail.
            response = _client.messages.create(
                model=CHAT_MODEL, max_tokens=max_tokens,
                system=system + (JSON_RULE if json_object else ""),
                messages=_merge_turns(messages))
            text = "".join(block.text for block in response.content
                           if getattr(block, "type", "") == "text").strip()
        else:
            response = _client.chat.completions.create(
                model=CHAT_MODEL, temperature=temperature, max_tokens=max_tokens,
                **({"response_format": {"type": "json_object"}} if json_object else {}),
                messages=[{"role": "system", "content": system}, *messages])
            text = (response.choices[0].message.content or "").strip()
    except Exception as err:  # noqa: BLE001 — a model failure must not break the turn
        log.warning("%s: model unavailable (%s)", caller, err)
        return None

    if json_object:
        return _first_json(text)
    return text or None


def complete(system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 400,
             json_object: bool = False, caller: str = "llm") -> str | None:
    """Ask the model once, without conversation history."""
    return chat(system, [{"role": "user", "content": user}], temperature=temperature,
                max_tokens=max_tokens, json_object=json_object, caller=caller)
