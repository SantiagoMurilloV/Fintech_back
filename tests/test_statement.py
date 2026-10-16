"""Accounting plan, statement engine and history import.

The golden figures are Mandioca's own income statement for May 2026 (vs April),
recomputed by hand from its formulas: the engine must land on them exactly.
"""
from __future__ import annotations

import io

import pandas as pd
import pytest

from app.models import AccountValue
from app.services import formula, plan, plan_import, statement

APRIL = dict(usd_received=1090152.00, usdt_sent=1081796.64, usdt_received=62792946.90,
             usd_sent=62511286.63, mandioca_cost_buy=1572, mandioca_cost_sell=87058,
             sales_commissions=12169.96, salaries=12910, rent=9500, compliance=2000,
             accounting=1000, auxo=5462, one_to_many=1000, events=600, merchandizing=551.20,
             other_costs_expenses=118316.98, reals_usdt_received=0, reals_usdt_sent=0)
MAY = dict(usd_received=552798.00, usdt_sent=548527.01, usdt_received=59147036.48,
           usd_sent=58874224.79, mandioca_cost_buy=164.23, mandioca_cost_sell=101072.69,
           sales_commissions=30703.19, salaries=12892, rent=9500, compliance=2000,
           accounting=1000, auxo=5462, one_to_many=1000, events=6350, merchandizing=942,
           other_costs_expenses=15857.04, reals_usdt_received=0, reals_usdt_sent=0)


@pytest.fixture
def mandioca(db):
    plan.load_template(db, "mandioca")
    return db


def _store(db, period: str, figures: dict) -> None:
    statement.set_values(db, [{"account_key": key, "period": period, "amount": value}
                              for key, value in figures.items()], actor="test")


def _line(result: dict, key: str) -> dict:
    return next(line for line in result["rows"] if line["key"] == key)


def _workbook(sheets: dict[str, list[list]]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer) as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, header=False, index=False)
    return buffer.getvalue()


# ------------------------------------------------------------------ engine

def test_mandioca_may_statement_matches_their_figures(mandioca):
    _store(mandioca, "2026-04", APRIL)
    _store(mandioca, "2026-05", MAY)

    result = statement.compute(mandioca, "2026-05", "prev")

    assert result["compare"] == "2026-04"
    assert _line(result, "operational_volume")["value"] == 59699834.48
    assert _line(result, "gross_profit")["value"] == 277082.68
    assert _line(result, "gross_profit_on_ramp")["value"] == 4270.99
    assert _line(result, "operational_costs")["value"] == 101236.92
    assert _line(result, "operational_profit")["value"] == 175845.76
    assert _line(result, "total_costs_expenses")["value"] == 85706.23
    assert _line(result, "net_result")["value"] == 90139.53
    # Vertical analysis, as their table prints it.
    assert round(_line(result, "gross_profit_off_ramp")["vertical_pct"], 2) == 98.46
    assert round(_line(result, "operational_costs")["vertical_pct"], 2) == 36.54
    assert round(_line(result, "salaries")["vertical_pct"], 2) == 40.47
    # Horizontal analysis: the deck's «operational profit drop in 12.68 %».
    profit = _line(result, "operational_profit")
    assert profit["previous"] == 201385.63
    assert round(profit["variation_pct"], 2) == -12.68
    assert profit["tone"] == "danger"                     # a result going down is bad
    assert _line(result, "operational_costs")["tone"] == "danger"   # a cost going up is bad
    assert _line(result, "other_costs_expenses")["tone"] == "success"
    # Their «14 bits to 17 bits» is basis points.
    assert round(_line(result, "cost_to_volume")["value"], 1) == 17.0
    assert round(_line(result, "cost_to_volume")["previous"], 1) == 13.9
    assert result["missing"] == []


