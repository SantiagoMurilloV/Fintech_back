"""The agent loop: Claude calls tools, reads what they return, and repeats.

The fixed pipeline (graph.py) answers with ONE tool chosen once. Questions
that need two lookups («compare USDT today with October 2025»), or that no
fixed tool covers («OTC profit of NEXXDI in May»), need a loop:

    Claude sees the question, the data catalog and the business skills
      → calls a read tool (a fixed one, or query_database with its own SQL)
      → reads the result (data + coverage)
      → calls another tool or answers

Boundaries, enforced by the server, not the prompt:
- Only read tools are offered. A write (register, edit, delete, documents)
  is handed back to graph.py through `request_action`, so drafts,
  confirmations and the rule that a written figure is parsed from the user's
  own message all keep applying.
- query_database runs read-only over catalog tables, row- and time-capped.
- At most MAX_TURNS model calls and MAX_SECONDS per question.
- The figures in the answer come from tool results; arithmetic is done in
  SQL, not by the model.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from ..config import CHAT_MODEL, CHAT_PROVIDER
from ..services import llm
from . import blocks, conversation, present, skills
from .data import catalog
from .registry import all_tools, get as get_tool

log = logging.getLogger("agent.loop")

MAX_TURNS = 6
MAX_SECONDS = 90
MAX_RESULT_CHARS = 6000
MAX_SHOWN_RESULTS = 1      # the final result; intermediate lookups stay in the trace

SYSTEM = """Usted es el asistente financiero del panel de Mandioca (rampas USD ↔ USDT/USDC,
operaciones OTC, mesa BRL). Responde preguntas sobre las órdenes y operaciones OTC, los
gastos, los precios de mercado y su historia, el estado de resultados y los reportes.

Cómo trabaja:
- Para cualquier cifra, use una herramienta. Si ninguna herramienta fija responde, use
  query_database con un SELECT sobre el catálogo de abajo. Haga sumas, diferencias y
  porcentajes DENTRO del SQL: nunca calcule usted.
- Puede encadenar varias herramientas: consulte, lea el resultado y vuelva a consultar.
  Use la menor cantidad de consultas posible.
- Si la consulta falla, lea el error y corríjala.
- Para registrar, editar, borrar o generar documentos use request_action: usted no escribe
  en la base.
- Para un pedido de los que describe un skill (reportes, cierres, análisis, tableros), cargue
  primero ese skill con load_skill y sígalo: dice qué contiene, de dónde sale cada cifra y
  cómo presentarlo. Arme los reportes y tableros con present.

Cómo responde:
- Español formal colombiano, de «usted». Directo: dos a cuatro frases, sin markdown.
- Use SOLO cifras que aparezcan en los resultados de las herramientas; no las redondee ni
  las cambie. Escríbalas en formato colombiano: punto para miles y coma decimal, con la
  moneda delante («USD 259.618,06», «0,375 %»).
- Diga en qué se basa en pocas palabras (fuente y fecha del dato, o cuántos registros).
- Si los datos no alcanzan para responder, dígalo y diga qué falta. Nunca invente.
- Fuera de finanzas, pagos, mercados y cripto no opina.

Hoy es {today}. El usuario está viendo: {view}.

Criterios del negocio:
{skills}

Skills disponibles (cárguelos con load_skill cuando el pedido lo requiera):
{catalog_skills}

