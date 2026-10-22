"""The recurring finance deliverables: daily close, statement views, dashboards."""
from __future__ import annotations

import pytest

from app.models import Order, RateSnapshot
from app.services import financial_reports as reports
from app.services import plan as plan_service
from app.services import statement


def trade(i, day, customer, usd, profit, cost, kind="Off-ramp", provider="Reap"):
    return Order(id=f"OTC-T-{i}", customer=customer, amount=usd, currency="USD",
                 status="approved", gateway=provider, date=day,
                 extra={"source": "otc", "tipo": kind, "utilidad": profit,
                        "costo_proveedor": cost, "spread_bruto": profit + cost, "activo": "USDT"})


@pytest.fixture
def data(db):
    plan_service.load_template(db, "mandioca")
    db.add_all([
        trade(1, "2025-10-14", "NEXXDI", 1000.0, 3.0, 1.0),
        trade(2, "2025-10-15", "NEXXDI", 2000.0, 5.0, 2.0),
        trade(3, "2025-10-15", "BOX WEB", 500.0, 1.0, 0.5, "On-ramp", "Mesta"),
        trade(4, "2025-09-10", "AMG", 800.0, 2.0, 1.0),
        RateSnapshot(source="coinbase_1d", pair="USDT/USD", price=1.00058,
                     rate_date="2025-10-15", taken_at="2025-10-15T23:59:59"),
        RateSnapshot(source="trm", pair="USD/COP", price=3933.31,
                     rate_date="2025-10-15", taken_at="2025-10-15T00:00:00"),
    ])
    db.commit()
    statement.set_values(db, [
        {"account_key": "usdt_received", "period": "2025-01", "amount": 1010},
        {"account_key": "usd_sent", "period": "2025-01", "amount": 1000},
        {"account_key": "salaries", "period": "2025-01", "amount": 2},
        {"account_key": "usdt_received", "period": "2025-02", "amount": 2020},
        {"account_key": "usd_sent", "period": "2025-02", "amount": 2000},
    ], actor="test")


def test_daily_close_compares_with_the_previous_trading_day(db, data):
    report = reports.daily_close(db, "2025-10-15")
    d = report["data"]
    assert (d["dia_trades"], d["dia_volume"], d["dia_profit"]) == (2, 2500.0, 6.0)
    assert d["dia_anterior_con_operaciones"] == "2025-10-14" and d["anterior_profit"] == 3.0
    assert d["mes_profit"] == 9.0 and d["usdt_usd"] == 1.00058 and d["trm"] == 3933.31
    assert [b["type"] for b in report["blocks"]][:2] == ["heading", "kpis"]


def test_a_day_without_trades_says_so(db, data):
    blocks = reports.daily_close(db, "2025-10-20")["blocks"]
    assert any(b["type"] == "notice" and "No hay operaciones" in b["content"] for b in blocks)


def test_a_range_adds_inputs_and_recomputes_totals(db, data):
    result = statement.compute_range(db, "2025-01", "2025-02", "none")
    gross = next(line for line in result["rows"] if line["key"] == "gross_profit_off_ramp")
    assert gross["value"] == 30.0                    # (1010-1000) + (2020-2000)
    one_by_one = sum(next(l for l in statement.compute(db, m, None)["rows"]
                          if l["key"] == "net_result")["value"] for m in ("2025-01", "2025-02"))
    net = next(line for line in result["rows"] if line["key"] == "net_result")
    assert net["value"] == pytest.approx(one_by_one)


def test_months_without_costs_are_flagged(db, data):
    assert statement.months_missing_costs(db, "2025-01", "2025-02") == ["2025-02"]
    report = reports.income_statement(db, "2025-01", "2025-02")
    assert report["data"]["meses_sin_gastos_cargados"] == ["2025-02"]
    assert any(b["type"] == "notice" and "sobreestimado" in b["content"] for b in report["blocks"])


@pytest.mark.parametrize("view", reports.VIEWS)
def test_every_statement_view_renders(db, data, view):
    report = reports.income_statement(db, "2025-02", "2025-02", view, "prev")
    table = next(b for b in report["blocks"] if b["type"] == "table")
    keys = {c["key"] for c in table["columns"]}
    assert ("vertical" in keys) == (view != "horizontal")
    assert ("variacion" in keys) == (view != "vertical")


@pytest.mark.parametrize("kind", reports.KINDS)
def test_every_dashboard_renders(db, data, kind):
    report = reports.dashboard(db, kind, "2025-10", "2025-10")
    assert report["blocks"][0]["type"] == "heading" and any(b["type"] == "kpis" for b in report["blocks"])


def test_operations_dashboard_figures(db, data):
    d = reports.dashboard(db, "operations", "2025-10", "2025-10")["data"]
    assert d["actual"]["trades"] == 3 and d["actual"]["volume"] == 3500.0
    assert d["actual"]["on_ramp"] == 500.0 and d["anterior"]["trades"] == 1
