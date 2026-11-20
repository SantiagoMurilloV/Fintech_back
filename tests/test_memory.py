"""Conversational memory: every message teaches the agent something, and only
what the user said outright becomes a rule without review."""
from __future__ import annotations

import pytest

from app.agent import loop, memory
from app.models import AgentLesson, AgentSignal

ANSWER = [{"role": "user", "content": "¿Cómo cerró octubre?"},
          {"role": "agent", "content": "Octubre cerró con Gross Revenue de USD 169.398,34."}]


@pytest.fixture(autouse=True)
def fresh_cache():
    memory.clear_cache()
    yield
    memory.clear_cache()


@pytest.mark.parametrize("text, topics", [
    ("Prepara el reporte de cierre de octubre", {"reportes", "cierre"}),
    ("¿Cuál fue el margen en puntos básicos?", {"metricas"}),
    ("Haz el análisis vertical del estado de resultados", {"analisis", "estado de resultados"}),
    ("Calcula el total de fees de Mesta", {"calculos", "proveedores"}),
])
def test_topics_are_read_from_the_words(text, topics):
    assert topics <= set(memory.topics_of(text))


@pytest.mark.parametrize("text, mood", [
    ("Eso no es, los fees están mal", "negative"),
    ("No cuadra con la hoja de Alejandro", "negative"),
    ("Perfecto, gracias", "positive"),
    ("¿Cuánto vendimos en mayo?", "neutral"),
])
def test_sentiment(text, mood):
    assert memory.sentiment_of(text) == mood


def test_an_explicit_rule_is_active_at_once(db):
    result = memory.observe(db, "Recuerda que Mandioca quiere el cierre siempre en USD",
                            ANSWER, 1)
    lesson = db.query(AgentLesson).one()
    assert result["lesson"] == "active" and lesson.kind == "explicit"
    assert "en USD" in memory.context(db)


def test_a_correction_that_says_what_was_wrong_is_learned_at_once(db):
    memory.observe(db, "Eso no es, Mansa no va restado en el Net Revenue", ANSWER, 1)
    lesson = db.query(AgentLesson).one()
    assert lesson.status == "active" and lesson.kind == "correction"
    assert lesson.evidence.startswith("Octubre cerró")
    assert "Mansa" in memory.context(db)


@pytest.mark.parametrize("complaint", ["Eso no es", "Está mal", "No cuadra, otra vez mal"])
def test_a_bare_complaint_is_a_miss_not_a_lesson(db, complaint):
    memory.observe(db, complaint, ANSWER, 1)
    assert db.query(AgentLesson).count() == 0
    assert db.query(AgentSignal).one().sentiment == "negative"


def test_a_negative_message_without_a_previous_answer_teaches_nothing(db):
    memory.observe(db, "Eso no es lo que necesito para el cierre", [], 1)
    assert db.query(AgentLesson).count() == 0
    assert db.query(AgentSignal).one().sentiment == "negative"


def test_repeating_a_rule_counts_instead_of_duplicating(db):
    for _ in range(2):
        memory.observe(db, "De ahora en adelante muestra los fees por proveedor", ANSWER, 1)
    assert db.query(AgentLesson).one().repeats == 2


def test_saying_outright_what_was_inferred_confirms_it(db):
    text = "Siempre muestra el margen en puntos básicos"
    memory._store_lesson(db, text, "correction", "pending", [], None, 1)
    db.commit()
    memory.observe(db, text, ANSWER, 1)
    assert db.query(AgentLesson).one().status == "active"


def test_repeated_topics_become_priorities_and_misses_weak_spots(db):
    for _ in range(3):
        memory.observe(db, "Dame el reporte de cierre del mes", [], 1)
    for _ in range(2):
        memory.observe(db, "Eso no es, el reporte de cierre está mal", ANSWER, 1)
    text = memory.context(db)
    assert "Lo que más pide el equipo" in text and "cierre (5)" in text
    assert "corregidas" in text and "reportes (2)" in text


def test_learning_never_breaks_the_chat(db, monkeypatch):
    monkeypatch.setattr(memory, "topics_of", lambda text: 1 / 0)
    assert memory.observe(db, "hola", [], 1)["lesson"] is None


def test_rejected_lessons_never_reach_the_prompt(db):
    memory.observe(db, "Recuerda que el cierre va en COP", ANSWER, 1)
    memory.decide(db, db.query(AgentLesson).one().id, "rejected")
    assert "COP" not in memory.context(db)


def test_the_loop_prompt_carries_what_was_learned():
    prompt = loop._system("hola", None, "postgresql", "-", "- Cierre siempre en USD")
    assert "Cierre siempre en USD" in prompt


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(db, role: str) -> dict:
    from app.models import User
    from app.services import security
    user = User(email=f"{role}@test", name=role, role=role,
                password_hash=security.hash_password("irrelevant-pass"),
                created_at=security.stamp())
    db.add(user)
    db.commit()
    token, _ = security.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


def test_the_endpoints_list_and_only_admins_decide(client, db):
    admin_headers, member = _bearer(db, "admin"), _bearer(db, "miembro")
    memory.observe(db, "Eso no es, los fees de Codex van separados de Mesta", ANSWER, 1)
    lesson_id = db.query(AgentLesson).one().id
    listed = client.get("/api/agent/memory", headers=admin_headers).json()
    assert listed["lessons"][0]["status"] == "active"
    assert client.post(f"/api/agent/lessons/{lesson_id}", json={"status": "rejected"},
                       headers=member).status_code == 403
    decided = client.post(f"/api/agent/lessons/{lesson_id}", json={"status": "rejected"},
                          headers=admin_headers)
    assert decided.json() == {"id": lesson_id, "status": "rejected"}
    assert db.get(AgentLesson, lesson_id).decided_by == "admin@test"


def test_praise_keeps_the_exchange_with_figures_masked(db):
    memory.observe(db, "Perfecto, gracias", ANSWER, 1)
    lesson = db.query(AgentLesson).one()
    assert lesson.kind == "praise" and lesson.status == "active"
    assert lesson.text == "¿Cómo cerró octubre?"
    assert "169" not in lesson.evidence and "USD N" in lesson.evidence
    text = memory.context(db)
    assert "valoró bien" in text and "¿Cómo cerró octubre?" in text


def test_a_reaction_is_credited_to_the_question_it_answers(db):
    for _ in range(2):
        memory.observe(db, "Excelente", ANSWER, 1)
    signal = db.query(AgentSignal).first()
    assert signal.topics == ["cierre"]
    assert "han funcionado" in memory.context(db) and "cierre (2)" in memory.context(db)
    assert db.query(AgentLesson).one().repeats == 2
