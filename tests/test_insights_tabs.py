"""The analysis: tabs that keep every order state in view."""
from __future__ import annotations

import pytest

from app.models import CustomColumn, Order
from app.services import insights, llm


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *args, **kwargs: None)


@pytest.fixture
def orders(db):
    db.add(CustomColumn(entity="orders", key="network", label="network", data_type="text",
                        created_at="2026-01-01T00:00:00"))
    db.add(CustomColumn(entity="orders", key="document_number", label="document_number",
                        data_type="number", created_at="2026-01-01T00:00:00"))
    rows = []
    for i in range(30):
        status = "rejected" if i < 18 else ("approved" if i < 26 else "pending")
        rows.append(Order(id=f"O-{i}", customer="Big Client" if i < 20 else f"Small {i}",
                          amount=100.0 + i * 10, currency="USD", status=status,
                          date=f"2026-0{1 + i % 6}-1{i % 9}",
                          extra={"network": "TRON" if i % 3 else "ETHEREUM",
                                 "document_number": 1_000_000_000 + i}))
    db.add_all(rows)
    db.commit()


def _tab(result, key):
    return next(tab for tab in result["tabs"] if tab["key"] == key)


def test_tabs_in_reading_order(db, orders):
    result = insights.build(db)
    assert [t["key"] for t in result["tabs"]] == [
        "resumen", "tendencia", "clientes", "operacion", "datos"]
    # The flat list (chat, PDF) is the same content, each tab titled.
    headings = [b["content"] for b in result["blocks"] if b["type"] == "heading"]
    assert headings[0] == "Resumen" and "Datos" in headings


def test_rejected_orders_are_counted_not_hidden(db, orders):
    summary = _tab(insights.build(db), "resumen")["blocks"]
    labels = [item["label"] for item in summary[0]["items"]]
    assert labels == ["Volumen total", "Aprobadas", "Pendientes", "Rechazadas"]
    rejected = summary[0]["items"][3]
    assert rejected["tone"] == "danger" and rejected["delta"].startswith("18 órdenes")
    attention = next(b for b in summary if b["type"] == "list")
    assert any("rechazadas" in item["title"] for item in attention["items"])


def test_charts_are_themed_and_coloured_by_state(db, orders):
    operation = _tab(insights.build(db), "operacion")["blocks"]
    state_chart = next(b for b in operation if b["type"] == "chart"
                       and b["title"].startswith("Por estado"))
    assert 'data-themed="1"' in state_chart["svg"]
    assert "#e88a80" in state_chart["svg"]          # rejected reads red
    trend = _tab(insights.build(db), "tendencia")["blocks"][0]
    assert trend["chart_type"] == "stacked" and trend["wide"]


def test_identifier_columns_are_never_summed(db, orders):
    datos = _tab(insights.build(db), "datos")["blocks"]
    text = str(datos)
    assert "document_number" not in text.lower() and "Document number" not in text


def test_cache_from_an_older_layout_is_rebuilt(db, orders):
    insights._store(db, {"fingerprint": "x", "generated_at": "2026-01-01T00:00:00",
                         "result": {"blocks": [], "data": {}}})
    assert insights.get(db)["tabs"]