def test_a_missing_figure_is_flagged_never_invented(mandioca):
    figures = dict(MAY)
    figures.pop("auxo")
    _store(mandioca, "2026-05", figures)

    result = statement.compute(mandioca, "2026-05", None)

    assert _line(result, "auxo")["value"] is None
    assert _line(result, "auxo")["status"] == "missing"
    fixed = _line(result, "fixed_costs")
    assert fixed["value"] == 26392.0 and fixed["status"] == "partial"
    assert _line(result, "net_result")["status"] == "partial"
    assert result["missing"] == ["Auxo"]


def test_a_month_without_figures_says_so(mandioca):
    result = statement.compute(mandioca, "2026-01", None)
    assert result["has_data"] is False
    assert _line(result, "net_result")["value"] is None
    assert statement.to_blocks(result)[-1]["type"] == "notice"


def test_compare_with_the_same_month_last_year(mandioca):
    _store(mandioca, "2025-05", {"rent": 9000})
    _store(mandioca, "2026-05", {"rent": 9500})
    result = statement.compute(mandioca, "2026-05", "year")
    assert result["compare"] == "2025-05"
    assert _line(result, "rent")["variation"] == 500


def test_blocks_carry_the_statement_for_chat_and_pdf(mandioca):
    _store(mandioca, "2026-04", APRIL)
    _store(mandioca, "2026-05", MAY)
    out = statement.to_blocks(statement.compute(mandioca, "2026-05", "prev"))
    assert [block["type"] for block in out] == ["heading", "kpis", "table"]
    assert [item["label"] for item in out[1]["items"]] == [
        "Operational Volume", "Gross Profit", "Operational Profit", "Net Revenue"]
    names = [row["account"] for row in out[2]["rows"]]
    assert "\u00a0" * 4 + "Salaries" in names     # indented under Fixed Costs


# -------------------------------------------------------------- validation

def test_both_shipped_templates_are_valid():
    assert {item["id"] for item in plan.templates()} >= {"mandioca", "basic"}
    for template in plan.templates():
        import json
        accounts = json.loads((plan.TEMPLATES_DIR / f"{template['id']}.json").read_text())["accounts"]
        plan.normalise(accounts)


@pytest.mark.parametrize("accounts, expected", [
    ([{"key": "a", "name": "A", "kind": "total", "formula": "b"},
      {"key": "b", "name": "B", "kind": "total", "formula": "a"}], "dependiendo de sí misma"),
    ([{"key": "a", "name": "A", "kind": "total", "formula": "ghost + 1"}], "no existen: ghost"),
    ([{"key": "a", "name": "A", "kind": "input", "formula": "1 + 1"}], "quite la fórmula"),
    ([{"key": "a", "name": "A", "kind": "total"}], "necesita su fórmula"),
    ([{"key": "a", "name": "Renta"}, {"key": "b", "name": "B", "aliases": ["renta"]}],
     "se llaman igual"),
    ([{"key": "Bad Key", "name": "A"}], "la clave debe"),
    ([{"key": "a", "name": "A", "parent_key": "nobody"}], "no existe"),
    ([{"key": "a", "name": "A", "kind": "total", "formula": "2 ** 500"}], "exponente"),
])
def test_a_broken_plan_is_refused_with_the_reason(accounts, expected):
    with pytest.raises(plan.PlanError) as err:
        plan.normalise(accounts)
    assert any(expected in problem for problem in err.value.problems), err.value.problems


def test_a_template_never_overwrites_a_plan_silently(mandioca):
    with pytest.raises(plan.PlanError, match="Ya hay un plan"):
        plan.load_template(mandioca, "basic")
    plan.load_template(mandioca, "basic", replace_existing=True)
    assert {row.key for row in plan.accounts(mandioca)} >= {"revenue", "net_result"}


def test_removing_an_input_drops_its_figures(mandioca):
    _store(mandioca, "2026-05", {"rent": 9500, "auxo": 5462})
    accounts = [plan.view(row) for row in plan.accounts(mandioca) if row.key != "auxo"]
    for account in accounts:
        if account["key"] == "fixed_costs":
            account["formula"] = "salaries + rent + compliance + accounting + one_to_many"
    result = plan.replace(mandioca, accounts)
    assert result["dropped_values"] == 1
    assert {v.account_key for v in mandioca.query(AccountValue).all()} == {"rent"}