Catálogo de datos (lo único que existe para usted):
{catalog}"""

REQUEST_ACTION = "request_action"
LOAD_SKILL = "load_skill"
PRESENT = "present"


@dataclass
class LoopResult:
    text: str
    blocks: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    # Set when the question is really an action: (tool, arguments) for graph.py.
    action: tuple[str, dict] | None = None
    ok: bool = True


def available() -> bool:
    return CHAT_PROVIDER == "anthropic" and llm.is_available()


def _read_tools() -> list:
    return [t for t in all_tools()
            if not t.mutates and not t.conversational and t.name != "read_attachment"]


def _write_tools() -> list:
    return [t for t in all_tools() if t.mutates]


def tool_specs() -> list[dict]:
    """Read tools as Anthropic tool definitions, plus skills, layout and the hand-off."""
    specs = [{"name": t.name, "description": t.description[:1000],
              "input_schema": t.parameters or {"type": "object", "properties": {}}}
             for t in _read_tools()]
    specs.append({
        "name": LOAD_SKILL,
        "description": "Carga el texto completo de un skill: cómo hacer una tarea del negocio.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "enum": [e["name"] for e in skills.catalog()]}},
            "required": ["name"]},
    })
    specs.append({"name": PRESENT, "description": present.DESCRIPTION,
                  "input_schema": present.SPEC})
    writes = _write_tools()
    if writes:
        specs.append({
            "name": REQUEST_ACTION,
            "description": ("El usuario pide una ACCIÓN que escribe (registrar, editar, borrar, "
                            "generar un documento). Indique cuál y con qué datos; el panel se "
                            "encarga de pedir lo que falte y de confirmar. Acciones: "
                            + "; ".join(f"{t.name}: {t.description[:140]}" for t in writes)),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": [t.name for t in writes]},
                    "arguments": {"type": "object"},
                },
                "required": ["tool"],
            },
        })
    return specs


def _system(question: str, view: str | None, dialect: str) -> str:
    listing = "\n".join(f"- {e['name']}: {e['description']}" for e in skills.catalog()
                         if e["name"] not in skills.ALWAYS)
    return SYSTEM.format(today=date.today().isoformat(), view=view or "el chat",
                         skills=skills.content(list(skills.ALWAYS)) or "(ninguno)",
                         catalog_skills=listing, catalog=catalog.describe(dialect))


def _messages(question: str, history: list[dict] | None) -> list[dict]:
    turns = conversation.recent_turns(history, 6, 600) + [{"role": "user", "content": question}]
    merged: list[dict] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += "\n\n" + turn["content"]
        else:
            merged.append(dict(turn))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


def _for_model(result) -> str:
    """What the model reads back: the computed data, trimmed, plus the summary."""
    payload = {"summary": result.summary, "data": result.data}
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "… [recortado: afine la consulta]"
    return text


def run(db: Session, question: str, history: list[dict] | None = None,
        view: str | None = None, client=None) -> LoopResult:
    """Answer one question with as many read-tool calls as it needs."""
    client = client or llm._client
    dialect = db.get_bind().dialect.name
    system = _system(question, view, dialect)
    messages = _messages(question, history)
    specs = tool_specs()
    trace: list[dict] = []
    shown: list[list[dict]] = []
    presented: list[dict] | None = None
    known: list[float] = []          # every number a tool returned in this answer
    started = time.monotonic()

    for turn in range(MAX_TURNS):
        if time.monotonic() - started > MAX_SECONDS:
            break
        tick = time.perf_counter()
        try:
            response = client.messages.create(model=CHAT_MODEL, max_tokens=1500, system=system,
                                              tools=specs, messages=messages)
        except Exception as err:  # noqa: BLE001 — the turn degrades, never breaks
            log.warning("loop: model unavailable (%s)", err)
            trace.append({"node": "loop", "turn": turn, "ok": False, "error": str(err)[:200]})
            return LoopResult("", trace=trace, ok=False)
        trace.append({"node": "loop", "turn": turn, "ok": True,
                      "ms": round((time.perf_counter() - tick) * 1000, 1)})
        calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not calls:
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            out = [blocks.text(text.strip())] if text.strip() else []
            for group in ([presented] if presented else shown[-MAX_SHOWN_RESULTS:]):
                out.extend(group)
            return LoopResult(text.strip(), out, trace)

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            arguments = dict(call.input or {})
            if call.name == REQUEST_ACTION:
                action = (arguments.get("tool"), dict(arguments.get("arguments") or {}))
                trace.append({"node": "execute", "tool": REQUEST_ACTION,
                              "args": {"tool": action[0]}, "ok": True})
                return LoopResult("", trace=trace, action=action)
            tick = time.perf_counter()
            if call.name == LOAD_SKILL:
                name = str(arguments.get("name") or "")
                text = skills.SKILLS.get(name)
                content = text or f"No existe el skill «{name}»."
                trace.append({"node": "execute", "tool": LOAD_SKILL, "args": {"name": name},
                              "ok": bool(text), "ms": 0.0})
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content,
                                **({} if text else {"is_error": True})})
                continue
            if call.name == PRESENT:
                missing = present.unbacked(arguments, known)
                if missing:
                    content = ("Estas cifras no vienen de ningún resultado de esta respuesta: "
                               + ", ".join(str(m) for m in missing[:12])
                               + ". Consúltelas primero (calcule en el SQL) y pase los valores "
                                 "tal como vinieron.")
                    ok, error = False, f"{len(missing)} cifra(s) sin respaldo"
                else:
                    layout = present.build(arguments)
                    if arguments.get("save_pdf"):
                        from ..services import financial_reports
                        saved = financial_reports.save_pdf(db, {
                            "title": arguments.get("title") or "Reporte",
                            "subtitle": arguments.get("subtitle") or "", "blocks": layout})
                        layout.append(blocks.file(f"{saved['title']}.pdf", saved["pdf_url"],
                                                  "pdf", "Guardado en Reportes."))
                    presented = layout
                    content, ok, error = "Reporte presentado al usuario.", True, None
                trace.append({"node": "execute", "tool": PRESENT, "ok": ok, "error": error,
                              "args": {"title": arguments.get("title")},
                              "ms": round((time.perf_counter() - tick) * 1000, 1)})
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content,
                                **({} if ok else {"is_error": True})})
                continue
            tool = get_tool(call.name)
            if tool is None or tool.mutates or tool.conversational:
                content, ok, error = f"La herramienta «{call.name}» no está disponible.", False, None
            else:
                try:
                    result = tool.handler(db, **arguments)
                    known.extend(present.numbers_in(result.data))
                    content, ok = _for_model(result), not result.data.get("error")
                    error = result.data.get("error")
                    if ok and result.blocks:
                        shown.append(result.blocks)
                except Exception as err:  # noqa: BLE001 — tell the model, let it adjust
                    db.rollback()
                    content, ok, error = f"Error: {str(err)[:300]}", False, str(err)[:200]
            trace.append({"node": "execute", "tool": call.name, "args": arguments, "ok": ok,
                          "error": error, "ms": round((time.perf_counter() - tick) * 1000, 1)})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content,
                            **({"is_error": True} if not ok else {})})
        messages.append({"role": "user", "content": results})

    out = [blocks.notice("No alcancé a responder con los datos en el tiempo previsto. "
                         "Pruebe con una pregunta más concreta.", tone="warning")]
    return LoopResult("", out, trace, ok=False)
