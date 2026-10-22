"""Run the agent evaluations against the configured database.

    python -m app.agent.evals.eval            # every case
    python -m app.agent.evals.eval otc_profit_oct_2025 orders_count

Each question goes through the real chat pipeline (graph.run) with a fresh
conversation, so this measures what a user would get. It calls the model:
it costs a few cents and needs ANTHROPIC_API_KEY. Results are printed as a
table and the exit code is the number of failures.
"""
from __future__ import annotations

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


def run_case(db, case: dict) -> dict:
    before = _scalar(db, case["must_not_change"]) if case.get("must_not_change") else None
    started = time.time()
    answer = graph.run(db, case["question"])
    seconds = round(time.time() - started, 1)
    tools = [s.get("tool") for s in answer.trace if s.get("node") == "execute"]
    shown = [b for b in answer.blocks]
    outcome = {"id": case["id"], "seconds": seconds, "planner": answer.planner, "tools": tools}

    if case.get("must_not_change"):
        after = _scalar(db, case["must_not_change"])
        outcome.update(ok=before == after, detail=f"antes {before} · después {after}")
    elif case.get("expect_text"):
        outcome.update(ok=bool(answer.text.strip()), detail=answer.text[:80])
    else:
        truth = case.get("truth")
        if truth is None:
            truth = _scalar(db, case["truth_sql"])
        truth = float(truth) if truth is not None else None
        tolerance = float(case.get("tolerance", 0.01))
        seen = (numbers_in(answer.text) + numbers_in([b.get("rows") for b in shown])
                + numbers_in([[i.get("value") for i in b.get("items", [])] for b in shown
                              if b.get("type") == "kpis"]))
        hit = truth is not None and any(abs(n - truth) <= tolerance for n in seen)
        outcome.update(ok=hit, detail=f"verdad {truth}")
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
