"""Conversation memory for the language model.

The chat stores every turn (agent/routers/chat.py) and hands the history to the
graph; this module turns it into what a model call can take: the last few
turns, trimmed, with roles the API understands. The same transcript backs the
figure guard, so a number the panel already showed may be quoted again.
"""
from __future__ import annotations

# How the chat labels a speaker -> how the model API labels it.
_ROLES = {"user": "user", "agent": "assistant", "assistant": "assistant"}

RECENT_TURNS = 8          # messages kept, oldest dropped first
TURN_MAX_CHARS = 1500     # a long table transcript is cut, not dropped


def recent_turns(history: list[dict] | None, limit: int = RECENT_TURNS,
                 max_chars: int = TURN_MAX_CHARS) -> list[dict]:
    """The last `limit` messages as model messages, each trimmed to `max_chars`."""
    turns = []
    for message in (history or [])[-limit:]:
        role = _ROLES.get(message.get("role", ""), "user")
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + " […]"
        turns.append({"role": role, "content": content})
    return turns


def transcript(history: list[dict] | None, limit: int = RECENT_TURNS) -> str:
    """Plain text of the recent turns, for grounding checks."""
    return "\n".join(turn["content"] for turn in recent_turns(history, limit, 100_000))
