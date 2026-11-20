"""Conversational memory: the agent learns from every conversation.

Each user message is read, deterministically (no model call, so it works with
any chat provider), for three things:

- Topics: what the team asks about (reports, closes, metrics, calculations…).
  Counted over time, the most repeated topics become the agent's priorities:
  where answers must be deepest.
- Sentiment: praise or frustration. A negative reaction right after an answer
  marks that answer as a miss, and the topic as a weak spot.
- Lessons: a rule the user states («recuerde que…», «de ahora en adelante…»,
  «Mandioca prefiere…») is stored and active at once. A correction inferred
  from a negative reaction is stored as pending until an admin approves it, so
  a misunderstanding never turns into a rule on its own.

What is learned reaches every prompt (loop, planner and narrator) through
`context()`. Lessons are instructions about HOW to answer; figures always come
from the tools, never from memory. Nothing here may break a chat turn.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AgentLesson, AgentSignal
from .parsing import normalize

log = logging.getLogger("agent.memory")

# Topic lexicon over normalized (lower-case, accent-free) text.
TOPICS: dict[str, str] = {
    "cierre": r"\bcierre|\bcerr[oa]|fin de mes|month.?end",
    "reportes": r"\breporte|\binforme|\bpdf\b",
    "analisis": r"\banalisis|\banaliza|vertical|horizontal|tendencia",
    "calculos": r"\bcalcul|\bsuma|\btotal|promedio|porcentaje",
    "metricas": r"\bmetrica|\bkpi|\bindicador|\bmargen|\bbps?\b|puntos basicos",
    "estado de resultados": r"estado de resultados|\bp&?l\b|utilidad (neta|operacional)",
    "gastos y comisiones": r"\bgasto|\bcomision|expenses",
    "proveedores": r"\bproveedor|\bfees?\b|codex|mesta|\bt-?0\b|mansa|hifi",
    "otc y volumen": r"\botc\b|\bvolumen|\bspread|off.?ramp|on.?ramp",
    "precios": r"\bprecio|\btrm\b|cotiza|tradingview|\busdt\b|\busdc\b",
    "dashboards": r"\bdashboard|\btablero|\bgrafic",
    "comparaciones": r"\bcompar|\bvs\b|frente a|versus|\bvariacion",
}

NEGATIVE = re.compile(
    r"\bno es\b|\bno son\b|esta mal|estan mal|\bmal\b|incorrect|equivoc|"
    r"no sirve|no entend|no me sirve|otra vez|de nuevo no|no era eso|eso no|"
    r"no cuadra|no coincide|inventa|alucin|no aparece")
POSITIVE = re.compile(
    r"\bperfecto\b|\bexcelente\b|\bgracias\b|\bgenial\b|\bbien hecho\b|\bmuy bien\b|"
    r"\beso es\b|\bexacto\b|\bcorrecto\b|\bme sirve\b")
# A rule the user states for the future. Matched at the start of the message
# (or of a sentence) so «siempre» inside a question does not count.
EXPLICIT = re.compile(
    r"(?:^|[.!;]\s*)(?:recuerd[ae]|recuerda que|ten(?:ga)? en cuenta|tome nota|"
    r"de ahora en adelante|a partir de ahora|desde ahora|siempre|nunca|"
    r"prefiero|preferimos|(?:mandioca|nosotros|el equipo) (?:quiere|prefiere|necesita|usa))\b")

MAX_TEXT = 380
ACTIVE_IN_PROMPT = 15
PRIORITY_WINDOW_DAYS = 30
PRIORITY_MIN = 3          # a topic asked this often becomes a priority
WEAK_MIN = 2              # this many negative reactions mark a weak spot
CACHE_SECONDS = 60

_cache: dict[str, tuple[float, str]] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def topics_of(text: str) -> list[str]:
    plain = normalize(text)
    return [name for name, pattern in TOPICS.items() if re.search(pattern, plain)]


def sentiment_of(text: str) -> str:
    plain = normalize(text)
    negative, positive = bool(NEGATIVE.search(plain)), bool(POSITIVE.search(plain))
    if negative and not positive:
        return "negative"
    if positive and not negative:
        return "positive"
    return "neutral"


def is_explicit_rule(text: str) -> bool:
    return bool(EXPLICIT.search(normalize(text.strip())))


def _pattern(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text)).strip(" .!¡¿?")[:MAX_TEXT]


def _last_agent_reply(history: list[dict] | None) -> str | None:
    for turn in reversed(history or []):
        if turn.get("role") in ("agent", "assistant"):
            return (turn.get("content") or "").strip() or None
    return None


def _store_lesson(db: Session, text: str, kind: str, status: str, topics: list[str],
                  evidence: str | None, conversation_id: int | None) -> AgentLesson:
    pattern = _pattern(text)
    lesson = db.scalar(select(AgentLesson).where(AgentLesson.pattern == pattern))
    if lesson is not None:
        lesson.repeats += 1
        # Said explicitly after being inferred: the user confirmed it.
        if kind == "explicit" and lesson.status == "pending":
            lesson.status, lesson.kind = "active", "explicit"
        return lesson
    lesson = AgentLesson(text=text.strip()[:MAX_TEXT], pattern=pattern, kind=kind,
                         status=status, topics=topics,
                         evidence=(evidence or "")[:MAX_TEXT] or None,
                         conversation_id=conversation_id, repeats=1, created_at=_now())
    db.add(lesson)
    return lesson


def observe(db: Session, question: str, history: list[dict] | None = None,
            conversation_id: int | None = None) -> dict:
    """Read one user message and learn from it. Never raises."""
    try:
        text = (question or "").strip()
        topics = topics_of(text)
        mood = sentiment_of(text)
        db.add(AgentSignal(conversation_id=conversation_id, sentiment=mood, topics=topics,
                           excerpt=text[:300], created_at=_now()))
        learned = None
        previous = _last_agent_reply(history)
        if len(text) >= 12 and is_explicit_rule(text):
            learned = _store_lesson(db, text, "explicit", "active", topics, previous,
                                    conversation_id)
        elif mood == "negative" and previous and len(text) >= 12:
            learned = _store_lesson(db, text, "correction", "pending", topics, previous,
                                    conversation_id)
        db.commit()
        _cache.clear()
        return {"topics": topics, "sentiment": mood,
                "lesson": learned.status if learned is not None else None}
    except Exception:  # noqa: BLE001 — learning must never cost the user an answer
        log.exception("memory.observe failed")
        db.rollback()
        return {"topics": [], "sentiment": "neutral", "lesson": None}


def topic_stats(db: Session, days: int = PRIORITY_WINDOW_DAYS) -> list[dict]:
    """Per topic: how often it was asked and how often the reaction was negative."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    asked: dict[str, int] = {}
    misses: dict[str, int] = {}
    rows = db.execute(select(AgentSignal.topics, AgentSignal.sentiment)
                      .where(AgentSignal.created_at >= since)).all()
    for topics, mood in rows:
        for topic in topics or []:
            asked[topic] = asked.get(topic, 0) + 1
            if mood == "negative":
                misses[topic] = misses.get(topic, 0) + 1
    return sorted(({"topic": t, "asked": n, "negative": misses.get(t, 0)}
                   for t, n in asked.items()), key=lambda r: -r["asked"])


