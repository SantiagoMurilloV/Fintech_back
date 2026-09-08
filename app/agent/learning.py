"""Learning layer: the agent gets faster the more the company uses it.

Routing is deterministic — rules first, LLM planner only for new phrasings.
What this module removes is the cost of *repeated* new phrasings: when the
planner resolves one, the outcome (question shape -> tool + arguments) is
remembered, and the next identical question routes by lookup instead of a
model call. Over time the store becomes the company's own vocabulary: how
THIS team asks for things, answered instantly.

The store obeys three rules that keep it as trustworthy as the hand-written
router:

- Only non-mutating tools are learned. A creation or an edit carries
  per-instance values (amounts, ids) and must always re-parse the message.
- Time-relative arguments are never replayed. `period` is recomputed from the
  question itself on every use, so «cómo cerró el mes» learned in julio stays
  correct in agosto. Message-scoped values (attachments) are never stored.
- A learned route that fails at execution is forgotten on the spot, and
  questions that point at "whatever is on screen" are never learned, because
  their meaning depends on the screen.

The most-used entries also feed the LLM planner as examples, so even a brand
new phrasing benefits from what the agent already knows about the company.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Learning
from .parsing import extract_period, normalize
from .registry import get as get_tool
from .router import DEICTIC, Plan

# Arguments that depend on the moment or the message, never on the phrasing.
VOLATILE_ARGS = {"period", "attachment_id", "question"}

MIN_QUESTION_CHARS = 6      # "ok", "sí", "3" never become patterns
MAX_PATTERN_CHARS = 300
MAX_ENTRIES = 500           # oldest-unused entries are evicted beyond this
EXAMPLE_LIMIT = 8


def _pattern(question: str) -> str:
    return normalize(question)[:MAX_PATTERN_CHARS]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def recall(db: Session, question: str) -> Plan | None:
    """The learned route for this exact question shape, or None.

    Runs after the rules and before the LLM planner, so hand-written rules
    keep priority and a hit replaces a model call with a lookup.
    """
    pattern = _pattern(question)
    if len(pattern) < MIN_QUESTION_CHARS:
        return None
    entry = db.scalar(select(Learning).where(Learning.pattern == pattern))
    if entry is None:
        return None
    if get_tool(entry.tool) is None:      # the tool no longer exists: stale
        db.delete(entry)
        return None

    entry.hits += 1
    entry.last_used_at = _now()

    arguments = dict(entry.arguments or {})
    period = extract_period(question)     # recomputed: never replayed stale
    if period:
        arguments["period"] = period
    return Plan(entry.tool, arguments, "learned", entry.tool, "learning.recall")


def remember(db: Session, question: str, tool_name: str, arguments: dict) -> None:
    """Record how the LLM planner resolved this phrasing, when it is safe to.

    Safe means: the tool reads instead of writing, the question does not point
    at the current screen, and the stored arguments carry no volatile values.
    """
    pattern = _pattern(question)
    if len(pattern) < MIN_QUESTION_CHARS or re.search(DEICTIC, pattern):
        return
    tool = get_tool(tool_name)
    if tool is None or tool.mutates:
        return

    replayable = {key: value for key, value in (arguments or {}).items()
                  if key not in VOLATILE_ARGS}
    stamp = _now()
    entry = db.scalar(select(Learning).where(Learning.pattern == pattern))
    if entry is None:
        _evict(db)
        db.add(Learning(pattern=pattern, tool=tool_name, arguments=replayable,
                        hits=1, last_used_at=stamp, created_at=stamp))
    else:
        # The planner decided again for the same phrasing: keep the latest.
        entry.tool, entry.arguments, entry.last_used_at = tool_name, replayable, stamp


def forget(db: Session, question: str) -> None:
    """Drop the learning behind a question whose replay just failed."""
    entry = db.scalar(select(Learning).where(Learning.pattern == _pattern(question)))
    if entry is not None:
        db.delete(entry)


def _evict(db: Session) -> None:
    """Keep the store bounded: beyond MAX_ENTRIES the least-recently-used go."""
    total = db.scalar(select(func.count()).select_from(Learning)) or 0
    if total < MAX_ENTRIES:
        return
    for stale in db.scalars(select(Learning).order_by(Learning.last_used_at)
                            .limit(total - MAX_ENTRIES + 1)):
        db.delete(stale)


def examples(db: Session) -> list[dict]:
    """The most-used learnings, as few-shot guidance for the LLM planner."""
    rows = db.scalars(select(Learning).order_by(Learning.hits.desc())
                      .limit(EXAMPLE_LIMIT)).all()
    return [{"question": row.pattern, "tool": row.tool} for row in rows]


def stats(db: Session) -> dict:
    """How much the agent has learned; shown as a health signal."""
    rows = db.scalars(select(Learning)).all()
    return {"entries": len(rows), "replays": sum(max(row.hits - 1, 0) for row in rows)}
