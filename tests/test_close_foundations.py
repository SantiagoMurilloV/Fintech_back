"""Month-end close foundations: the close always reaches its playbook, and the
evaluator can tell a right close (columns, order, figures) from a lucky one."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.agent import graph, learning, loop
from app.agent.evals import eval as evals
from app.models import Learning

CLOSE = "¿Cómo cerró el mes?"


@pytest.fixture
def learned_close(db):
    """A route learned before the close playbook existed: close -> summary."""
    db.add(Learning(pattern=learning._pattern(CLOSE), tool="get_period_summary",
                    arguments={}, hits=3, last_used_at="2026-01-01T00:00:00",
                    created_at="2026-01-01T00:00:00"))
    db.commit()


def test_a_learned_route_never_bypasses_the_loop_for_a_close(db, learned_close, monkeypatch):
    monkeypatch.setattr(loop, "available", lambda: True)
    plan = graph._node_plan(db, CLOSE, [], None)
    assert plan.tool == graph.LOOP_TOOL


def test_without_the_loop_the_learned_route_still_answers(db, learned_close, monkeypatch):
    monkeypatch.setattr(loop, "available", lambda: False)
    plan = graph._node_plan(db, CLOSE, [], None)
    assert plan.source in ("learned", "rules")      # unchanged behaviour


def test_learning_still_replays_questions_the_loop_does_not_own(db, monkeypatch):
    monkeypatch.setattr(loop, "available", lambda: True)
    question = "muéstrame los gastos de marketing pendientes"
    db.add(Learning(pattern=learning._pattern(question), tool="get_period_summary",
                    arguments={}, hits=1, last_used_at="2026-01-01T00:00:00",
                    created_at="2026-01-01T00:00:00"))
    db.commit()
    assert not graph._loop_owns(question)


def test_the_system_prompt_names_the_configured_company():
    prompt = loop._system("hola", None, "postgresql")
    assert "{company}" not in prompt and loop.COMPANY_NAME in prompt


# ------------------------------------------------------------- the evaluator

COLUMNS = ["USD Sent", "USDT Received", "Gross Revenue", "Net Revenue"]


def answer(columns, row, text=""):
    table = {"type": "table", "columns": [{"key": c, "label": c} for c in columns],
             "rows": [row]}
    return NS(text=text, blocks=[table])


def test_a_close_with_every_figure_and_the_columns_in_order_passes():
    case = {"expect_columns": COLUMNS}
    result = answer(COLUMNS, {c: v for c, v in zip(COLUMNS, [100.0, 99.5, 3.2, 1.1])})
    ok, _ = evals.check(case, result, {"usd_sent": 100.0, "net_revenue": 1.1})
    assert ok


def test_one_missing_figure_fails_even_when_another_matches():
    result = answer(COLUMNS, {"USD Sent": 100.0, "Net Revenue": 9.9})
    ok, detail = evals.check({}, result, {"usd_sent": 100.0, "net_revenue": 1.1})
    assert not ok and "net_revenue" in detail


def test_columns_out_of_order_fail():
    shuffled = ["Gross Revenue", "USD Sent", "USDT Received", "Net Revenue"]
    ok, detail = evals.check({"expect_columns": COLUMNS}, answer(shuffled, {}), {})
    assert not ok and "columnas" in detail


def test_extra_provider_columns_in_between_are_fine():
    with_hifi = ["USD Sent", "USDT Received", "Gross Revenue", "HiFi", "Net Revenue"]
    ok, _ = evals.check({"expect_columns": COLUMNS}, answer(with_hifi, {}), {})
    assert ok


def test_required_wording_must_be_said():
    case = {"expect_labels": ["sin dato", "antes de gastos"]}
    said = answer(["Expenses"], {"Expenses": "sin dato"},
                  text="Resultado antes de gastos y comisiones: USD 10,00.")
    silent = answer(["Expenses"], {"Expenses": 0})
    assert evals.check(case, said, {})[0]
    assert not evals.check(case, silent, {})[0]


def test_every_eval_case_is_well_formed():
    import yaml
    cases = yaml.safe_load(evals.CASES.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for case in cases:
        assert case.get("question"), case["id"]
        checks = ("truth", "truth_sql", "truths_sql", "expect_columns", "expect_labels",
                  "must_not_change", "expect_text")
        assert any(k in case for k in checks), case["id"]


# ---------------------------------------------------------- data coverage

def test_coverage_says_which_months_exist_and_which_lack_expenses(db):
    from app.agent.data import coverage
    from app.models import AccountValue, Order
    for n, day in enumerate(["2026-04-03", "2026-05-09", "2026-06-01", "2026-10-15"]):
        db.add(Order(id=f"OTC-{n}", customer="X", amount=10.0, currency="USD",
                     status="approved", date=day, extra={"source": "otc"}))
    for period in ("2026-04", "2026-05"):
        db.add(AccountValue(account_key="salaries", period=period, amount=1.0,
                            updated_at="2026-06-01T00:00:00"))
    db.add(AccountValue(account_key="sales_commissions", period="2026-05", amount=1.0,
                        updated_at="2026-06-01T00:00:00"))
    db.commit()
    text = coverage.describe(db)
    assert "Operaciones OTC (orders): 2026-04 a 2026-06, 2026-10." in text
    assert "SIN gastos: 2026-06, 2026-10" in text
    assert "SIN comisiones: 2026-04, 2026-06, 2026-10" in text


def test_coverage_never_breaks_the_turn(monkeypatch):
    from app.agent.data import coverage
    coverage.clear()
    monkeypatch.setattr(coverage, "describe", lambda db: 1 / 0)
    assert coverage.cached(NS(rollback=lambda: None)) == "(no disponible)"


def test_the_prompt_carries_the_coverage():
    prompt = loop._system("hola", None, "postgresql", "- Operaciones OTC (orders): 2026-10.")
    assert "Cobertura de datos" in prompt and "2026-10" in prompt
