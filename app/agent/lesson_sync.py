"""Every learned lesson becomes a page in the company brain.

What the team teaches the agent in chat (agent/memory.py) is mirrored into
gbrain, source `lessons`, one page per lesson under `lessons/`, linked to the
existing pages of the `mandioca` and `general` sources that cover its topics.
So the rules the team states are visible, searchable and connected next to
the definitions and processes they refine.

- Active lessons are written (`put_page`); rejected or pending ones are
  removed (`delete_page`), so the brain only holds what the agent applies.
- Pages never carry business figures: the text and the evidence are masked
  (every number becomes «N»), as praise examples already are.
- A lesson is pushed while its `updated_at` differs from `brain_synced_at`
  (the `updated_at` of the version the brain holds). It is marked synced only
  after the brain confirms; a transport failure stops the run, and the next
  run retries.
- Runs in the background after a chat answer or an admin decision, with its
  own database session. Nothing here raises: the chat never waits for, or
  fails because of, the brain.

The brain-side contract (tool names, arguments, source, slugs) lives in the
CONTRACT section below and nowhere else.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import AgentLesson
from ..services import brain
from .memory import NEGATIVE, mask_figures
from .parsing import normalize

log = logging.getLogger("agent.lesson_sync")

# ------------------------------------------------------------------ CONTRACT
# Agreed with the brain service. Adjust here only.
SOURCE_ID = "lessons"
SLUG_PREFIX = "lessons/"
PUT_TOOL = "put_page"
DELETE_TOOL = "delete_page"
# put_page without expected_revision/force is create-only; an existing page
# answers revision_required (revision_conflict on older servers). The lessons
# source is owned by this sync, so the page is then overwritten with force.
EXISTS_CODES = {"revision_required", "revision_conflict"}
# Deleting a page that is not there counts as done.
MISSING_CODES = {"page_not_found", "not_found"}
# How long put_page holds its reply waiting for the commit.
WRITE_WAIT_MS = 5000


def _request_id(tool: str, arguments: dict) -> str:
    """Deterministic per intent: a replay after a timeout is the same write."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mandioca-lessons:{tool}:"
                          + json.dumps(arguments, sort_keys=True, ensure_ascii=False)))


def put_arguments(slug: str, content: str, *, force: bool = False) -> dict:
    arguments = {"slug": slug, "content": content, "source_id": SOURCE_ID,
                 "content_origin": "user_said", "wait_ms": WRITE_WAIT_MS}
    if force:
        arguments["force"] = True
    arguments["request_id"] = _request_id(PUT_TOOL, arguments)
    return arguments


def delete_arguments(slug: str) -> dict:
    arguments = {"slug": slug, "source_id": SOURCE_ID, "force": True}
    arguments["request_id"] = _request_id(DELETE_TOOL, arguments)
    return arguments


