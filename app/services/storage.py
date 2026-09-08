"""File storage: Cloudinary when configured, local disk as fallback.

Generated PDFs and chat attachments both go through here so the rest of the
code never has to care which backend is active.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import BASE_DIR, PUBLIC_BASE_URL
from . import media

STORAGE_DIR = BASE_DIR / "storage"


def save(content: bytes, filename: str, subfolder: str = "reports") -> dict:
    """Persist bytes and return {url, name, storage}."""
    if media.is_configured():
        uploaded = media.upload_receipt(content, filename, subfolder=subfolder)
        return {"url": uploaded["url"], "name": filename, "storage": "cloudinary"}

    folder = STORAGE_DIR / subfolder
    folder.mkdir(parents=True, exist_ok=True)
    # Hash-suffixed name keeps repeated generations from colliding.
    digest = hashlib.sha1(content).hexdigest()[:10]
    stem, _, ext = filename.rpartition(".")
    safe_name = f"{(stem or filename).replace('/', '_')}_{digest}.{ext or 'bin'}"
    (folder / safe_name).write_bytes(content)
    return {
        "url": f"{PUBLIC_BASE_URL}/api/files/{subfolder}/{safe_name}",
        "name": filename,
        "storage": "local",
    }


def local_path(subfolder: str, filename: str) -> Path | None:
    """Resolve a locally stored file, refusing paths outside the storage dir."""
    candidate = (STORAGE_DIR / subfolder / filename).resolve()
    root = STORAGE_DIR.resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate
