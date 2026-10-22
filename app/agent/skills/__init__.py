"""Playbooks: what the agent knows how to do, general and per client.

    playbooks/general/<name>.md           for every client (how to do a task)
    playbooks/clients/<CLIENT_ID>/*.md    for this client only:
        context.md                        what the client's business and data mean;
                                          always in the agent's context
        <name>.md                         same name as a general one → replaces it
                                          for this client; a new name → a task only
                                          this client has

Markdown, not code: adding or changing what the agent knows how to do is
editing a file. Each file starts with «# Title» and «> one-line description»;
the agent sees that catalog and loads the full text when a request needs it.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...config import CLIENT_ID

ROOT = Path(__file__).resolve().parents[3] / "playbooks"
GENERAL_DIR = ROOT / "general"
CLIENT_DIR = ROOT / "clients" / CLIENT_ID
CONTEXT = "context"

# Keyword patterns that activate each skill file.
SKILL_TRIGGERS = {
    "monthly_close": r"resumen|cierre|cerro|ejecutivo|como va|margen|ingresos|pdf|informe",
    "anomaly_review": r"alerta|anomal|riesgo|raro|problema|rechazo",
    "schema_evolution": r"columna|campo|calculad|formula|iva|comision",
}


def _load() -> tuple[dict[str, str], dict[str, str]]:
    """{name: text} and {name: "general" | "client"}; client files win by name."""
    texts, origin = {}, {}
    for folder, label in ((GENERAL_DIR, "general"), (CLIENT_DIR, "client")):
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            texts[path.stem] = path.read_text(encoding="utf-8")
            origin[path.stem] = label
    return texts, origin


SKILLS, ORIGIN = _load()


def select(question: str) -> list[str]:
    """Return the names of the skills that apply to a question."""
    return [name for name, pattern in SKILL_TRIGGERS.items()
            if name in SKILLS and re.search(pattern, question, re.IGNORECASE)]


def content(names: list[str]) -> str:
    """Concatenate the selected skills, for the LLM planner's context."""
    return "\n\n---\n\n".join(SKILLS[name] for name in names if name in SKILLS)


# Always in the agent loop's context: what this client's business and data mean.
ALWAYS = (CONTEXT,)


def catalog() -> list[dict]:
    """Every skill as {name, title, description}, from its first lines.

    A skill file starts with «# Title» and «> one-line description». The agent
    loop lists them in its prompt and loads the full text on demand
    (`load_skill`), so the model decides what knowledge it needs.
    """
    entries = []
    for name, text in SKILLS.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0].lstrip("# ").strip() if lines else name
        description = next((line.lstrip("> ").strip() for line in lines[1:3]
                            if line.startswith(">")), title)
        entries.append({"name": name, "title": title, "description": description,
                        "origin": ORIGIN.get(name, "general")})
    return entries
