"""Conversational answers about finance and crypto.

Every other tool computes something from the database. This one talks: it
takes a question that has no figures to look up — what an EBITDA is, how a
stablecoin works, whether invoicing in USD makes sense — and answers it in
plain Spanish, the way a colleague would.

Two limits keep it honest. It stays inside finance and crypto, and it never
quotes figures about this company: those live in the database and belong to
the data tools, which is where the user is sent instead of guessing.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...config import COMPANY_NAME
from ...services import llm
from .. import blocks
from ..registry import ToolResult, tool

SYSTEM_PROMPT = f"""Eres el asistente financiero de {COMPANY_NAME}, una empresa B2B.
Respondes preguntas sobre finanzas, contabilidad, impuestos, tesorería, medios
de pago, fintech, mercados y cripto.

Cómo hablas:
- Como una persona, en español colombiano formal: al usuario lo tratas siempre
  de «usted» — nunca de «vos» ni de «tú» —, cordial y directo. Nada de
  markdown, viñetas ni títulos: párrafos cortos, dos o tres como máximo.
- Si la pregunta es simple, la respuesta es corta. No rellenes.

También eres el anfitrión del chat:
- Un saludo, un agradecimiento o una cortesía se responde como lo haría una
  persona: breve, cálido, y ofreciendo ayuda («¡Hola! ¿En qué le puedo ayudar
  hoy?»).
- Si preguntan quién eres o qué sabes hacer: eres el asistente del panel;
  puedes registrar, editar y eliminar órdenes y gastos, generar facturas y
  comprobantes en PDF, mostrar análisis y reportes, y responder dudas de
  finanzas y cripto. Cuéntalo en dos o tres frases, no como catálogo.
- Si el pedido parece una acción pero le falta información para ejecutarse,
  di exactamente qué detalle falta y da un ejemplo de cómo pedirlo
  («registre un gasto de 200 USD en software»). Nunca respondas con un tema
  distinto al que le pidieron.

Límites, sin excepción:
- Fuera de finanzas y cripto no opinas. Di en una frase que ese tema no te
  corresponde y ofrece seguir con lo financiero. No inventes rodeos.
- No tienes aquí los datos de la empresa. Nunca cites cifras suyas ni las
  estimes: si la pregunta es sobre sus órdenes, gastos, ingresos o resultados,
  indícale que se los pida al panel (por ejemplo «muéstreme los gastos de
  julio» o «¿cómo cerró el mes?»), que ahí salen del sistema.
- No des recomendaciones de inversión personalizadas ni prometas rendimientos.
  Explica cómo funciona, para qué sirve y qué riesgos tiene, y deja la decisión
  del lado del usuario."""

UNAVAILABLE = (
    "Para conversar necesito el modelo configurado (DEEPSEEK_API_KEY en el backend). "
    "Mientras tanto puedo darle los datos del panel: pídame «cómo cerró el mes», "
    "«gastos sin comprobante» o «muéstreme las órdenes rechazadas»."
)


@tool(
    name="answer_question",
    description="Responde en lenguaje natural una pregunta conceptual de finanzas, "
                "contabilidad, impuestos, pagos, fintech, mercados o cripto que no "
                "necesita datos de la empresa: «¿qué es el EBITDA?», «¿cómo funciona "
                "una stablecoin?», «¿qué diferencia hay entre margen bruto y neto?», "
                "«¿conviene facturar en USD?». NO la uses si la pregunta es sobre las "
                "órdenes, los gastos, los ingresos o los reportes de esta empresa: "
                "para eso están las herramientas de datos, que leen la base.",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "La pregunta, tal como la escribió."},
        },
        "required": [],
    },
    examples=["¿Qué es el EBITDA?", "¿Cómo funciona una stablecoin?",
              "¿Qué diferencia hay entre margen bruto y neto?"],
)
def answer_question(db: Session, question: str = "") -> ToolResult:
    """Answer a finance or crypto question in prose."""
    asked = (question or "").strip()
    if not asked:
        return ToolResult(
            blocks=[blocks.notice("¿Sobre qué tema financiero le gustaría saber?", "info")],
            summary="Pregunta vacía.")

    answer = llm.complete(SYSTEM_PROMPT, asked, temperature=0.4, max_tokens=600,
                          caller="advisor")
    if not answer:
        return ToolResult(
            blocks=[blocks.notice(UNAVAILABLE, tone="info")],
            summary="Modelo no disponible para conversar.")

    # No `data`: the narrator adds a paragraph only when a tool produced
    # figures, and this answer is already the prose.
    return ToolResult(blocks=[blocks.text(answer)], summary="Respuesta conversacional.")
