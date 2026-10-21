"""The computer-use loop: show Claude the screen, do what it asks, repeat.

Uses the computer toolset (`computer_toolset_20260801`): each action is its
own tool (`screenshot`, `left_click`, `type`, `scroll`…) and every result we
send back is a fresh screenshot of the headless browser. The loop ends when
Claude answers in text instead of acting, or after `max_steps` actions —
a bound on cost as much as on time. The final text is parsed as JSON when
the task asked for JSON.
"""
from __future__ import annotations

import json
import logging
import re
import time

from ...config import ANTHROPIC_API_KEY, COMPUTER_USE_MODEL
from .browser import Browser

log = logging.getLogger("computer_use")

TOOLSET = {
    "type": "computer_toolset_20260801",
    # Members the headless browser cannot perform faithfully are withheld.
    "configs": {name: {"enabled": False} for name in (
        "zoom", "hold_key", "left_mouse_down", "left_mouse_up", "cursor_position")},
}

_client = None


def is_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _anthropic():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _image(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def _toolset(block) -> dict:
    """A result answering a toolset member must name the toolset it came from."""
    name = getattr(block, "toolset_name", None)
    return {"toolset_name": name} if name else {}


def _json_in(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def run(browser: Browser, task: str, *, max_steps: int = 8, max_seconds: int = 120,
        model: str | None = None) -> dict:
    """Do `task` on the browser's current page.

    Returns {text, json, steps, actions, error}. Stops after `max_steps`
    actions or `max_seconds`, whichever comes first — a reading that takes
    longer than that is not worth its cost.
    """
    client = _anthropic()
    started = time.monotonic()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": task}, _image(browser.screenshot())]}]
    steps, actions_log = 0, []
    while True:
        if time.monotonic() - started > max_seconds:
            return {"text": "", "json": None, "steps": steps, "actions": actions_log,
                    "error": f"sin respuesta tras {max_seconds} s"}
        response = client.messages.create(
            model=model or COMPUTER_USE_MODEL, max_tokens=1024,
            tools=[TOOLSET], messages=messages)
        actions = [block for block in response.content if block.type == "tool_use"]
        if not actions:
            text = "".join(block.text for block in response.content if block.type == "text")
            return {"text": text, "json": _json_in(text), "steps": steps,
                    "actions": actions_log, "error": None}
        if steps >= max_steps:
            return {"text": "", "json": None, "steps": steps, "actions": actions_log,
                    "error": f"sin respuesta tras {max_steps} acciones"}

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in actions:
            steps += 1
            actions_log.append(block.name)
            try:
                shot = browser.act({"action": block.name, **(block.input or {})})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                **_toolset(block), "content": [_image(shot)]})
            except Exception as err:  # noqa: BLE001 — tell Claude, let it try another way
                log.info("computer action %s failed: %s", block.name, err)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                **_toolset(block), "is_error": True,
                                "content": str(err)[:300]})
        messages.append({"role": "user", "content": results})
