"""Deterministic parsing of record data written in free text.

The planner may pick a tool and suggest text fields, but every amount, currency
and date that reaches the database is extracted here, by rule: «500.000 COP»
always becomes 500000.0 through this same code, so a creation can be replayed
from the original message. Money never depends on the model.

`normalize` is length-preserving on purpose: the offsets of a match in the
normalized text are valid in the original one, which lets `literal` cut the
fragments already understood (amounts, dates, ids) and keep the rest verbatim,
accents and capitalisation included.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

STATUS_WORDS = {
    "aprobad": "approved", "approved": "approved",
    "pendient": "pending", "pending": "pending",
    "rechazad": "rejected", "rejected": "rejected", "rechazo": "rejected",
    "reembolsad": "refunded", "refunded": "refunded", "devuelt": "refunded",
}

GATEWAY_WORDS = {"wompi": "Wompi", "epayco": "ePayco", "bold": "Bold", "stripe": "Stripe (USD)"}

# Currency markers, least ambiguous first: a bare "pesos" means COP.
CURRENCY_WORDS = [
    (r"\b(?:usdt|tether)\b", "USDT"),
    (r"\b(?:usdc|usd\s+coin)\b", "USDC"),
    (r"\b(?:usd|us\$|dolar(?:es)?|dls)\b", "USD"),
    (r"\b(?:mxn|mx\$|pesos?\s+mexicanos?)\b", "MXN"),
    (r"\b(?:cop|pesos?\s+colombianos?|pesos?)\b", "COP"),
]

MULTIPLIERS = [(r"^(?:millon(?:es)?|mm|palos?)$", 1_000_000), (r"^(?:mil(?:es)?|k)$", 1_000)]

# Verbs and nouns that turn a message into a record command.
CREATE_VERBS = r"\b(?:crea|registra|agrega|anade|ingresa|anota|carga|mete|guarda|apunta|nuev)\w*"
EDIT_VERBS = (r"\b(?:edita|modifica|cambia|actualiza|corrige|ajusta|renombra|reclasifica|"
              r"pon|ponle|cambiale|correg|arregla|sube|baja|marca|deja|clasifica|asigna|"
              r"fija|establece|setea|reemplaza|sustituye|pasa)\w*")
DELETE_VERBS = r"\b(?:elimina|borra|quita|remueve|descarta|suprime|anula)\w*"
EXPENSE_NOUNS = r"\b(?:gasto|egreso|pago|compra)\w*"
ORDER_NOUNS = r"\b(?:orden|operacion|venta|transaccion|pedido|cobro|ingreso)\w*"
# Things that are created or removed but are not records: they have own tools.
NON_RECORD = r"\b(?:columna|campo|reporte|grafic|dashboard|conversacion|usuario|cuenta)\w*"

# How the user names each editable field, per entity.
FIELD_WORDS = {
    "expenses": {
        "description": r"concepto|descripci[oó]n|detalle|nombre",
        "category": r"categor[ií]a|rubro",
        "vendor": r"proveedor",
        "owner": r"responsable|encargad[oa]|due[ñn][oa]",
        "amount": r"monto|valor|importe|precio|cifra",
        "currency": r"moneda|divisa",
        "date": r"fecha|d[ií]a",
    },
    "orders": {
        "customer": r"cliente|comprador",
        "amount": r"monto|valor|importe|precio|cifra",
        "currency": r"moneda|divisa",
        "status": r"estado|status",
        "gateway": r"pasarela|gateway|medio\s+de\s+pago",
        "date": r"fecha|d[ií]a",
    },
}

# Values that carry their own meaning, so they are read by type, not by name.
TYPED_FIELDS = ("amount", "currency", "date", "status", "gateway")

# Which stored values a field should reuse instead of creating a variant.
KNOWN_OF = {"category": "categories", "vendor": "vendors",
            "owner": "owners", "customer": "customers"}

_ACCENTS = str.maketrans(
    "áàäâãéèëêíìïîóòöôõúùüûñçÁÀÄÂÃÉÈËÊÍÌÏÎÓÒÖÔÕÚÙÜÛÑÇ",
    "aaaaaeeeeiiiiooooouuuuncAAAAAEEEEIIIIOOOOOUUUUNC",
)


def normalize(text: str) -> str:
    """Lowercase and drop accents, one output character per input character."""
    return text.lower().translate(_ACCENTS)


# --------------------------------------------------------------------- amounts

# Grouped thousands first, so "500.000" is read whole and not as "500".
_NUMBER = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?"

# The leading boundary keeps codes such as "Q3" or "S3" from reading as money.
_AMOUNT_RE = re.compile(
    rf"(?<![\w])(?P<symbol>us\$|mx\$|\$)?\s*(?P<number>{_NUMBER})\s*"
    rf"(?P<mult>millones|millon|palos|palo|mm|miles|mil|k)?\s*"
    rf"(?P<currency>usdt|usdc|tether|usd|cop|mxn|dolares|dolar|pesos|peso)?"
)

# Digits that are never an amount: dates, periods, record ids, percentages.
_NOISE_PATTERNS = [
    r"\bord-?\s*\d+\b",
    r"\bext-\w+\b",
    r"\b20\d{2}-\d{2}-\d{2}\b",
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    r"\b20\d{2}-\d{2}\b",
    r"\b\d+(?:[.,]\d+)?\s*%",
    r"#\s*\d+",
    rf"\b\d{{1,2}}\s+de\s+(?:{'|'.join(MONTHS)})\b(?:\s+de(?:l)?\s+20\d{{2}})?",
    r"\b(?:de|del|en|ano)\s+20\d{2}\b",
]


def _noise_spans(normalized: str) -> list[tuple[int, int]]:
    return [match.span() for pattern in _NOISE_PATTERNS
            for match in re.finditer(pattern, normalized)]


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace spans with blanks, keeping every other offset in place."""
    chars = list(text)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    return "".join(chars)


