"""The agent loop and its Data layer: Claude may read anything the catalog
exposes, through queries the server checks, and never writes."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.agent import loop
from app.agent.data import query
from app.models import Order, RateSnapshot


@pytest.fixture
def data(db):
    db.add_all([
        Order(id="OTC-2025-00002", customer="NEXXDI", amount=1000.0, currency="USD",
              status="approved", date="2025-10-02",
              extra={"source": "otc", "tipo": "Off-ramp", "utilidad": 7.5}),
        Order(id="OTC-2025-00003", customer="BOX WEB", amount=500.0, currency="USD",
              status="approved", date="2025-10-03",
              extra={"source": "otc", "tipo": "Off-ramp", "utilidad": 2.5}),
        RateSnapshot(source="tradingview", pair="USDT/USD", price=0.99917,
                     taken_at="2026-10-21T18:11:37"),
    ])
    db.commit()


# ------------------------------------------------------------------ the guard

@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET amount = 0",
    "SELECT * FROM users",
    "SELECT * FROM settings",
    "SELECT 1; DROP TABLE orders",
    "SELECT * FROM orders -- comment",
    "WITH x AS (SELECT * FROM refresh_tokens) SELECT * FROM x",
    "SELECT * INTO copy FROM orders",
    "SELECT * FROM sqlite_master",
])
def test_anything_but_a_read_of_the_catalog_is_refused(db, sql):
    with pytest.raises(query.QueryRefused):
        query.run(db, sql)


def test_a_read_runs_and_reports_its_coverage(db, data):
    result = query.run(db, "SELECT customer, amount FROM orders ORDER BY amount DESC")
    assert result["rows"] == [["NEXXDI", 1000.0], ["BOX WEB", 500.0]]
    assert result["coverage"] == "2 fila(s), resultado completo"


def test_rows_are_capped_and_the_cut_is_said(db, data):
    result = query.run(db, "SELECT id FROM orders", max_rows=1)
    assert result["truncated"] and "recortado" in result["coverage"]


def test_ctes_over_catalog_tables_are_fine(db, data):
    sql = ("WITH m AS (SELECT substr(date,1,7) AS mes, "
           "SUM(CAST(json_extract(extra,'$.utilidad') AS REAL)) AS u FROM orders GROUP BY 1) "
           "SELECT mes, u FROM m")
    assert query.run(db, sql)["rows"] == [["2025-10", 10.0]]


# ------------------------------------------------------------------- the loop

def call(tool_name, **inputs):
    return NS(type="tool_use", name=tool_name, id=f"tu_{tool_name}", input=inputs)


def say(text):
    return NS(type="text", text=text)


class FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = NS(create=self.create)

    def create(self, **kwargs):
        # A snapshot: the loop keeps appending to the same message list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return NS(content=self.replies.pop(0))


def test_the_loop_queries_reads_and_answers(db, data):
    claude = FakeClaude([
        [call("query_database", sql="SELECT price, taken_at FROM rate_snapshots "
                                    "WHERE source='tradingview' ORDER BY taken_at DESC LIMIT 1",
              purpose="USDT según TradingView")],
        [say("Según TradingView, el USDT está en 0.99917 (lectura de las 18:11 UTC).")],
    ])
    result = loop.run(db, "¿A cuánto está el USDT?", client=claude)
    assert result.ok and "0.99917" in result.text
    assert result.blocks[0]["type"] == "text" and result.blocks[1]["type"] == "table"
    # The second call carried the query result back to Claude.
    tool_result = claude.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "0.99917" in tool_result["content"]
    # No sampling parameters and no write tools are ever offered.
    assert "temperature" not in claude.calls[0]
    names = {spec["name"] for spec in claude.calls[0]["tools"]}
    assert "query_database" in names and "delete_order" not in names
    assert "Catálogo de datos" in claude.calls[0]["system"]


def test_a_failing_query_goes_back_to_claude_to_fix(db, data):
    claude = FakeClaude([
        [call("query_database", sql="SELECT * FROM users")],
        [call("query_database", sql="SELECT COUNT(*) FROM orders")],
        [say("Hay 2 órdenes.")],
    ])
    result = loop.run(db, "¿Cuántas órdenes hay?", client=claude)
    refused = claude.calls[1]["messages"][-1]["content"][0]
    assert refused.get("is_error") and "users" in refused["content"]
    assert result.ok and result.text == "Hay 2 órdenes."


def test_an_action_is_handed_back_not_executed(db, data):
    claude = FakeClaude([[call("request_action", tool="create_expense",
                               arguments={"description": "Hosting"})]])
    result = loop.run(db, "Registra un gasto de hosting", client=claude)
    assert result.action == ("create_expense", {"description": "Hosting"})
    assert db.query(Order).count() == 2


def test_the_loop_stops_at_its_turn_budget(db, data):
    claude = FakeClaude([[call("query_database", sql="SELECT 1 FROM orders")]
                         for _ in range(loop.MAX_TURNS)])
    result = loop.run(db, "¿?", client=claude)
    assert not result.ok and len(claude.calls) == loop.MAX_TURNS


# ------------------------------------------------------------- skills, layout

def test_skills_are_listed_and_loaded_on_demand(db, data):
    claude = FakeClaude([[call("load_skill", name="daily_close")], [say("Listo.")]])
    loop.run(db, "Prepara el cierre diario", client=claude)
    assert "daily_close:" in claude.calls[0]["system"]          # listed, not inlined
    assert "Para qué sirve" not in claude.calls[0]["system"]
    loaded = claude.calls[1]["messages"][-1]["content"][0]
    assert "Cierre diario" in loaded["content"] and not loaded.get("is_error")


def test_present_shows_only_numbers_that_came_from_a_query(db, data):
    query = call("query_database", sql="SELECT SUM(amount) AS volumen FROM orders")
    claude = FakeClaude([
        [query],
        [call("present", title="Cierre", kpis=[
            {"label": "Volumen", "value": 1500.0, "unit": "usd"},
            {"label": "Utilidad", "value": 42.0, "unit": "usd"}])],      # 42 was never queried
        [call("present", title="Cierre", kpis=[
            {"label": "Volumen", "value": 1500.0, "unit": "usd"}])],
        [say("El volumen fue USD 1.500,00.")],
    ])
    result = loop.run(db, "Cierre del día", client=claude)
    refused = claude.calls[2]["messages"][-1]["content"][0]
    assert refused.get("is_error") and "42" in refused["content"]
    kpis = next(b for b in result.blocks if b["type"] == "kpis")
    assert kpis["items"][0]["value"] == "USD 1.500,00"
    assert result.blocks[1]["type"] == "heading"           # the presented report, not the query table
