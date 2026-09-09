"""Single client for the chat model (DeepSeek, OpenAI-compatible).

Every call to the model goes through here: the planner choosing a tool, the
narrator introducing a result and the advisor answering a question. One place
decides the model, the timeout and what happens when it is not configured —
and `complete` never raises, so a model that is down degrades the answer
instead of breaking the turn.
"""
from __future__ import annotations

import logging

from ..config import CHAT_MODEL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

log = logging.getLogger("llm")

_client = None
if DEEPSEEK_API_KEY:
    from openai import OpenAI

    _client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def is_available() -> bool:
    return _client is not None


def chat(system: str, messages: list[dict], *, temperature: float = 0.2,
         max_tokens: int = 400, json_object: bool = False, caller: str = "llm") -> str | None:
    """Ask the model with a conversation: prior turns plus the current message.

    `messages` are {"role": "user" | "assistant", "content": str}, oldest
    first. Returns None when the model is unavailable or fails.
    """
    if _client is None:
        return None

    try:
        response = _client.chat.completions.create(
            model=CHAT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            **({"response_format": {"type": "json_object"}} if json_object else {}),
            messages=[{"role": "system", "content": system}, *messages],
        )
    except Exception as err:  # noqa: BLE001 — a model failure must not break the turn
        log.warning("%s: model unavailable (%s)", caller, err)
        return None

    return (response.choices[0].message.content or "").strip() or None


def complete(system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 400,
             json_object: bool = False, caller: str = "llm") -> str | None:
    """Ask the model once, without conversation history."""
    return chat(system, [{"role": "user", "content": user}], temperature=temperature,
                max_tokens=max_tokens, json_object=json_object, caller=caller)
