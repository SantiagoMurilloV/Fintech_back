"""Cloudinary integration: receipt and document uploads.

Requires CLOUDINARY_URL in .env (cloudinary://api_key:api_secret@cloud_name).
Without credentials the media endpoints return 503 with a clear message.
"""
from __future__ import annotations

from ..config import CLOUDINARY_FOLDER, CLOUDINARY_URL

_configured = False
if CLOUDINARY_URL:
    import cloudinary
    cloudinary.config(secure=True)  # reads CLOUDINARY_URL from the environment
    _configured = True


def is_configured() -> bool:
    return _configured


def upload_receipt(content: bytes, filename: str, subfolder: str = "receipts") -> dict:
    """Upload a file (pdf/image) and return {url, public_id, name}."""
    if not _configured:
        raise RuntimeError("Cloudinary is not configured (CLOUDINARY_URL missing in fintech_back/.env).")
    import cloudinary.uploader
    result = cloudinary.uploader.upload(
        content,
        folder=f"{CLOUDINARY_FOLDER}/{subfolder}",
        resource_type="auto",  # pdf, image, etc.
        use_filename=True,
        unique_filename=True,
        filename_override=filename,
    )
    return {"url": result["secure_url"], "public_id": result["public_id"], "name": filename}
