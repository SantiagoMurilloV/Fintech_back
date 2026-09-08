"""Deterministic extraction of content from uploaded files.

Supported kinds:
  - pdf   : text per page via pypdf
  - sheet : xlsx/csv parsed with pandas (headers, row count, preview)
  - image : dimensions and format via Pillow (no OCR dependency)
  - text  : decoded as UTF-8

Nothing here calls an LLM: the same file always yields the same summary.
"""
from __future__ import annotations

import io

import pandas as pd

MAX_TEXT_CHARS = 20000
PREVIEW_ROWS = 5

PDF_MIMES = {"application/pdf"}
SHEET_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
}
TEXT_MIMES = {"text/plain", "text/markdown", "application/json"}


def classify(filename: str, mime_type: str) -> str:
    name = filename.lower()
    if mime_type in PDF_MIMES or name.endswith(".pdf"):
        return "pdf"
    if mime_type in SHEET_MIMES or name.endswith((".xlsx", ".xls", ".csv")):
        return "sheet"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type in TEXT_MIMES or name.endswith((".txt", ".md", ".json")):
        return "text"
    return "other"


def extract(content: bytes, filename: str, mime_type: str) -> dict:
    """Return {kind, text, meta, summary} for an uploaded file."""
    kind = classify(filename, mime_type)
    if kind == "pdf":
        return _extract_pdf(content, filename)
    if kind == "sheet":
        return _extract_sheet(content, filename)
    if kind == "image":
        return _extract_image(content, filename)
    if kind == "text":
        text = content.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
        lines = text.count("\n") + 1
        return {"kind": "text", "text": text, "meta": {"lines": lines},
                "summary": f"Archivo de texto «{filename}» con {lines} líneas."}
    return {"kind": "other", "text": None, "meta": {},
            "summary": f"Archivo «{filename}» adjuntado (tipo no interpretable)."}


def _extract_pdf(content: bytes, filename: str) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as err:  # noqa: BLE001 — malformed PDFs are user input
        return {"kind": "pdf", "text": None, "meta": {"error": str(err)},
                "summary": f"No pude leer el PDF «{filename}»."}

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")

    text = "\n\n".join(pages)[:MAX_TEXT_CHARS]
    words = len(text.split())
    info = reader.metadata or {}
    meta = {
        "pages": len(reader.pages),
        "words": words,
        "title": str(info.get("/Title", "") or ""),
        "has_text_layer": bool(text.strip()),
    }
    if not text.strip():
        summary = (f"PDF «{filename}» con {len(reader.pages)} páginas, pero sin capa de texto "
                   "(probablemente escaneado): puedo archivarlo como comprobante, no leer su contenido.")
    else:
        summary = f"PDF «{filename}»: {len(reader.pages)} páginas y {words} palabras extraídas."
    return {"kind": "pdf", "text": text, "meta": meta, "summary": summary}


def _extract_sheet(content: bytes, filename: str) -> dict:
    try:
        if filename.lower().endswith(".csv"):
            frame = pd.read_csv(io.BytesIO(content))
        else:
            frame = pd.read_excel(io.BytesIO(content))
    except Exception as err:  # noqa: BLE001
        return {"kind": "sheet", "text": None, "meta": {"error": str(err)},
                "summary": f"No pude leer la hoja de cálculo «{filename}»."}

    columns = [str(c) for c in frame.columns]
    preview = frame.head(PREVIEW_ROWS).astype(object).where(pd.notna(frame.head(PREVIEW_ROWS)), None)
    meta = {
        "rows": int(len(frame)),
        "columns": columns,
        "preview": preview.values.tolist(),
    }
    summary = (f"Hoja «{filename}»: {len(frame)} filas y {len(columns)} columnas "
               f"({', '.join(columns[:8])}{'…' if len(columns) > 8 else ''}).")
    return {"kind": "sheet", "text": None, "meta": meta, "summary": summary}


def _extract_image(content: bytes, filename: str) -> dict:
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(content))
        meta = {"width": image.width, "height": image.height, "format": image.format}
        summary = (f"Imagen «{filename}» ({image.width}×{image.height}, {image.format}). "
                   "Puedo archivarla como comprobante de un gasto.")
    except Exception as err:  # noqa: BLE001
        meta, summary = {"error": str(err)}, f"No pude interpretar la imagen «{filename}»."
    return {"kind": "image", "text": None, "meta": meta, "summary": summary}