def _describe(db: Session) -> str:
    lessons = db.scalars(select(AgentLesson).where(AgentLesson.status == "active")
                         .order_by(AgentLesson.repeats.desc(), AgentLesson.id.desc())
                         .limit(ACTIVE_IN_PROMPT)).all()
    stats = topic_stats(db)
    priorities = [s for s in stats if s["asked"] >= PRIORITY_MIN][:5]
    weak = [s for s in stats if s["negative"] >= WEAK_MIN][:3]
    if not (lessons or priorities or weak):
        return ""
    lines = []
    if lessons:
        lines.append("Lo que el equipo le ha enseñado (cúmplalo; son reglas de forma y "
                     "criterio, las cifras siguen saliendo de las herramientas):")
        lines += [f"- {lesson.text}" for lesson in lessons]
    if priorities:
        lines.append("Lo que más pide el equipo en el último mes (dé ahí respuestas completas: "
                     "cifras, comparación y lectura): "
                     + ", ".join(f"{s['topic']} ({s['asked']})" for s in priorities) + ".")
    if weak:
        lines.append("Donde sus respuestas han sido corregidas (revise el skill y la fuente "
                     "antes de responder): "
                     + ", ".join(f"{s['topic']} ({s['negative']})" for s in weak) + ".")
    return "\n".join(lines)


def context(db: Session) -> str:
    """What was learned, as prompt text ('' when nothing yet). Never raises."""
    now = time.monotonic()
    hit = _cache.get("context")
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        text = _describe(db)
    except Exception:  # noqa: BLE001
        log.exception("memory.context failed")
        db.rollback()
        return hit[1] if hit else ""
    _cache["context"] = (now, text)
    return text


def decide(db: Session, lesson_id: int, status: str, user: str | None = None) -> AgentLesson:
    if status not in ("active", "rejected", "pending"):
        raise ValueError("Estado inválido: use active, rejected o pending.")
    lesson = db.get(AgentLesson, lesson_id)
    if lesson is None:
        raise LookupError("La lección no existe.")
    lesson.status, lesson.decided_at, lesson.decided_by = status, _now(), user
    db.commit()
    _cache.clear()
    return lesson


def overview(db: Session) -> dict:
    lessons = db.scalars(select(AgentLesson).order_by(AgentLesson.id.desc()).limit(200)).all()
    counts = dict(db.execute(select(AgentSignal.sentiment, func.count())
                             .group_by(AgentSignal.sentiment)).all())
    return {
        "lessons": [{"id": x.id, "text": x.text, "kind": x.kind, "status": x.status,
                     "topics": x.topics, "evidence": x.evidence, "repeats": x.repeats,
                     "created_at": x.created_at, "decided_by": x.decided_by}
                    for x in lessons],
        "topics": topic_stats(db),
        "sentiment": {k: counts.get(k, 0) for k in ("positive", "neutral", "negative")},
    }


def clear_cache() -> None:
    _cache.clear()
