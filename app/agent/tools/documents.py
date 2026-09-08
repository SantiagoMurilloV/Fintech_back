"""Document tools: read uploaded files, import them and attach them to expenses."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Attachment, Expense
from ...services import excel_import
from .. import blocks
from ..registry import ToolResult, tool

PREVIEW_CHARS = 1200


def _latest(db: Session) -> Attachment | None:
    return db.scalars(select(Attachment).order_by(Attachment.id.desc()).limit(1)).first()


def _resolve(db: Session, attachment_id: int | None) -> Attachment:
    attachment = db.get(Attachment, int(attachment_id)) if attachment_id else _latest(db)
    if attachment is None:
        raise ValueError("No hay ningún archivo adjunto para leer.")
    return attachment


@tool(
    name="read_attachment",
    description="Lee un archivo adjuntado en el chat (PDF, imagen, Excel/CSV o texto) y muestra su "
                "contenido extraído. Si no se indica id, usa el último adjunto.",
    parameters={
        "type": "object",
        "properties": {"attachment_id": {"type": "integer"}},
        "required": [],
    },
    examples=["¿Qué dice el PDF que subí?", "Lee el documento adjunto"],
)
def read_attachment(db: Session, attachment_id: int | None = None) -> ToolResult:
    attachment = _resolve(db, attachment_id)
    meta = attachment.meta or {}

    detail_rows = [
        {"field": "Archivo", "value": attachment.filename},
        {"field": "Tipo", "value": attachment.kind},
        {"field": "Tamaño", "value": f"{attachment.size_bytes / 1024:.0f} KB"},
    ]
    for key, label in (("pages", "Páginas"), ("words", "Palabras"),
                       ("rows", "Filas"), ("width", "Ancho"), ("height", "Alto")):
        if meta.get(key) is not None:
            detail_rows.append({"field": label, "value": str(meta[key])})

    result_blocks = [
        blocks.text(f"Contenido de «{attachment.filename}»:"),
        blocks.table(
            columns=[{"key": "field", "label": "Campo"}, {"key": "value", "label": "Valor"}],
            rows=detail_rows,
        ),
    ]

    if attachment.kind == "sheet" and meta.get("columns"):
        preview_rows = [
            {str(column): ("" if value is None else str(value))
             for column, value in zip(meta["columns"], row)}
            for row in meta.get("preview", [])
        ]
        result_blocks.append(blocks.table(
            columns=[{"key": str(c), "label": str(c)} for c in meta["columns"]],
            rows=preview_rows,
            caption=f"Vista previa · {meta.get('rows', 0)} filas en total",
        ))
        result_blocks.append(blocks.notice(
            "Puedo importar estas filas a órdenes o gastos si me lo pide.", tone="info"))
    elif attachment.extracted_text:
        excerpt = attachment.extracted_text[:PREVIEW_CHARS]
        suffix = "…" if len(attachment.extracted_text) > PREVIEW_CHARS else ""
        result_blocks.append(blocks.text(excerpt + suffix))
    elif attachment.kind == "image":
        result_blocks.append(blocks.file(attachment.filename, attachment.url or "", "image",
                                         "Imagen adjunta"))
        result_blocks.append(blocks.notice(
            "No extraigo texto de imágenes; puedo archivarla como comprobante de un gasto.",
            tone="info"))
    else:
        result_blocks.append(blocks.notice(
            "El archivo no tiene texto legible.", tone="warning"))

    return ToolResult(blocks=result_blocks,
                      data={"attachment_id": attachment.id, "kind": attachment.kind},
                      summary=f"Adjunto {attachment.filename} leído.")


@tool(
    name="import_attachment",
    description="Importa a la base de datos un Excel/CSV adjuntado, como órdenes o como gastos. "
                "Valida fila por fila y reporta las que no pudo cargar.",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["orders", "expenses"]},
            "attachment_id": {"type": "integer"},
        },
        "required": ["kind"],
    },
    mutates=True,
    examples=["Importa el Excel que subí como gastos"],
)
def import_attachment(db: Session, kind: str, attachment_id: int | None = None) -> ToolResult:
    attachment = _resolve(db, attachment_id)
    if attachment.kind != "sheet":
        raise ValueError("Solo puedo importar archivos .xlsx, .xls o .csv.")
    raw = (attachment.meta or {}).get("content_b64")
    if not raw:
        raise ValueError("El contenido del archivo ya no está disponible; vuelva a subirlo.")

    import base64
    content = base64.b64decode(raw)
    result = excel_import.import_file(db, kind, content, attachment.filename)

    result_blocks = [blocks.notice(result["message"],
                                   tone="success" if result["inserted"] else "warning")]
    if result["errors"]:
        result_blocks.append(blocks.listing(
            [{"title": error, "tone": "warning"} for error in result["errors"]],
            title="Filas omitidas"))
    return ToolResult(blocks=result_blocks, data=result, summary=result["message"])


@tool(
    name="attach_receipt_to_expense",
    description="Vincula un archivo adjunto (imagen o PDF) como comprobante de un gasto existente.",
    parameters={
        "type": "object",
        "properties": {
            "expense_id": {"type": "integer"},
            "attachment_id": {"type": "integer"},
        },
        "required": ["expense_id"],
    },
    mutates=True,
    examples=["Adjunta esta factura al gasto 12"],
)
def attach_receipt_to_expense(db: Session, expense_id: int,
                              attachment_id: int | None = None) -> ToolResult:
    attachment = _resolve(db, attachment_id)
    expense = db.get(Expense, int(expense_id))
    if expense is None:
        raise ValueError(f"No encontré el gasto {expense_id}.")
    if not attachment.url:
        raise ValueError("El archivo no quedó almacenado; vuelva a subirlo.")

    expense.receipt_name = attachment.filename
    expense.receipt_url = attachment.url
    db.commit()

    return ToolResult(
        blocks=[
            blocks.notice(f"«{attachment.filename}» quedó como comprobante del gasto "
                          f"#{expense.id} ({expense.description}).", tone="success"),
            blocks.file(attachment.filename, attachment.url, attachment.kind, "Comprobante vinculado"),
        ],
        data={"expense_id": expense.id, "attachment_id": attachment.id},
        summary=f"Comprobante vinculado al gasto {expense.id}.",
    )