def test_values_only_land_on_inputs(mandioca):
    with pytest.raises(statement.StatementError):
        statement.set_values(mandioca, [{"account_key": "net_result", "period": "2026-05",
                                         "amount": 1}], actor="x")
    result = statement.set_values(mandioca, [
        {"account_key": "rent", "period": "2026-05", "amount": "9500"},
        {"account_key": "rent", "period": "2026-13", "amount": 1}], actor="x")
    assert result["written"] == 1 and len(result["problems"]) == 1
    statement.set_values(mandioca, [{"account_key": "rent", "period": "2026-05",
                                     "amount": None}], actor="x")
    assert mandioca.query(AccountValue).count() == 0


# ------------------------------------------------------------------ import

@pytest.mark.parametrize("cell, period", [
    ("2026-05", "2026-05"), ("May/2026", "2026-05"), ("Abr/2026", "2026-04"),
    ("Mayo 2026", "2026-05"), ("05/2026", "2026-05"), ("Apr-26", "2026-04"),
    ("septiembre de 2025", "2025-09"), (pd.Timestamp("2026-07-01"), "2026-07"),
    ("Vertical Analysis", None), ("Concepto", None), (2026, None),
])
def test_month_headers_in_the_shapes_teams_write_them(cell, period):
    assert plan_import.parse_period(cell) == period


@pytest.mark.parametrize("cell, number", [
    ("$59,699,834.48", 59699834.48), ("552,798.00", 552798.0), ("1.234,56", 1234.56),
    ("(18.00)", -18.0), ("-$12,933", -12933.0), ("12,933", 12933.0), (9500, 9500.0),
    ("36.54%", None), ("", None), ("—", None), ("USD Received", None), (float("nan"), None),
])
def test_figures_in_the_shapes_teams_write_them(cell, number):
    assert plan_import.parse_number(cell) == number


def test_a_clean_history_sheet_is_stored_and_its_totals_checked(mandioca):
    rows = [["Cuenta", "2026-04", "May/2026"]]
    rows += [[key.replace("_", " "), APRIL[key], MAY[key]] for key in APRIL]
    rows += [["Operational Profit", "$201,386", "$175,845.76"],      # April typed rounded
             ["Net Revenue", 37875.87, 95638.53],                    # May: the other version
             ["Gross Profit", "100.00%", "100.00%"]]                 # a ratio, not a figure
    content = _workbook({"Cifras": rows})

    report = plan_import.run(mandioca, content, "historia.xlsx", actor="test")

    assert report["periods"] == ["2026-04", "2026-05"]
    assert report["written"] == 36 and report["conflicts"] == []
    assert report["matched"] == 1                 # May operational profit, exact
    assert len(report["rounding"]) == 2           # April: 201,386 and 37,875.87, under a dollar
    mismatch, = report["mismatches"]
    assert (mismatch["key"], mismatch["period"]) == ("net_result", "2026-05")
    assert (mismatch["file"], mismatch["computed"]) == (95638.53, 90139.53)
    assert mismatch["difference"] == 5499.0
    assert any("la fórmula da 90.139,53" in problem for problem in report["problems"])
    assert any("2 celdas con porcentaje" in note for note in report["notes"])
    stored = statement.compute(mandioca, "2026-05", "prev")
    assert _line(stored, "net_result")["value"] == 90139.53