def _to_number(raw: str) -> float:
    """Read a number the way it is written in Colombia, then the US way.

    With both separators the last one is the decimal mark. With a single
    separator followed by exactly three digits it is a thousands group
    («500.000» and «500,000» are both half a million); one or two digits make
    it a decimal («1,5» -> 1.5).
    """
    if "." in raw and "," in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        return float(raw.replace(thousands, "").replace(decimal, "."))
    for separator in (".", ","):
        if separator in raw:
            groups = raw.split(separator)
            if all(len(group) == 3 for group in groups[1:]):
                return float("".join(groups))
            return float(raw.replace(separator, "."))
    return float(raw)


def _multiplier(word: str | None) -> int:
    if not word:
        return 1
    for pattern, factor in MULTIPLIERS:
        if re.match(pattern, word):
            return factor
    return 1


@dataclass(frozen=True)
class Amount:
    value: float
    span: tuple[int, int]
    marked: bool        # written with a currency, a symbol or a multiplier


def _best_amount(text: str) -> Amount | None:
    """Pick the number that reads as money: the one carrying a currency mark."""
    normalized = normalize(text)
    masked = _blank(normalized, _noise_spans(normalized))

    best: tuple[int, Amount] | None = None
    for match in _AMOUNT_RE.finditer(masked):
        number = match.group("number")
        if not number:
            continue
        multiplier = _multiplier(match.group("mult"))
        value = round(_to_number(number) * multiplier, 2)
        if value <= 0:
            continue
        score = (2 if (match.group("currency") or match.group("symbol")) else 0)
        score += 1 if multiplier > 1 else 0
        if best is None or score > best[0]:
            best = (score, Amount(value, match.span(), score > 0))
    return best[1] if best else None


def parse_amount(text: str) -> float | None:
    found = _best_amount(text)
    return found.value if found else None


def parse_currency(text: str) -> str | None:
    normalized = normalize(text)
    for pattern, code in CURRENCY_WORDS:
        if re.search(pattern, normalized):
            return code
    return None


# ----------------------------------------------------------------------- dates

def parse_date(text: str, today: date | None = None) -> str | None:
    """Return an ISO date for the day named in the text, or None."""
    today = today or date.today()
    normalized = normalize(text)

    iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", normalized)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    if re.search(r"\b(?:anteayer|antier)\b", normalized):
        return (today - timedelta(days=2)).isoformat()
    if re.search(r"\bayer\b", normalized):
        return (today - timedelta(days=1)).isoformat()
    if re.search(r"\b(?:hoy|de hoy)\b", normalized):
        return today.isoformat()

    slashed = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", normalized)
    if slashed:
        year = int(slashed.group(3) or today.year)
        year = year + 2000 if year < 100 else year
        return _safe_date(year, int(slashed.group(2)), int(slashed.group(1)))

    spelled = re.search(rf"\b(\d{{1,2}})\s+de\s+({'|'.join(MONTHS)})\b(?:\s+de(?:l)?\s+(20\d{{2}}))?",
                        normalized)
    if spelled:
        year = int(spelled.group(3) or today.year)
        return _safe_date(year, MONTHS[spelled.group(2)], int(spelled.group(1)))
    return None


