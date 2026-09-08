"""Serving for locally stored generated files (used when Cloudinary is off)."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services import storage
from .auth import AuthDep

router = APIRouter(tags=["files"], dependencies=[AuthDep])

ALLOWED_FOLDERS = {"reports", "invoices", "receipts", "attachments"}

# Quotes and control characters would break the header; names are ours anyway.
_UNSAFE_IN_HEADER = re.compile(r'[^\w.\- ]')


@router.get("/api/files/{subfolder}/{filename}")
def get_file(subfolder: str, filename: str, download: bool = False):
    """Serve a generated file, shown in place unless a download is asked for.

    Invoices and receipts carry customer names and amounts, so they need a
    session like any other data. An <iframe> cannot send the Authorization
    header, so the viewer passes the same token as `?token=` (see auth).
    """
    if subfolder not in ALLOWED_FOLDERS:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    path = storage.local_path(subfolder, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    # `inline` is what makes the browser render the PDF instead of offering to
    # save it: passing `filename=` to FileResponse would force an attachment.
    disposition = "attachment" if download else "inline"
    safe_name = _UNSAFE_IN_HEADER.sub("_", path.name)
    return FileResponse(path, headers={
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
    })
