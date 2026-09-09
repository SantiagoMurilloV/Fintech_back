"""Deterministic intent router.

Rules run first: a regex table maps the question straight to a tool call, with
arguments extracted from the text. Only when no rule matches is the LLM asked
to pick a tool — and even then it may only choose a registered tool and its
arguments, never produce a number.

Keeping the rules first is what makes repeated questions give byte-identical
answers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parsing import (  # noqa: F401 — re-exported: the router is their entry point
    FIELD_WORDS,
    GATEWAY_WORDS,
    MONTHS,
    STATUS_WORDS,
    creation_intent,
    extract_period,
    normalize,
    record_tool,
)

# Every way the user can name a base field of a record, in one pattern.
BASE_FIELD_WORDS = "|".join(
    pattern for entity in FIELD_WORDS.values() for pattern in entity.values())

# Un saludo o una cortesía es conversación, jamás una consulta de datos.
SMALL_TALK = (r"^\s*(hola+|holi+|ola|buenas?|buen[ao]s\s+(dias?|tardes?|noches?)|hey|hi|"
              r"hello|que\s+tal|que\s+mas|como\s+estas?|como\s+vas|gracias+|muchas\s+"
              r"gracias|ok(ey)?|listo|dale|perfecto|genial|excelente|quien\s+(eres|sos)|"
              r"que\s+(puedes|podes|sabes)\s+hacer|ayuda|help)\s*[!?.…]*\s*$")

# Pedir el documento de un movimiento NUEVO: «factura de pago de nómina por
# 1000 USD» no busca una orden existente — describe un registro por crear, con
# su PDF adjunto.
DOC_WORDS = r"\b(factura|recibo|comprobante)\w*\b"
EXPENSE_DOC_CUES = (r"\b(pago|pagos|nomina|gasto|egreso|compra|servicio|proveedor|"
                    r"arriendo|sueldo|salario|honorarios|suscripcion)\w*\b")
ORDER_DOC_CUES = r"\b(cliente|venta|orden|pedido|cobro|cobrar)\w*\b"

# Marcas de una pregunta sobre un concepto, no sobre lo que hay en la base.
CONCEPTUAL = (r"\b(que es|que son|que significa|que quiere decir|como funciona|"
              r"como se calcula|como calculo|como se hace|diferencia|"
              r"para que sirve|me conviene|conviene|vale la pena|ventajas|"
              r"desventajas|que opinas|que pensas|que piensas|recomiendas|"
              r"recomendas|explicame|explicar|que riesgo tiene|es seguro|"
              r"es buena idea|deberia|por que existe|en que consiste)\b")

# Marcas de que la pregunta apunta a los registros propios: una acción sobre
# el panel, un periodo o un id. Cualquiera devuelve la frase a las reglas de
# siempre. Los posesivos quedan fuera a propósito: «si mis costos son en pesos»
# sigue siendo una pregunta conceptual.
OWN_DATA = (r"\b(este mes|mes pasado|del mes|del periodo|muestra\w*|muestrame|"
            r"listame|lista\w*|dame|cuant\w+|genera\w*|generame|registra\w*|"
            r"grafica\w*|exporta\w*|importa\w*|sincroniza\w*|ord-?\s*\d+|"
            + "|".join(MONTHS) + r")\b")

# Preguntar por los comprobantes que faltan es un listado de gastos; generar
# un comprobante es otra cosa. La negación decide cuál de las dos.
MISSING_RECEIPT = (r"\bsin comprobante\b|\bno tienen?\s+comprobante\b|"
                   r"\bfaltan?\s+comprobante\b|\bcomprobantes?\s+faltantes?\b|"
                   r"\bfalta\s+el\s+comprobante\b")

CHART_TYPE_WORDS = {
    "torta": "pie", "pastel": "pie", "pie": "pie", "circular": "pie",
    "linea": "line", "lineas": "line", "line": "line", "tendencia": "line",
    "barra": "bar", "barras": "bar", "bar": "bar", "columna": "bar",
    "horizontal": "hbar",
}

CHART_SOURCE_WORDS = [
    (r"semana|semanal|weekly", "weekly_revenue"),
    (r"gateway|pasarela|wompi|epayco|bold|stripe", "gateway_volume"),
    (r"categor", "expense_categories"),
    (r"estado|status", "order_status"),
]


@dataclass
class Plan:
    """What the router decided: a tool, its arguments and why."""
    tool: str
    arguments: dict = field(default_factory=dict)
    source: str = "rules"          # "rules" | "llm" | "fallback"
    intent: str = ""
    rule: str | None = None
    # Records a reference matched when it was not conclusive, so the turn can
    # ask which one instead of editing the wrong row.
    candidates: list = field(default_factory=list)


# Which tool a bare "¿y esto?" means, depending on the screen the user is on.
VIEW_TOOLS = {
    "orders": ("list_orders", {"limit": 10}, "las órdenes"),
    "expenses": ("list_expenses", {"limit": 10}, "los gastos"),
    "reports": ("get_kpi_dashboard", {}, "el análisis del periodo"),
    "library": ("list_saved_reports", {}, "los reportes guardados"),
}

# Phrases that point at "whatever is on screen" instead of naming a subject.
DEICTIC = r"\b(esto|esta|este|aqui|ahi|pantalla|vista|lo que veo|esta tabla|esta lista)\b"

# Short reactions to what the assistant just said: «¿por qué?», «¿eso es
# bueno?», «explícame». They are about the previous answer, so they go to the
# conversational tool, which sees the history — never to a data tool that
# would answer something else.
FOLLOW_UP = (r"^[\s¿¡]*(y\s+)?(por\s*que|porque|eso es (bueno|malo|normal|grave|mucho|poco)|"
             r"es (bueno|malo|normal|grave|preocupante|mucho|poco)|esta (bien|mal)|"
             r"que opinas?|que te parece|que significa( eso)?|que quiere decir( eso)?|"
             r"explica\w*|no entiendo|entiendo|como (lo )?interpreto|"
             r"que (deberia|debo|puedo|tengo que) hacer|que me recomiendas|"
             r"deberia preocuparme|me preocupa|en que me afecta|dame (mas )?contexto|"
             r"mas detalles?|detallame|ayudame a entender|en pocas palabras|y eso|"
             r"como asi|de verdad|seguro|en serio)\b.{0,60}$")

# Words that ask for a ranking: the largest, the smallest, the one with the most.
SUPERLATIVE = (r"\b(mas (grande|alta|alto|elevad\w*|car[ao]|costos\w*|pequen\w*|baj[ao]|"
               r"chic[ao]|barat[ao]|ordenes|pedidos|operaciones|transacciones)|mayor(es)?|"
               r"menor(es)?|maxim\w*|minim\w*|top|ranking|principal(es)?|mejor(es)?|peor(es)?|"
               r"que mas (factura|compra|paga|vende|gasta|opera)|"
               r"(factura|compra|paga|vende|gasta|opera|tiene|con) mas|"
               r"quien(es)? mas|(clientes?|orden(es)?) (que )?mas)\b")

# Verbs that mean "do something", not "show me something". The screen-context
# shortcuts must never swallow these: "crea una orden aprobada" is a creation,
# not a request to list approved orders.
ACTION_VERBS = (
    r"\b(crea|crear|creame|agrega|agregame|anade|anadir|registra|registrar|elimina|eliminar|"
    r"borra|borrar|quita|quitar|remueve|remover|importa|importar|sincroniza|sincronizar|"
    r"genera|generar|generame|grafica|graficar|actualiza|actualizar|modifica|modificar|"
    r"cambia|cambiar|adjunta|adjuntar|vincula|vincular|sube|subir|exporta|exportar)\w*\b"
)


def _limit(text: str, default: int = 10) -> int:
    match = re.search(r"\b(\d{1,3})\b", normalize(text))
    if not match:
        return default
    value = int(match.group(1))
    return value if 1 <= value <= 50 else default


def _status(normalized: str) -> str | None:
    for stem, code in STATUS_WORDS.items():
        if stem in normalized:
            return code
    return None


def _gateway(normalized: str) -> str | None:
    for word, name in GATEWAY_WORDS.items():
        if word in normalized:
            return name
    return None


def _mentions_latest(normalized: str) -> bool:
    """"el último gasto", "la más reciente" -> take the newest record."""
    return bool(re.search(r"\bultim\w*\b|\breciente\w*\b", normalized))


def _chart_type(normalized: str) -> str:
    for word, kind in CHART_TYPE_WORDS.items():
        if word in normalized:
            return kind
    return "bar"


def _chart_source(normalized: str) -> str:
    for pattern, source in CHART_SOURCE_WORDS:
        if re.search(pattern, normalized):
            return source
    return "weekly_revenue"


def route(question: str, context: dict | None = None) -> Plan | None:
    """Map a question to a tool call, or None when no rule is confident.

    `context` carries the screen the user is on (e.g. {"view": "orders"}), which
    resolves questions that point at the current view without naming it.
    """
    normalized = normalize(question)
    period = extract_period(question)
    view = (context or {}).get("view")

    def with_period(arguments: dict) -> dict:
        if period:
            arguments["period"] = period
        return arguments

    # Screen-context shortcuts only apply to questions, never to commands.
    is_command = bool(re.search(ACTION_VERBS, normalized))

    # "¿Y esto?" / "resúmeme esta pantalla" -> whatever the current view shows.
    if view in VIEW_TOOLS and not is_command and re.search(DEICTIC, normalized):
        tool_name, base_arguments, _ = VIEW_TOOLS[view]
        arguments = with_period(dict(base_arguments))
        status = _status(normalized)
        if tool_name == "list_orders" and status:
            arguments["status"] = status
        return Plan(tool_name, arguments, "rules", f"view:{view}", "context.deictic")

    # On the orders screen a bare "las rechazadas" already means orders.
    if (view == "orders" and not is_command and _status(normalized)
            and not re.search(r"\bgastos?\b", normalized)):
        arguments = with_period({"limit": _limit(question), "status": _status(normalized)})
        gateway = _gateway(normalized)
        if gateway:
            arguments["gateway"] = gateway
        return Plan("list_orders", arguments, "rules", "list_orders", "context.orders.status")

    # --- Small talk --------------------------------------------------------
    # El chat también saluda: «hola» respondido con un resumen financiero es
    # absurdo. La herramienta conversacional contesta como una persona.
    if re.search(SMALL_TALK, normalized):
        return Plan("answer_question", {}, "rules", "answer_question", "advisor.smalltalk")

    # --- Follow-ups --------------------------------------------------------
    # A reaction to the previous answer stays in the conversation.
    if (not is_command and re.search(FOLLOW_UP, normalized)
            and not re.search(OWN_DATA, normalized)):
        return Plan("answer_question", {}, "rules", "answer_question", "advisor.followup")

    # --- Conceptual questions --------------------------------------------
    # "¿Qué es el EBITDA?", "¿me conviene facturar en USD?", "¿qué riesgo tiene
    # cobrar en USDT?" hablan de un concepto, no de los registros de la
    # empresa. Sin esta regla, palabras como «margen», «factura» o «riesgo»
    # las capturaría una regla de datos y devolvería una tabla en vez de una
    # respuesta. Solo aplica cuando la frase no apunta a datos propios ni es
    # una orden de hacer algo.
    if (not is_command and re.search(CONCEPTUAL, normalized)
            and not re.search(OWN_DATA, normalized)):
        return Plan("answer_question", {}, "rules", "answer_question", "advisor.concept")

    # --- Rankings --------------------------------------------------------
    # «la orden más grande», «el cliente con la orden más elevada», «qué cliente
    # factura más», «top 5 clientes por número de órdenes». Sorting is a tool's
    # job: a listing cannot answer it and the narrator must not guess it. It
    # runs before the document rules so «qué cliente factura más» is a ranking,
    # not an invoice.
    if not is_command and re.search(SUPERLATIVE, normalized):
        arguments = with_period({"limit": _limit(question, default=5)})
        status = _status(normalized)
        if status:
            arguments["status"] = status
        counts_orders = re.search(
            r"\b(numero|cantidad|volumen)\s+de\s+(ordenes|pedidos)\b|\bmas\s+(ordenes|pedidos)\b",
            normalized)
        if re.search(r"\b(orden(es)?|pedidos?|pagos?|transaccion\w*|tickets?)\b", normalized) \
                and not counts_orders:
            if re.search(r"\bmenor(es)?\b|\bmas (pequen\w*|baj[ao]|chic[ao]|barat[ao])\b|\bminim\w*",
                         normalized):
                arguments["order"] = "asc"
            return Plan("rank_orders", arguments, "rules", "rank_orders", "ranking.orders")
        if re.search(r"\bclientes?\b", normalized):
            arguments["metric"] = "count" if counts_orders else "amount"
            return Plan("rank_customers", arguments, "rules", "rank_customers", "ranking.customers")
        # A superlative without a clear subject («no, con el monto más grande»)
        # refines the previous turn; no later rule fits it, so the planner,
        # which sees the history, decides.

    # --- Documents -------------------------------------------------------
    if re.search(r"\b(lee|leer|revisa|que dice|contenido)\b.*\b(pdf|documento|archivo|adjunto|imagen|factura)\b",
                 normalized) or re.search(r"\b(adjunt|subi|subo)\w*\b.*\b(que dice|lee)\b", normalized):
        return Plan("read_attachment", {}, "rules", "read_document", "document.read")
    if re.search(r"\bimporta\w*\b.*\b(excel|csv|hoja|archivo|adjunto)\b", normalized):
        kind = "expenses" if re.search(r"gasto", normalized) else "orders"
        return Plan("import_attachment", {"kind": kind}, "rules", "import_document", "document.import")
    # Nombrar «el comprobante del gasto 12» junto a un archivo es vincularlo;
    # pedir que lo genere es otra herramienta, así que los verbos de creación
    # quedan fuera de esta regla.
    if re.search(r"\b(adjunta|vincula|asocia)\b.*\bcomprobante\b", normalized) or (
            re.search(r"\bcomprobante\b.*\bgasto\s+#?\d+", normalized)
            and not re.search(r"\b(genera|crea|dame|quiero|necesito|descarga|imprime)\w*\b",
                              normalized)):
        match = re.search(r"gasto\s+#?(\d+)", normalized)
        if match:
            return Plan("attach_receipt_to_expense", {"expense_id": int(match.group(1))},
                        "rules", "attach_receipt", "document.attach")

    # --- Documento de un movimiento nuevo ---------------------------------
    # Con monto y sin referencia a un registro existente, «generame una
    # factura de X por N» es crear el registro Y documentarlo: el PDF queda
    # adjunto (gastos) o en la biblioteca (órdenes). Sin pista de si es gasto
    # u orden, se pregunta en vez de adivinar.
    from .parsing import parse_amount, record_id
    if (re.search(DOC_WORDS, normalized) and parse_amount(question)
            and record_id(question) is None
            and not re.search(r"\bdel?\s+(gasto|orden)\b", normalized)):
        is_expense = bool(re.search(EXPENSE_DOC_CUES, normalized))
        is_order = bool(re.search(ORDER_DOC_CUES, normalized))
        if is_expense and not is_order:
            return Plan("create_expense", {"with_receipt": True}, "rules",
                        "create_expense", "records.document.expense")
        if is_order and not is_expense:
            return Plan("create_order", {"with_invoice": True}, "rules",
                        "create_order", "records.document.order")
        return Plan("create_document", {"source_text": question}, "rules",
                    "create_document", "records.document.ask")

    # --- Reporting -------------------------------------------------------
    # Solo el sustantivo: «facturamos» y «facturar» hablan de ingresos o de una
    # decisión, no de emitir un documento.
    if re.search(r"\bfacturas?\b", normalized):
        order_id = re.search(r"\bord-?\s*(\d+)\b", normalized)
        if order_id:
            return Plan("generate_invoice", {"order_id": f"ORD-{order_id.group(1)}"},
                        "rules", "invoice", "report.invoice")
        external = re.search(r"\bext-(\w{1,14})\b", normalized)
        if external:
            return Plan("generate_invoice", {"order_id": f"EXT-{external.group(1).upper()}"},
                        "rules", "invoice", "report.invoice")
        # "factura del último gasto" is really a receipt request.
        if re.search(r"\bgastos?\b", normalized):
            return Plan("generate_receipt", {"latest": True} if _mentions_latest(normalized) else {},
                        "rules", "receipt", "report.receipt.latest")
        # No order named: the tool lists candidates instead of failing.
        return Plan("generate_invoice", {}, "rules", "invoice", "report.invoice.pick")
    if re.search(r"\bcomprobante\w*\b", normalized) and not re.search(MISSING_RECEIPT, normalized):
        match = re.search(r"gasto\s+#?(\d+)|comprobante\s+(?:del\s+)?(?:gasto\s+)?#?(\d+)", normalized)
        if match:
            expense_id = int(next(group for group in match.groups() if group))
            return Plan("generate_receipt", {"expense_id": expense_id},
                        "rules", "receipt", "report.receipt")
        return Plan("generate_receipt", {"latest": True} if _mentions_latest(normalized) else {},
                    "rules", "receipt", "report.receipt.pick")
    if re.search(r"\b(grafic|grafiq|chart|diagrama)\w*\b", normalized):
        return Plan("generate_chart",
                    with_period({"source": _chart_source(normalized),
                                 "chart_type": _chart_type(normalized)}),
                    "rules", "chart", "report.chart")
    if re.search(r"\b(pdf|informe|reporte ejecutivo|reporte financiero)\b", normalized) and \
       not re.search(r"\bguardad|listad\b", normalized):
        return Plan("generate_financial_report", with_period({}), "rules",
                    "financial_report", "report.financial")
    if re.search(r"\breportes? (guardad|generad|disponibl)\w*|\bque reportes\b|\blista\w* reportes\b",
                 normalized):
        return Plan("list_saved_reports", {}, "rules", "list_reports", "report.list")

    # --- Schema ----------------------------------------------------------
    if re.search(r"\b(elimina|borra|quita|remueve)\w*\b.*\bcolumnas?\b", normalized):
        entity = "expenses" if "gasto" in normalized else ("orders" if "orden" in normalized else None)
        # Everything after "columna" is the column name.
        name = re.search(r"columnas?\s+(?:llamada\s+|de\s+nombre\s+)?[\"'«]?([^\"'»,.]+)", normalized)
        label = name.group(1).strip() if name else ""
        # Drop a trailing entity mention: "columna IVA de gastos" -> "IVA".
        label = re.sub(r"\s+(de|en|del|de la)\s+(gastos?|ordenes?)\s*$", "", label).strip()
        if label:
            arguments = {"column": label}
            if entity:
                arguments["entity"] = entity
            return Plan("remove_column", arguments, "rules", "remove_column", "schema.remove")
        return None
    if re.search(r"\b(agrega|añade|anade|crea|crear)\b.*\bcolumna\b", normalized):
        return None  # needs a label and possibly a formula: let the planner parse it
    if re.search(r"\b(que|cuales|lista\w*)\b.*\bcolumnas?\b", normalized):
        entity = "expenses" if "gasto" in normalized else ("orders" if "orden" in normalized else None)
        return Plan("list_columns", {"entity": entity} if entity else {},
                    "rules", "list_columns", "schema.list")

    # --- External endpoint -----------------------------------------------
    if re.search(r"\bsincroniz\w*|\btrae\w*\b.*\bendpoint\b", normalized):
        kind = "expenses" if "gasto" in normalized else "orders"
        return Plan("sync_external_data", {"kind": kind}, "rules", "sync_external", "external.sync")
    if re.search(r"\bendpoint\b|\bintegracion\b", normalized):
        return Plan("check_external_endpoint", {}, "rules", "check_external", "external.check")

    # --- Analytics -------------------------------------------------------
    if re.search(r"\b(alerta|anomal|riesgo|problema|raro)\w*\b", normalized):
        return Plan("detect_anomalies", with_period({}), "rules", "anomalies", "analytics.anomalies")
    if re.search(r"\b(dashboard|tablero|kpis?|indicadores|analisis)\b", normalized):
        return Plan("get_kpi_dashboard", with_period({}), "rules", "dashboard", "analytics.dashboard")

    # --- Records: create, update, delete ----------------------------------
    # "registra un gasto de 200 USD", "cambia el monto del gasto 12 a 250 USD",
    # "elimina la orden ORD-4123". The tool is decided here and its arguments
    # are parsed by agent/parsing.py, so no amount ever depends on the planner;
    # what is missing is asked for one field at a time (agent/drafts.py).
    tool = record_tool(normalized)
    if tool:
        return Plan(tool, {}, "rules", tool, f"records.{tool}")

    # --- Records ---------------------------------------------------------
    if re.search(r"\b(muestra|muestrame|lista\w*|dame|ver|cuales son)\b.*\bordenes?\b", normalized) or \
       re.search(r"\bordenes\b.*\b(rechazad|aprobad|pendient|reembolsad)\w*", normalized):
        arguments = with_period({"limit": _limit(question)})
        status = _status(normalized)
        gateway = _gateway(normalized)
        if status:
            arguments["status"] = status
        if gateway:
            arguments["gateway"] = gateway
        return Plan("list_orders", arguments, "rules", "list_orders", "records.orders")
    if re.search(MISSING_RECEIPT, normalized):
        return Plan("list_expenses", with_period({"missing_receipt": True, "limit": 25}),
                    "rules", "list_expenses", "records.expenses.missing")
    if re.search(r"\b(muestra|muestrame|lista\w*|dame|ver)\b.*\bgastos?\b", normalized):
        arguments = with_period({"limit": _limit(question)})
        category = re.search(r"gastos? de ([a-z]+)", normalized)
        if category and category.group(1) not in ("este", "ese", "julio", "mes", "la", "el"):
            arguments["category"] = category.group(1)
        return Plan("list_expenses", arguments, "rules", "list_expenses", "records.expenses")

    # The broadest rules go last, and only for questions: a command that reaches
    # this point (e.g. "registra un gasto de 90 USD") must fall through to the
    # planner, which fills in the tool arguments, instead of being read as a query.
    if is_command:
        return None
    if re.search(r"\b(resumen|cerro|cierre|como va|como vamos|ingresos|facturamos|margen|mes)\b",
                 normalized):
        return Plan("get_period_summary", with_period({}), "rules", "summary", "analytics.summary")
    if re.search(r"\bgastos?\b", normalized):
        return Plan("list_expenses", with_period({"limit": 10}), "rules", "list_expenses",
                    "records.expenses.generic")

    return None


def view_label(context: dict | None) -> str:
    """Human description of the current screen, for the LLM prompts."""
    view = (context or {}).get("view")
    if view in VIEW_TOOLS:
        return VIEW_TOOLS[view][2]
    return "el chat del agente"