# Memory topics (memory.TOPICS) -> the brain pages that cover them, as
# source-qualified slugs of the mandioca-brain repo.
TOPIC_PAGES: dict[str, list[str]] = {
    "cierre": ["mandioca:notes/procesos/cierre-mensual-mandioca",
               "general:notes/procesos/cierre-mensual",
               "general:notes/procesos/cierre-diario"],
    "reportes": ["mandioca:notes/procesos/cierre-mensual-mandioca",
                 "mandioca:wiki/concepts/indicadores-mandioca",
                 "mandioca:notes/decisiones/hoja-de-alejandro-solo-estructura"],
    "analisis": ["general:wiki/concepts/analisis-vertical",
                 "general:wiki/concepts/analisis-horizontal",
                 "general:notes/procesos/revision-de-anomalias"],
    "calculos": ["mandioca:wiki/concepts/net-revenue",
                 "mandioca:notes/decisiones/net-revenue-formula-no-cambia",
                 "mandioca:notes/decisiones/dato-faltante-sin-dato"],
    "metricas": ["mandioca:wiki/concepts/indicadores-mandioca",
                 "general:wiki/concepts/margen-en-puntos-basicos",
                 "general:wiki/concepts/ebitda"],
    "estado de resultados": ["general:notes/procesos/estado-de-resultados",
                             "general:wiki/concepts/estado-de-resultados-concepto",
                             "mandioca:wiki/concepts/plan-de-cuentas-mandioca"],
    "gastos y comisiones": ["mandioca:notes/decisiones/costos-comisiones-gastos-separados",
                            "mandioca:wiki/concepts/expenses",
                            "mandioca:wiki/concepts/comission",
                            "general:wiki/concepts/gasto",
                            "general:wiki/concepts/comision"],
    "proveedores": ["mandioca:wiki/concepts/fees-por-proveedor",
                    "general:wiki/concepts/costo-de-proveedor"],
    "otc y volumen": ["mandioca:notes/datos/orders-libro-otc",
                      "mandioca:wiki/concepts/item-1-transactional-volume",
                      "general:wiki/concepts/off-ramp",
                      "general:wiki/concepts/on-ramp"],
    "precios": ["mandioca:notes/datos/rate-snapshots",
                "mandioca:wiki/concepts/mesa-brl"],
    "dashboards": ["general:notes/procesos/tableros-por-area"],
    "comparaciones": ["general:wiki/concepts/analisis-horizontal",
                      "general:notes/procesos/revision-de-anomalias"],
}
COMPANY_PAGE = "mandioca:companies/mandioca"
# Providers and people named in a lesson link to their own page.
ENTITY_PAGES: dict[str, str] = {
    r"\bmansa\b": "mandioca:companies/mansa",
    r"\bcodex\b": "mandioca:companies/codex",
    r"\bmesta\b": "mandioca:companies/mesta",
    r"\bt-?0\b": "mandioca:companies/t-0",
    r"\bhifi\b": "mandioca:companies/hifi",
    r"\bconduit\b": "mandioca:companies/conduit",
    r"\breap\b": "mandioca:companies/reap",
    r"\bstillman\b": "mandioca:companies/stillman",
    r"\bone ?trade\b": "mandioca:companies/one-trade",
    r"\balejandro\b": "mandioca:people/alejandro",
    r"\bnet revenue\b": "mandioca:wiki/concepts/net-revenue",
    r"\bgross revenue\b": "mandioca:wiki/concepts/gross-revenue",
}
# ---------------------------------------------------------------------------

SYNC_LIMIT = 20
TITLE_MAX = 90
EVIDENCE_LABEL = {
    "correction": "Respuesta corregida",
    "praise": "Respuesta valorada (forma a imitar)",
    "explicit": "Respuesta previa",
}
KIND_LABEL = {
    "correction": "Corrección del equipo a una respuesta del agente.",
    "praise": "Ejemplo de respuesta que el equipo valoró bien.",
    "explicit": "Regla que el equipo le dio al agente.",
}

_lock = threading.Lock()
_last: dict = {"at": None, "result": None}


# ------------------------------------------------------------------ rendering

def _mask(text: str) -> str:
    """Numbers become «N»; the provider name T-0 is not a figure."""
    protected = re.sub(r"\b[Tt]-0\b", "\x00", text or "")
    return mask_figures(protected).replace("\x00", "T-0")


SLUG_WORDS = 5
_TRAILING = {"a", "al", "de", "del", "el", "en", "la", "las", "los", "que", "y", "o", "con", "por"}


def slug_for(lesson: AgentLesson) -> str:
    """`lessons/<id>-<first words>`: the id keeps it unique and stable (a
    lesson's text never changes), the words make it readable."""
    plain = normalize(_mask(lesson.text))
    if lesson.kind == "correction":         # «eso no es, …»: keep what was wrong
        plain = re.sub(r"^\W*(?:es|son)\b", "", NEGATIVE.sub(" ", plain).strip())
    # Masked figures read as the word «n»: dropped, so slugs carry no numbers.
    words = [w for w in re.findall(r"[a-z]+", plain) if w != "n"][:SLUG_WORDS]
    while words and words[-1] in _TRAILING:
        words.pop()
    return f"{SLUG_PREFIX}{lesson.id}" + ("-" + "-".join(words) if words else "")


def links_for(lesson: AgentLesson) -> list[str]:
    """Source-qualified slugs of the related brain pages, without repeats."""
    found = [COMPANY_PAGE]
    for topic in lesson.topics or []:
        found += TOPIC_PAGES.get(topic, [])
    plain = normalize(f"{lesson.text} {lesson.evidence or ''}")
    found += [page for pattern, page in ENTITY_PAGES.items() if re.search(pattern, plain)]
    return list(dict.fromkeys(found))


def _yaml(value) -> str:
    # JSON scalars and arrays are valid YAML flow values.
    return json.dumps(value, ensure_ascii=False)


