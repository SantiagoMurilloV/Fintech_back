"""Ask the agent one question from the terminal, with its full trace.

    python -m app.agent.runner "¿Cuál fue la utilidad OTC de octubre 2025?"

Runs the real pipeline against the configured database (rules, loop, tools),
prints the answer, the tables it showed and every step it took — the quickest
way to see why the agent answered what it answered.
"""
from __future__ import annotations

import json
import sys
import time

from ..database import SessionLocal
from . import graph


def main(question: str) -> None:
    with SessionLocal() as db:
        started = time.time()
        answer = graph.run(db, question)
        print(f"\nPregunta: {question}\nPlanner: {answer.planner} · {round(time.time() - started, 1)} s\n")
        for block in answer.blocks:
            if block["type"] == "text":
                print(block["content"], "\n")
            elif block["type"] == "table":
                print(f"[tabla] {block.get('caption') or ''}")
                for row in block.get("rows", [])[:10]:
                    print("   ", json.dumps(row, ensure_ascii=False, default=str))
                print()
            else:
                print(f"[{block['type']}]", str(block.get("content") or block.get("title") or "")[:200])
        print("Pasos:")
        for step in answer.trace:
            detail = step.get("args", {}).get("sql") if isinstance(step.get("args"), dict) else None
            print(f"  {step.get('node'):<8} {step.get('tool') or '':<22} "
                  f"{'ok' if step.get('ok') else 'falló'} {step.get('ms', '')} ms"
                  + (f"\n           SQL: {' '.join(detail.split())[:300]}" if detail else ""))


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "¿Cómo cerró el mes?")
