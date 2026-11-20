"""search_knowledge: what the company brain knows about how things are done.

Reads the company's knowledge graph (services/brain.py): definitions of the
closing-sheet columns, accounting-plan items, providers, processes, decisions
and rules. It answers «what is», «how does Mandioca want», «why» — never «how
much». The text goes to the model only (no blocks, no figures in `data`), so
nothing from the brain can be presented as a computed number.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...services import brain
from ..registry import ToolResult, tool


SPEC = dict(
    name="search_knowledge",
    description=(
        "Consulta el cerebro de la empresa: definiciones (columnas del cierre, ítems del "
        "plan de cuentas), proveedores, procesos, decisiones y reglas de cómo la empresa "
        "quiere las cosas. Úsela para «qué es», «cómo lo quiere», «por qué» o antes de un "
        "cierre o reporte para recordar sus reglas. NO tiene cifras: los números salen "
        "siempre de las otras herramientas. Cite la página que usó."),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string",
                         "description": "Qué buscar, en palabras del negocio."},
            "page": {"type": "string",
                     "description": "Opcional: slug de una página para leerla completa "
                                    "(p. ej. companies/mandioca)."},
        },
        "required": ["question"],
    },
    examples=["¿Qué es el Net Revenue para Mandioca?"],
)


def search_knowledge(db: Session, question: str = "", page: str = "") -> ToolResult:
    if page:
        text = brain.call("get_page", {"slug": page})
    else:
        text = brain.call("query", {"query": question, "limit": 5})
    if not text:
        return ToolResult(blocks=[], data={"source": "brain", "found": False},
                          summary="El cerebro no respondió o no tiene nada sobre esto; "
                                  "responda con las demás herramientas.")
    return ToolResult(blocks=[], data={"source": "brain", "found": True},
                      summary=f"Del cerebro de la empresa ({page or question}):\n{text}")


def _register() -> None:
    tool(**SPEC)(search_knowledge)


# Registered only where the brain is configured: elsewhere the agent and its
# prompts stay exactly as they were.
if brain.enabled():
    _register()