def _safe_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_period(text: str, today: date | None = None) -> str | None:
    """Find 'YYYY-MM', 'julio', 'julio 2026' or 'mes pasado' in the question."""
    today = today or date.today()
    explicit = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", text)
    if explicit:
        return explicit.group(0)

    normalized = normalize(text)
    for name, number in MONTHS.items():
        if re.search(rf"\b{name}\b", normalized):
            year_match = re.search(rf"{name}\s+(?:de\s+)?(20\d{{2}})", normalized)
            year = int(year_match.group(1)) if year_match else today.year
            return f"{year:04d}-{number:02d}"

    if re.search(r"\bmes pasado\b|\bmes anterior\b", normalized):
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return f"{year:04d}-{month:02d}"
    return None


# ---------------------------------------------------------------- text fields

# A captured value ends at punctuation or where the next field starts: status
# and gateway words included, so "para Grupo Andino aprobada por Wompi" keeps
# the customer name alone.
_STOP = (r"proveedor|categor[ií]a|rubro|responsable|encargad[oa]|concepto|descripci[oó]n|"
         r"monto|valor|precio|fecha|cliente|gateway|pasarela|estado|por|para|con|hoy|ayer|"
         r"el\s+d[ií]a|de(?:l)?\s+\d|aprobad\w*|pendiente\w*|rechazad\w*|reembolsad\w*|"
         r"devuelt\w*|wompi|epayco|bold|stripe|mediante|v[ií]a|usando|"
         rf"de(?:l)?\s+(?:{'|'.join(MONTHS)})")

# Filler that may prefix an answer: "es Figma", "el cliente Acme", "son 200 USD".
_FILLER = (r"^(?:s[ií]\s+|no\s+|es\s+|son\s+|era\s+|fue\s+|fueron\s+|seria\s+|ser[ií]a\s+|"
           r"va\s+a\s+ser\s+|sale\s+|vale\s+|cuesta\s+|"
           r"pon(?:le)?\s+|ponlo\s+en\s+|que\s+sea\s+|el\s+|la\s+|los\s+|las\s+|un\s+|una\s+|"
           r"de\s+|del\s+|a\s+|en\s+|para\s+|"
           r"cliente\s+|proveedor\s+|categor[ií]a\s+|concepto\s+|responsable\s+)+")

_EXPENSE_KEYWORDS = {
    "description": r"concepto|descripci[oó]n",
    "category": r"categor[ií]a|rubro",
    "vendor": r"proveedor|pagad[oa]\s+a|facturad[oa]\s+por",
    "owner": r"responsable|encargad[oa]|a\s+cargo\s+de",
}

_ORDER_KEYWORDS = {
    "customer": r"cliente|para|a\s+nombre\s+de",
}

MAX_VALUE_LEN = 120