def render_page(lesson: AgentLesson) -> str:
    """The lesson as gbrain markdown: frontmatter plus a figure-free body."""
    text = _mask(lesson.text).strip()
    title = text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1].rstrip() + "…"
    if lesson.kind == "praise":
        title = f"Ejemplo valorado: {title}"
    front = {
        "type": "lesson",
        "title": title,
        "kind": lesson.kind,
        "status": lesson.status,
        "topics": list(lesson.topics or []),
        "repeats": lesson.repeats or 1,
        "created": lesson.created_at,
        "updated": lesson.updated_at or lesson.created_at,
        "source": "chat",
        "conversation_id": lesson.conversation_id,
        "lesson_id": lesson.id,
        "tags": ["lesson", lesson.kind],
    }
    lines = ["---", *(f"{key}: {_yaml(value)}" for key, value in front.items()), "---", "",
             f"# {title}", "", KIND_LABEL.get(lesson.kind, ""), "", text, ""]
    if lesson.evidence:
        lines += [f"## {EVIDENCE_LABEL.get(lesson.kind, 'Contexto')}", "",
                  "Las cifras están ocultas como N: salen siempre de las herramientas.", "",
                  *(f"> {line}" if line.strip() else ">" for line in
                    _mask(lesson.evidence).strip().splitlines()), ""]
    lines += ["## Relacionado", "", *(f"- [[{page}]]" for page in links_for(lesson)), ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------- sync

class _Down(Exception):
    pass


def _push(lesson: AgentLesson) -> bool:
    """Write an active lesson. True when the brain has it; raises _Down."""
    slug, content = slug_for(lesson), render_page(lesson)
    outcome = brain.write(PUT_TOOL, put_arguments(slug, content))
    if outcome.status == "refused" and outcome.code in EXISTS_CODES:
        outcome = brain.write(PUT_TOOL, put_arguments(slug, content, force=True))
    if outcome.status == "down":
        raise _Down(outcome.text)
    return outcome.status == "ok"


def _remove(lesson: AgentLesson) -> bool:
    """Delete a lesson's page. True when the brain no longer has it; raises _Down."""
    outcome = brain.write(DELETE_TOOL, delete_arguments(slug_for(lesson)))
    if outcome.status == "down":
        raise _Down(outcome.text)
    return outcome.status == "ok" or outcome.code in MISSING_CODES


def _due(db: Session, limit: int) -> list[AgentLesson]:
    version = AgentLesson.updated_at
    return db.scalars(
        select(AgentLesson)
        .where(or_(AgentLesson.brain_synced_at.is_(None), version.is_(None),
                   version != AgentLesson.brain_synced_at))
        .order_by(AgentLesson.id)
        .limit(limit)).all()


def sync_lessons(db: Session, limit: int = SYNC_LIMIT) -> dict:
    """Push up to `limit` changed lessons to the brain; counts. Never raises."""
    counts = {"pushed": 0, "deleted": 0, "skipped": 0, "failed": 0, "pending": 0,
              "stopped": False, "enabled": brain.writes_enabled()}
    if not counts["enabled"]:
        return counts
    if not _lock.acquire(blocking=False):      # another run is already syncing
        counts["skipped_run"] = True
        return counts
    try:
        for lesson in _due(db, limit):
            if lesson.updated_at is None:       # rows from before the column
                lesson.updated_at = lesson.created_at
            version = lesson.updated_at
            try:
                if lesson.status == "active":
                    done = _push(lesson)
                    counts["pushed" if done else "failed"] += 1
                elif lesson.brain_synced_at is None:
                    done = True                 # never written: nothing to remove
                    counts["skipped"] += 1
                else:
                    done = _remove(lesson)
                    counts["deleted" if done else "failed"] += 1
            except _Down:
                counts["stopped"] = True
                break
            if done:
                lesson.brain_synced_at = version
            db.commit()
        counts["pending"] = len(_due(db, limit))
    except Exception:  # noqa: BLE001 — the sync is retried on the next trigger
        log.exception("lesson sync failed")
        db.rollback()
        counts["stopped"] = True
    finally:
        _lock.release()
    _last.update(at=datetime.now().isoformat(timespec="seconds"), result=counts)
    return counts


@contextmanager
def open_session():
    """A session of its own: the request's session is closed after the response."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sync_in_background() -> None:
    """Entry point for FastAPI BackgroundTasks. Never raises."""
    if not brain.writes_ready():
        return
    try:
        with open_session() as db:
            sync_lessons(db)
    except Exception:  # noqa: BLE001
        log.exception("background lesson sync failed")


def last_sync() -> dict:
    return dict(_last)
