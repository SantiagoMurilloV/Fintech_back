"""Run the agent evaluations against the configured database.

    python -m app.agent.evals.eval            # every case
    python -m app.agent.evals.eval otc_profit_oct_2025 orders_count

Each question goes through the real chat pipeline (graph.run) with a fresh
conversation, so this measures what a user would get. It calls the model:
it costs a few cents and needs ANTHROPIC_API_KEY. Results are printed as a
table and the exit code is the number of failures.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import yaml
from sqlalchemy import text

from ...database import SessionLocal
from .. import graph
from ..present import numbers_in

CASES = Path(__file__).with_name("cases.yaml")


def _scalar(db, sql: str):
    return db.execute(text(sql)).scalar()


def _seen_numbers(answer) -> list[float]:
    """Every number the user sees: the text, table rows and KPI values."""
    shown = list(answer.blocks)
    return (numbers_in(answer.text) + numbers_in([b.get("rows") for b in shown])
            + numbers_in([[i.get("value") for i in b.get("items", [])] for b in shown
                          if b.get("type") == "kpis"]))


def _table_labels(block: dict) -> list[str]:
    return [str(c.get("label") or c.get("key") or "").strip().lower()
            for c in block.get("columns") or []]


def _in_order(wanted: list[str], labels: list[str]) -> bool:
    """`wanted` appears in `labels` in that order (other columns may sit between)."""
    position = 0
    for label in labels:
        if position < len(wanted) and wanted[position] in label:
            position += 1
    return position == len(wanted)


def check(case: dict, answer, truths: dict[str, float | None]) -> tuple[bool, str]:
    """Whether the answer satisfies every expectation the case declares.

    `truths` holds the resolved figures (label -> value) that must all appear.
    `expect_columns` must show up, in order, among one table's column labels.
    `expect_labels` must each appear somewhere: text, block titles or labels.
    """
    problems: list[str] = []
    tolerance = float(case.get("tolerance", 0.01))
    seen = _seen_numbers(answer)
    for label, truth in truths.items():
        if truth is None or not any(abs(n - truth) <= tolerance for n in seen):
            problems.append(f"falta {label}={truth}")

    columns = [str(c).strip().lower() for c in case.get("expect_columns") or []]
    if columns:
        tables = [_table_labels(b) for b in answer.blocks if b.get("type") == "table"]
        if not any(_in_order(columns, labels) for labels in tables):
            problems.append("columnas fuera de orden o ausentes")

    haystack = (answer.text + " " + json.dumps(answer.blocks, ensure_ascii=False,
                                               default=str)).lower()
    for label in case.get("expect_labels") or []:
        if str(label).lower() not in haystack:
            problems.append(f"no dice «{label}»")

    if problems:
        return False, "; ".join(problems)[:160]
    return True, ", ".join(f"{k}={v}" for k, v in truths.items())[:160]


def _truths(db, case: dict) -> dict[str, float | None]:
    truths: dict[str, float | None] = {}
    if "truth" in case or "truth_sql" in case:
        value = case.get("truth")
        if value is None:
            value = _scalar(db, case["truth_sql"])
        truths["verdad"] = float(value) if value is not None else None
    for label, value in (case.get("truths") or {}).items():
        truths[label] = float(value)      # fixed figures, e.g. the client's own sheet
    for label, sql in (case.get("truths_sql") or {}).items():
        value = _scalar(db, sql)
        truths[label] = float(value) if value is not None else None
    return truths


def run_case(db, case: dict) -> dict:
    before = _scalar(db, case["must_not_change"]) if case.get("must_not_change") else None
    started = time.time()
    answer = graph.run(db, case["question"])
    seconds = round(time.time() - started, 1)
    tools = [s.get("tool") for s in answer.trace if s.get("node") == "execute"]
    outcome = {"id": case["id"], "seconds": seconds, "planner": answer.planner, "tools": tools}

    if case.get("must_not_change"):
        after = _scalar(db, case["must_not_change"])
        outcome.update(ok=before == after, detail=f"antes {before} · después {after}")
    elif case.get("expect_text"):
        outcome.update(ok=bool(answer.text.strip()), detail=answer.text[:80])
    else:
        ok, detail = check(case, answer, _truths(db, case))
        outcome.update(ok=ok, detail=detail)
    return outcome


def main(selected: list[str]) -> int:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    if selected:
        cases = [c for c in cases if c["id"] in selected]
    failures = 0
    print(f"{'caso':<32} {'ok':<4} {'seg':>5}  {'planner':<8} herramientas · detalle")
    with SessionLocal() as db:
        for case in cases:
            try:
                result = run_case(db, case)
            except Exception as err:  # noqa: BLE001 — one broken case never hides the rest
                db.rollback()
                result = {"id": case["id"], "ok": False, "seconds": 0, "planner": "-",
                          "tools": [], "detail": f"error: {err}"[:120]}
            failures += 0 if result["ok"] else 1
            print(f"{result['id']:<32} {'✓' if result['ok'] else '✗':<4} {result['seconds']:>5}  "
                  f"{result['planner']:<8} {','.join(t for t in result['tools'] if t)} · "
                  f"{result['detail']}")
    print(f"\n{len(cases) - failures}/{len(cases)} casos correctos")
    return failures


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
