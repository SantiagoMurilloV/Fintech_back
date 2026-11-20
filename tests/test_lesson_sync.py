"""Learned lessons become pages in the company brain: written with their own
token, linked to the existing pages, without figures, and never in the way of
a chat answer."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app.agent import lesson_sync, memory
from app.database import get_db
from app.main import app
from app.models import AgentLesson, User
from app.routers import chat as chat_router
from app.services import brain, security

ANSWER = [{"role": "user", "content": "¿Cómo cerró octubre?"},
          {"role": "agent", "content": "Octubre cerró con Net Revenue de USD 169.398,34."}]


def _response(message: dict) -> NS:
    return NS(text=json.dumps(message), headers={"content-type": "application/json"},
              raise_for_status=lambda: None)


class FakeBrain:
    """A gbrain MCP endpoint holding pages per (source, slug), with put_page's
    create-only rule and delete_page's not-found refusal."""

    def __init__(self):
        self.pages: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, dict, str]] = []
        self.down = False

    @staticmethod
    def _refuse(code: str) -> NS:
        return _response({"jsonrpc": "2.0", "id": 1, "result": {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps({"error": {"code": code}})}]}})

    def post(self, url, headers, json, timeout):
        name, args = json["params"]["name"], json["params"]["arguments"]
        self.calls.append((name, args, headers["Authorization"]))
        if self.down:
            raise TimeoutError("timed out")
        key = (args.get("source_id"), args.get("slug"))
        if name == "put_page":
            if key in self.pages and not args.get("force") and not args.get("expected_revision"):
                return self._refuse("revision_required")
            self.pages[key] = args["content"]
        elif name == "delete_page":
            if key not in self.pages:
                return self._refuse("page_not_found")
            del self.pages[key]
        return _response({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": '{"status": "committed"}'}]}})


@pytest.fixture
def fake(monkeypatch):
    fake = FakeBrain()
    monkeypatch.setattr(brain, "BRAIN_URL", "http://brain.internal:8080")
    monkeypatch.setattr(brain, "BRAIN_READ_TOKEN", "read-token")
    monkeypatch.setattr(brain, "BRAIN_WRITE_TOKEN", "write-token")
    monkeypatch.setattr(brain.httpx, "post", fake.post)
    brain.reset()
    memory.clear_cache()
    yield fake
    brain.reset()


def _lesson(db, text="Recuerda que Mansa no va restado en el Net Revenue",
            history=ANSWER) -> AgentLesson:
    memory.observe(db, text, history, 7)
    return db.query(AgentLesson).order_by(AgentLesson.id.desc()).first()


def _correction(db) -> AgentLesson:
    memory.observe(db, "Eso no es, los fees de Mesta van separados de las comisiones", ANSWER, 7)
    return db.query(AgentLesson).filter_by(kind="correction").one()


# ------------------------------------------------------------------ rendering

def test_the_page_links_topics_to_real_brain_pages(db):
    lesson = _correction(db)
    page = lesson_sync.render_page(lesson)
    assert page.startswith("---\ntype: \"lesson\"\n")
    assert '"kind": ' not in page and 'kind: "correction"' in page
    assert 'source: "chat"' in page and "conversation_id: 7" in page
    assert "## Relacionado" in page
    assert "[[mandioca:companies/mandioca]]" in page
    assert "[[mandioca:wiki/concepts/fees-por-proveedor]]" in page     # proveedores
    assert "[[mandioca:notes/decisiones/costos-comisiones-gastos-separados]]" in page
    assert "[[mandioca:companies/mesta]]" in page                       # named provider


def test_every_mapped_page_exists_in_the_brain_repo():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "mandioca-brain" / "pages"
    if not root.exists():
        pytest.skip("mandioca-brain checkout not available")
    pages = {p for pages in lesson_sync.TOPIC_PAGES.values() for p in pages}
    pages |= set(lesson_sync.ENTITY_PAGES.values()) | {lesson_sync.COMPANY_PAGE}
    for qualified in pages:
        source, slug = qualified.split(":", 1)
        assert (root / source / f"{slug}.md").exists(), qualified
    assert set(lesson_sync.TOPIC_PAGES) == set(memory.TOPICS)


def test_no_business_figures_reach_the_page(db):
    memory.observe(db, "Eso no es, el total de fees de Mansa era 12.500 y no 9.800", ANSWER, 7)
    lesson = db.query(AgentLesson).one()
    page = lesson_sync.render_page(lesson)
    for figure in ("169.398", "169", "12.500", "9.800"):
        assert figure not in page
    assert "USD N" in page and "era N y no N" in page
    assert not any(ch.isdigit() for ch in lesson_sync.slug_for(lesson).split("-", 1)[1])


def test_praise_is_rendered_as_an_example_with_masked_figures(db):
    memory.observe(db, "Perfecto, gracias", ANSWER, 7)
    lesson = db.query(AgentLesson).one()
    page = lesson_sync.render_page(lesson)
    assert 'title: "Ejemplo valorado: ¿Cómo cerró octubre?"' in page
    assert "> Octubre cerró con Net Revenue de USD N" in page and "169" not in page
    assert "[[mandioca:notes/procesos/cierre-mensual-mandioca]]" in page


def test_the_slug_is_stable_and_under_the_lessons_prefix(db):
    lesson = _lesson(db)
    assert lesson_sync.slug_for(lesson) == f"lessons/{lesson.id}-recuerda-que-mansa-no-va"
    memory.observe(db, "Eso no es, Mansa no va restado en el Net Revenue", ANSWER, 7)
    correction = db.query(AgentLesson).filter_by(kind="correction").one()
    assert lesson_sync.slug_for(correction) == f"lessons/{correction.id}-mansa-no-va-restado"


# ----------------------------------------------------------------------- sync

def test_an_active_lesson_is_put_with_the_write_token(db, fake):
    lesson = _lesson(db)
    counts = lesson_sync.sync_lessons(db)
    assert counts["pushed"] == 1 and counts["pending"] == 0
    name, args, auth = fake.calls[0]
    assert name == "put_page" and auth == "Bearer write-token"
    assert args["source_id"] == "lessons" and args["slug"] == lesson_sync.slug_for(lesson)
    assert "force" not in args and args["request_id"]
    assert ("lessons", lesson_sync.slug_for(lesson)) in fake.pages
    assert lesson.brain_synced_at == lesson.updated_at


def test_an_already_synced_lesson_is_skipped(db, fake):
    _lesson(db)
    lesson_sync.sync_lessons(db)
    fake.calls.clear()
    assert lesson_sync.sync_lessons(db)["pushed"] == 0
    assert fake.calls == []


def test_a_changed_lesson_overwrites_its_page(db, fake):
    lesson = _lesson(db)
    lesson_sync.sync_lessons(db)
    _lesson(db)                                   # said again: repeats 2
    assert lesson.updated_at != lesson.brain_synced_at
    assert lesson_sync.sync_lessons(db)["pushed"] == 1
    assert [c[0] for c in fake.calls] == ["put_page", "put_page", "put_page"]
    assert fake.calls[-1][1]["force"] is True     # create-only refused, then overwrite
    assert "repeats: 2" in fake.pages[("lessons", lesson_sync.slug_for(lesson))]


def test_a_rejected_lesson_is_deleted(db, fake):
    lesson = _lesson(db)
    lesson_sync.sync_lessons(db)
    memory.decide(db, lesson.id, "rejected", "admin@test")
    counts = lesson_sync.sync_lessons(db)
    assert counts["deleted"] == 1 and fake.pages == {}
    assert fake.calls[-1][0] == "delete_page" and fake.calls[-1][1]["source_id"] == "lessons"


def test_deleting_a_page_that_is_gone_counts_as_done(db, fake):
    lesson = _lesson(db)
    lesson_sync.sync_lessons(db)
    fake.pages.clear()
    memory.decide(db, lesson.id, "pending", "admin@test")
    assert lesson_sync.sync_lessons(db)["deleted"] == 1
    assert lesson.brain_synced_at == lesson.updated_at


def test_a_never_synced_rejected_lesson_needs_no_call(db, fake):
    lesson = _lesson(db)
    memory.decide(db, lesson.id, "rejected", "admin@test")
    assert lesson_sync.sync_lessons(db)["skipped"] == 1 and fake.calls == []


def test_a_failure_stops_the_run_marks_nothing_and_retries_later(db, fake):
    _lesson(db)
    _lesson(db, "De ahora en adelante muestra los fees por proveedor")
    fake.down = True
    counts = lesson_sync.sync_lessons(db)
    assert counts["stopped"] and counts["pushed"] == 0 and counts["pending"] == 2
    assert len(fake.calls) == 1                   # stopped at the first failure
    assert db.query(AgentLesson).filter(AgentLesson.brain_synced_at.isnot(None)).count() == 0
    assert lesson_sync.sync_lessons(db)["stopped"] and len(fake.calls) == 1   # cooldown
    assert brain.ready()                          # reads are not paused by a write failure
    fake.down = False
    brain.reset()
    assert lesson_sync.sync_lessons(db)["pushed"] == 2


def test_a_refused_write_is_not_marked_synced(db, fake, monkeypatch):
    lesson = _lesson(db)
    monkeypatch.setattr(brain.httpx, "post", lambda *a, **k: FakeBrain._refuse("insufficient_scope"))
    counts = lesson_sync.sync_lessons(db)
    assert counts["failed"] == 1 and not counts["stopped"]
    assert lesson.brain_synced_at is None


def test_disabled_without_the_write_token(db, fake, monkeypatch):
    monkeypatch.setattr(brain, "BRAIN_WRITE_TOKEN", None)
    _lesson(db)
    counts = lesson_sync.sync_lessons(db)
    assert counts["enabled"] is False and counts["pushed"] == 0 and fake.calls == []
    assert brain.status()["writes_enabled"] is False


def test_the_read_token_never_writes(fake):
    brain.call("query", {"query": "x"})
    brain.write("put_page", {"slug": "lessons/x", "content": "x"})
    assert [c[2] for c in fake.calls] == ["Bearer read-token", "Bearer write-token"]


# ------------------------------------------------------------------- routes

def _bearer(db, role: str) -> dict:
    user = User(email=f"{role}@test", name=role, role=role,
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(db, monkeypatch):
    @contextmanager
    def same_session():
        yield db

    monkeypatch.setattr(lesson_sync, "open_session", same_session)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_sync_endpoint_is_admin_only(client, db, fake):
    _lesson(db)
    assert client.post("/api/agent/brain-sync").status_code == 401
    assert client.post("/api/agent/brain-sync", headers=_bearer(db, "miembro")).status_code == 403
    response = client.post("/api/agent/brain-sync", headers=_bearer(db, "admin"))
    assert response.status_code == 200 and response.json()["pushed"] == 1


def test_the_endpoint_says_when_writes_are_off(client, db, monkeypatch):
    monkeypatch.setattr(brain, "BRAIN_WRITE_TOKEN", None)
    response = client.post("/api/agent/brain-sync", headers=_bearer(db, "admin"))
    assert response.status_code == 409


def test_a_decision_syncs_in_the_background(client, db, fake):
    lesson = _lesson(db)
    lesson_sync.sync_lessons(db)
    response = client.post(f"/api/agent/lessons/{lesson.id}", json={"status": "rejected"},
                           headers=_bearer(db, "admin"))
    assert response.status_code == 200 and fake.pages == {}


def _stub_agent(monkeypatch):
    monkeypatch.setattr(chat_router, "run_agent", lambda db, question, **_: NS(
        text="Listo.", blocks=[], trace=[], intent="chat", planner="stub"))


def test_chat_answers_and_then_syncs_the_lesson(client, db, fake, monkeypatch):
    _stub_agent(monkeypatch)
    response = client.post("/api/chat", headers=_bearer(db, "miembro"),
                           json={"content": "Recuerda que Mandioca quiere el cierre siempre en USD"})
    assert response.status_code == 200 and response.json()["reply"] == "Listo."
    assert len(fake.pages) == 1


def test_chat_still_answers_when_the_brain_is_down(client, db, fake, monkeypatch):
    _stub_agent(monkeypatch)
    fake.down = True
    response = client.post("/api/chat", headers=_bearer(db, "miembro"),
                           json={"content": "Recuerda que Mandioca quiere el cierre siempre en USD"})
    assert response.status_code == 200 and response.json()["reply"] == "Listo."
    lesson = db.query(AgentLesson).one()
    assert lesson.status == "active" and lesson.brain_synced_at is None
    assert brain.status()["writes_enabled"] is True
    assert lesson_sync.last_sync()["result"]["stopped"] is True     # shown in /api/health