def _clean(value: str | None) -> str | None:
    """Trim a captured value; reject what is only punctuation or a number."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" \t\n\"'«»:;,.-")
    # The trailing space lets an answer made only of filler ("son 200 USD",
    # once the amount is cut) collapse to nothing instead of leaving "son".
    cleaned = re.sub(_FILLER, "", f"{cleaned} ", flags=re.IGNORECASE)
    cleaned = cleaned.strip(" \t\n\"'«»:;,.-")
    cleaned = re.sub(r"\s+(?:y|e|o)$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or not re.search(r"[a-zA-Z]", cleaned):
        return None
    return cleaned[:MAX_VALUE_LEN]


def _capture(text: str, keywords: str) -> str | None:
    """Take the value written right after one of `keywords`."""
    match = re.search(rf"(?:{keywords})\s*[:=]?\s+(?P<value>[^,;\n]+)", text, re.IGNORECASE)
    if not match:
        return None
    value = match.group("value")
    cut = re.search(rf"\s+(?:{_STOP})\b", value, re.IGNORECASE)
    if cut:
        value = value[:cut.start()]
    return _clean(value)


def match_known(text: str, values) -> str | None:
    """Return the stored value named in the text, so answers reuse it verbatim.

    Longest match wins: "Google Cloud" beats "Google" when both exist.
    """
    normalized = normalize(text)
    best: str | None = None
    for value in values:
        token = normalize(str(value)).strip()
        if not token:
            continue
        if re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", normalized):
            if best is None or len(token) > len(best):
                best = str(value)
    return best


def literal(text: str) -> str | None:
    """The message as a plain value, with what is already understood removed.

    Used when the user answers the field the agent just asked for: amounts,
    dates and ids are cut out and the remaining words are the answer.
    """
    normalized = normalize(text)
    spans = _noise_spans(normalized)
    amount = _best_amount(text)
    # A bare number may be part of the answer ("Campaña Q3"); only a number
    # written as money is removed.
    if amount and amount.marked:
        spans.append(amount.span)
        for pattern, _ in CURRENCY_WORDS:
            spans += [match.span() for match in re.finditer(pattern, normalized)]
    return _clean(_blank(text, spans))


# ------------------------------------------------------------------- records

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


def _drop_empty(fields: dict) -> dict:
    return {key: value for key, value in fields.items() if value not in (None, "")}


def parse_expense(text: str, known: dict | None = None) -> dict:
    """Extract the fields of an expense named in free text."""
    known = known or {}
    fields = {key: _capture(text, keywords) for key, keywords in _EXPENSE_KEYWORDS.items()}

    # "un gasto de licencias de diseño por 200 USD" -> concept without a keyword.
    if not fields["description"]:
        fields["description"] = _capture(text, r"gastos?\s+(?:de|por|en)")
    if not fields["category"]:
        fields["category"] = match_known(text, known.get("categories", ()))
    if not fields["vendor"]:
        fields["vendor"] = match_known(text, known.get("vendors", ()))
    if not fields["owner"]:
        fields["owner"] = match_known(text, known.get("owners", ()))

    # A category name captured as the concept is a category, not a concept.
    if fields["description"] and fields["description"] == fields["category"]:
        fields["description"] = None

    fields["amount"] = parse_amount(text)
    fields["currency"] = parse_currency(text)
    fields["date"] = parse_date(text)
    return _drop_empty(fields)


def parse_order(text: str, known: dict | None = None) -> dict:
    """Extract the fields of an order (an "operación") named in free text."""
    known = known or {}
    normalized = normalize(text)
    # «cliente» primero: en «para el cliente Acme», capturar tras «para» deja
    # el valor «el cliente…», que el corte por palabras clave vacía.
    customer = (_capture(text, r"cliente") or _capture(text, r"para|a\s+nombre\s+de"))
    if not customer:
        customer = match_known(text, known.get("customers", ()))

    return _drop_empty({
        "customer": customer,
        "amount": parse_amount(text),
        "currency": parse_currency(text),
        "status": _status(normalized),
        "gateway": _gateway(normalized),
        "date": parse_date(text),
    })


# --------------------------------------------------------------- referencing

# ORD-#### nativas y EXT-... importadas (el guion de EXT es obligatorio para
# no confundirse con palabras como «extra»).
_ORDER_ID_RE = re.compile(r"\b(?:ord-?\s*(\d{1,6})|ext-(\w{1,14}))\b", re.IGNORECASE)
_EXPENSE_ID_RE = re.compile(
    r"\bgastos?\s*(?:#|numero|num|nro|id)?\s*(\d{1,6})\b|\b(?:#|id)\s*(\d{1,6})\b",
    re.IGNORECASE)

LATEST_RE = r"\b(?:ultim|reciente|mas nuev)\w*"


def record_id(text: str) -> tuple[str, str | int, tuple[int, int]] | None:
    """Find an explicit record id: ('orders', 'ORD-4123', span) or ('expenses', 12, span)."""
    normalized = normalize(text)
    order = _ORDER_ID_RE.search(normalized)
    if order:
        if order.group(1):
            return "orders", f"ORD-{order.group(1)}", order.span()
        return "orders", f"EXT-{order.group(2).upper()}", order.span()
    expense = _EXPENSE_ID_RE.search(normalized)
    if expense:
        digits = next(group for group in expense.groups() if group)
        return "expenses", int(digits), expense.span()
    return None


def mentions_latest(text: str) -> bool:
    return bool(re.search(LATEST_RE, normalize(text)))


# ------------------------------------------------------------------- actions

def record_intent(text: str) -> tuple[str, str] | None:
    """Return ('create'|'update'|'delete', 'expenses'|'orders') for a command.

    Only the intent is decided here; the arguments are parsed separately, so a
    command with no data still opens the conversation that collects it.
    """
    normalized = normalize(text)
    if re.search(NON_RECORD, normalized):
        return None

    identified = record_id(text)
    entity = None
    if re.search(EXPENSE_NOUNS, normalized):
        entity = "expenses"
    elif re.search(ORDER_NOUNS, normalized):
        entity = "orders"
    elif identified:
        entity = identified[0]
    if entity is None:
        return None

    if re.search(DELETE_VERBS, normalized):
        return "delete", entity
    if re.search(EDIT_VERBS, normalized):
        return "update", entity
    if re.search(CREATE_VERBS, normalized):
        # "agrégale el proveedor al gasto 12" adds to a record that exists.
        return ("update" if identified else "create"), entity
    return None


def record_tool(text: str) -> str | None:
    """The tool a record command maps to, e.g. 'update_expense'."""
    intent = record_intent(text)
    if intent is None:
        return None
    action, entity = intent
    return f"{action}_{'expense' if entity == 'expenses' else 'order'}"


def creation_intent(text: str) -> str | None:
    """The creation tool a command asks for, or None."""
    tool = record_tool(text)
    return tool if tool and tool.startswith("create_") else None


# ------------------------------------------------------------------- changes

# A new value ends at punctuation or where another field is named.
_ALL_FIELD_WORDS = "|".join(
    pattern for entity in FIELD_WORDS.values() for pattern in entity.values())

# Separators between a field and its new value: "monto a 250", "monto: 250".
_TO_VALUE = r"(?:\s*[:=]\s*|\s+(?:a|al|por|en|de|del|hacia|como|queda\s+en|sea)\s+|\s+)"


def _capture_change(text: str, words: str) -> str | None:
    match = re.search(rf"\b(?:{words}){_TO_VALUE}(?P<value>[^,;\n]+)", text, re.IGNORECASE)
    if not match:
        return None
    value = match.group("value")
    cut = re.search(rf"\s+(?:{_ALL_FIELD_WORDS}|{_STOP})\b", value, re.IGNORECASE)
    if cut:
        value = value[:cut.start()]
    return _clean(value)


def _new_status(text: str) -> str | None:
    """Prefer the status written as the new value over one describing the target."""
    normalized = normalize(text)
    explicit = re.search(
        r"\b(?:a|al|como|queda\s+en|dejal[ao]\s+(?:en|como)|marcal[ao]\s+como)\s+"
        r"(aprobad\w*|pendient\w*|rechazad\w*|reembolsad\w*|devuelt\w*)", normalized)
    return _status(explicit.group(1)) if explicit else _status(normalized)


def parse_changes(text: str, entity: str, known: dict | None = None) -> dict:
    """Extract the new values a message assigns to the fields of a record."""
    known = known or {}
    words = FIELD_WORDS[entity]
    changes: dict = {}

    for field, pattern in words.items():
        if field in TYPED_FIELDS:
            continue
        value = _capture_change(text, pattern)
        if value:
            changes[field] = value

    # Values recognised by type always win over what was captured by name.
    for field, stored in KNOWN_OF.items():
        if field in words:
            canonical = match_known(text, known.get(stored, ()))
            if canonical:
                changes[field] = canonical

    amount = parse_amount(text)
    if amount is not None:
        changes["amount"] = amount
    currency = parse_currency(text)
    if currency:
        changes["currency"] = currency
    day = parse_date(text)
    if day:
        changes["date"] = day
    if entity == "orders":
        status = _new_status(text)
        if status:
            changes["status"] = status
        gateway = _gateway(normalize(text))
        if gateway:
            changes["gateway"] = gateway
    return changes


# -------------------------------------------------------------- confirmation

AFFIRMATIVE = (r"\b(?:s[ií]|dale|confirmo|confirma|confirmado|hazlo|hagalo|adelante|ok|okey|"
               r"vale|correcto|exacto|procede|listo|eso|borral[ao]|eliminal[ao]|"
               r"actualizal[ao]|guardal[ao])\w*\b")
NEGATIVE = r"\b(?:no|nel|nop|mejor\s+no|cancela|cancelar|dejal[ao]|olvidal[ao]|para|detente)\w*\b"


def decision(text: str) -> str | None:
    """Read a yes/no answer; None when the message is neither."""
    normalized = normalize(text)
    # Negation is checked first: "no, mejor no" must never read as a yes.
    if re.search(NEGATIVE, normalized):
        return "no"
    if re.search(AFFIRMATIVE, normalized):
        return "yes"
    return None


# Shared with agent/targets.py, which resolves references over the same text.
blank = _blank
capture_after = _capture