def test_two_versions_of_the_same_month_are_not_guessed(mandioca):
    """Their deck: one sheet leaves Auxo out and pays 12,855 in salaries."""
    vertical = [["May/2026", "Concept", "Abr/2026", "May/2026", "Vertical Analysis"],
                ["Fixed Costs", "Salaries", None, "12,855.00", "48.78%"],
                ["", "Rent", None, "9,500.00", "36.05%"],
                ["", "Fixed Costs", None, "26,355.00", "100.00%"]]
    horizontal = [["May/2026", "Concept", "Abr/2026", "May/2026", "Variation"],
                  ["", "Salaries", "12,910.00", "12,892.00", "(18.00)"],
                  ["", "Auxo", "5,462.00", "5,462.00", "0.00"],
                  ["", "Rent", "9,500.00", "9,500.00", "0.00"]]
    content = _workbook({"Vertical": vertical, "Horizontal": horizontal})

    report = plan_import.run(mandioca, content, "deck.xlsx", actor="test", dry_run=True)

    conflict, = report["conflicts"]
    assert (conflict["key"], conflict["period"]) == ("salaries", "2026-05")
    assert [v["value"] for v in conflict["values"]] == [12855.0, 12892.0]
    assert any("cifras distintas" in problem for problem in report["problems"])
    # Fixed costs cannot be checked while one of its figures is in dispute:
    # blaming the total for the gap would be a false alarm.
    assert ("fixed_costs", "2026-05") in {(u["key"], u["period"]) for u in report["unverified"]}
    assert not any(m["key"] == "fixed_costs" for m in report["mismatches"])
    # dry run: nothing written.
    assert mandioca.query(AccountValue).count() == 0


def test_a_rounded_copy_of_the_same_figure_keeps_the_precise_one(mandioca):
    precise = [["", "2026-05"], ["Mandioca Cost Buy", "164.23"]]
    rounded = [["", "2026-05"], ["Mandioca Cost Buy", "164"]]
    content = _workbook({"Detalle": precise, "Resumen": rounded})

    report = plan_import.run(mandioca, content, "x.xlsx", actor="test")

    assert report["conflicts"] == []
    assert any("redondeadas en otra hoja" in note for note in report["notes"])
    stored = mandioca.query(AccountValue).one()
    assert stored.amount == 164.23


def test_unknown_accounts_are_named_not_dropped_silently(mandioca):
    content = _workbook({"Cifras": [["Cuenta", "2026-05"], ["Rent", 9500],
                                    ["Office snacks", 120]]})
    report = plan_import.run(mandioca, content, "x.xlsx", actor="test")
    assert report["unknown"] == [{"label": "Office snacks", "where": "Cifras, fila 3"}]
    assert any("no están en el plan: Office snacks" in p for p in report["problems"])
    assert report["written"] == 1


def test_aliases_match_their_own_spelling(mandioca):
    content = _workbook({"Cifras": [["", "2026-05"], ["USDT Sended", 548527.01],
                                    ["COMPLIANCES", 2000]]})
    report = plan_import.run(mandioca, content, "x.xlsx", actor="test")
    assert report["written"] == 2


def test_a_file_without_months_explains_what_it_needs(mandioca):
    content = _workbook({"Cifras": [["Cuenta", "Valor"], ["Rent", 9500]]})
    with pytest.raises(plan_import.ImportError_, match="fila de meses"):
        plan_import.run(mandioca, content, "x.xlsx", actor="test")


def test_the_downloaded_template_imports_back(mandioca):
    _store(mandioca, "2026-05", MAY)
    content = plan_import.template_xlsx(mandioca, "2026-04", "2026-05")
    report = plan_import.run(mandioca, content, "plantilla.xlsx", actor="test", dry_run=True)
    assert report["unchanged"] == len(MAY) and report["unknown"] == []


# ----------------------------------------------------------------- formula

def test_formulas_cannot_hang_a_worker():
    assert formula.evaluate("9 ** 9 ** 9", {}) is None
    assert formula.evaluate('"a" * 10 ** 9', {}) is None
    # Each step allowed on its own, the chain is not.
    assert formula.evaluate('"a" * 9999 * 9999', {}) is None
    assert formula.evaluate("((10 ** 100) ** 100) ** 100", {}) is None
    assert formula.evaluate("upper(name) * 2", {"name": "ab"}) == "ABAB"
    assert formula.evaluate("amount * 1.19 ** 2", {"amount": 100}) == pytest.approx(141.61)
    assert formula.evaluate("rent * 2 + 1", {"rent": 10}) == 21
    assert formula.names("round(rent + salaries, 2) - abs(x)") == {"rent", "salaries", "x"}
