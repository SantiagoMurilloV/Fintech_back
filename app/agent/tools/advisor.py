"""Conversational answers: finance and crypto, and the conversation itself.

Every other tool computes something from the database. This one talks: it
answers a question that has no figures to look up — what an EBITDA is, how a
stablecoin works — and it follows the conversation: «¿y eso es bueno?» after a
monthly summary is answered here, from the figures the panel already showed.

Two limits keep it honest. It stays inside finance and crypto, and it never
produces a figure of its own: what the panel computed in this conversation it
may quote verbatim; anything else it sends to the data tools. A guard checks
every money figure in its answer against the transcript before it is shown.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from ...config import COMPANY_NAME
from ...services import llm
from .. import blocks, conversation, formatting, skills
from ..registry import ToolResult, all_tools, tool

SYSTEM_PROMPT = f"""You are the financial assistant of {COMPANY_NAME}, a B2B company, and
you live inside its financial panel. You answer in Spanish.

How you speak:
- Like a knowledgeable colleague, in formal Colombian Spanish: always «usted»,
  never «vos» or «tú». Cordial and direct. No markdown, no bullet points, no
  headings: short paragraphs, three at most. A simple question gets a short
  answer.
- You see the recent conversation. Follow it: a reaction such as «¿por qué?»,
  «¿eso es bueno?» or «explíqueme» refers to what was just shown. Refer back to
  it naturally instead of starting over.

Figures — the one rule that has no exceptions:
- The figures the panel computed appear in the conversation, already
  formatted. You may quote them exactly as written and comment on them.
- You never compute, derive, estimate, round or convert a figure, and you
  never introduce one that is not in the conversation. Not a sum, not a
  difference, not a percentage of your own.
- If the user asks for a figure that is not there, say so and tell them how
  to ask the panel for it (for example «¿cómo cerró agosto?», «¿qué cliente
  factura más?», «muéstreme las órdenes rechazadas»). The panel answers from
  the database.

What you know about the panel — use it to interpret, never to invent:
{{criteria}}
Mention these criteria only when the conversation is about alerts, anomalies
or risk, or the user asks for an assessment. Never claim that an alert fired
unless the conversation shows it. A «¿por qué?» after a result is answered
from that result: say what the data shows and what would need checking, and
distinguish what you know from what you suppose.

What the user can ask the panel to do (suggest these when they fit):
{{capabilities}}

You are also the host of the chat:
- A greeting, a thank-you or a courtesy gets a brief, warm human reply and an
  offer to help.
- Asked who you are or what you can do: you are the panel's assistant; you
  can record, edit and delete orders and expenses, generate invoices and
  receipts as PDF, show analyses, rankings and reports, and answer finance and
  crypto questions. Two or three sentences, not a catalogue.
- If the message looks like an action but lacks a detail, name the missing
  detail and give an example of how to ask («registre un gasto de 200 USD en
  software»). Never answer a different topic than the one asked.

Limits, without exception:
- Outside finance, accounting, payments, fintech, markets and crypto you do
  not opine. Say in one sentence that it is not your job and offer to go on
  with the financial side. No detours.
- No personalised investment advice and no promised returns. Explain how
  something works, what it is for and what its risks are; the decision stays
  with the user.

Today is {{today}}. The user is looking at: {{view}}."""

UNAVAILABLE = (
    "Para conversar necesito el modelo configurado (DEEPSEEK_API_KEY en el backend). "
    "Mientras tanto puedo darle los datos del panel: pídame «cómo cerró el mes», "
    "«gastos sin comprobante» o «muéstreme las órdenes rechazadas»."
)

NO_SUCH_FIGURE = (
    "Esa cifra no la tengo calculada en esta conversación, y no la voy a estimar. "
    "Pídasela al panel: por ejemplo «¿cómo cerró el mes?», «¿qué cliente factura más?» "
    "o «muéstreme las órdenes rechazadas», y ahí sale de la base."
)


def _capabilities() -> str:
    lines = []
    for registered in all_tools():
        if registered.examples and registered.name != "answer_question":
            lines.append(f"- {registered.examples[0]}")
    return "\n".join(lines)


def _system(view: str | None) -> str:
    return (SYSTEM_PROMPT
            .replace("{criteria}", skills.content(sorted(skills.SKILLS)) or "(none)")
            .replace("{capabilities}", _capabilities())
            .replace("{today}", date.today().isoformat())
            .replace("{view}", view or "el chat del agente"))


@tool(
    name="answer_question",
    description="Conversa: responde una pregunta conceptual de finanzas, contabilidad, "
                "impuestos, pagos, fintech, mercados o cripto («¿qué es el EBITDA?», «¿cómo "
                "funciona una stablecoin?»), o una reacción a la respuesta anterior («¿por "
                "qué?», «¿eso es bueno?», «explícame»), usando solo las cifras que el panel ya "
                "mostró en la conversación. NO la uses para pedir datos nuevos de las órdenes, "
                "los gastos o los reportes de esta empresa: para eso están las herramientas de "
                "datos, que leen la base.",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "La pregunta, tal como la escribió."},
        },
        "required": [],
    },
    examples=["¿Qué es el EBITDA?", "¿Cómo funciona una stablecoin?", "¿Y eso es bueno?"],
    conversational=True,
)
def answer_question(db: Session, question: str = "", history: list[dict] | None = None,
                    view: str | None = None) -> ToolResult:
    """Answer in prose, grounded in the conversation so far."""
    asked = (question or "").strip()
    if not asked:
        return ToolResult(
            blocks=[blocks.notice("¿Sobre qué tema financiero le gustaría saber?", "info")],
            summary="Pregunta vacía.")

    turns = conversation.recent_turns(history)
    messages = [*turns, {"role": "user", "content": asked}]
    answer = llm.chat(_system(view), messages, temperature=0.3, max_tokens=600, caller="advisor")
    answer = formatting.strip_markdown(answer) if answer else answer
    if not answer:
        return ToolResult(
            blocks=[blocks.notice(UNAVAILABLE, tone="info")],
            summary="Modelo no disponible para conversar.")

    # Every money figure in the answer must already be in the transcript.
    backing = conversation.transcript(history) + "\n" + asked
    unbacked = formatting.unbacked_figures(answer, backing)
    if unbacked:
        retry = llm.chat(
            _system(view) + "\n\nYour previous draft used figures that are not in the "
            f"conversation ({', '.join(unbacked)}). Answer again without any figure that "
            "the conversation does not contain verbatim.",
            messages, temperature=0.2, max_tokens=600, caller="advisor")
        answer = formatting.strip_markdown(retry) if retry else ""
        unbacked = formatting.unbacked_figures(answer, backing) if answer else ["(sin respuesta)"]
    if unbacked:
        return ToolResult(blocks=[blocks.notice(NO_SUCH_FIGURE, tone="warning")],
                          data={}, summary="Cifra no respaldada; respuesta retenida.")

    # No `data`: the narrator adds prose only when a tool produced figures, and
    # this answer is already the prose.
    return ToolResult(blocks=[blocks.text(answer)], summary="Respuesta conversacional.")
