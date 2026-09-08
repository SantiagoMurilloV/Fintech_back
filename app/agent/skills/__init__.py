"""Skill loading.

Skills are markdown playbooks that tell the planner which tools to chain for a
family of questions, plus the fixed rules that keep answers consistent. They
are loaded once at import time and selected by keyword — no LLM involved in the
selection, so the same question always loads the same skill.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILL_DIR = Path(__file__).parent

# Keyword patterns that activate each skill file.
SKILL_TRIGGERS = {
    "monthly_close": r"resumen|cierre|cerro|ejecutivo|como va|margen|ingresos|pdf|informe",
    "anomaly_review": r"alerta|anomal|riesgo|raro|problema|rechazo",
    "schema_evolution": r"columna|campo|calculad|formula|iva|comision",
}


def _load() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(SKILL_DIR.glob("*.md"))
    }


SKILLS = _load()


def select(question: str) -> list[str]:
    """Return the names of the skills that apply to a question."""
    return [name for name, pattern in SKILL_TRIGGERS.items()
            if name in SKILLS and re.search(pattern, question, re.IGNORECASE)]


def content(names: list[str]) -> str:
    """Concatenate the selected skills, for the LLM planner's context."""
    return "\n\n---\n\n".join(SKILLS[name] for name in names if name in SKILLS)
