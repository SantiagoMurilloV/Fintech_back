"""Agent chat, conversation history and chat attachments."""
from __future__ import annotations

import base64
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import run as run_agent
from ..database import get_db
from ..models import Attachment, Conversation, Message
from ..services import documents, storage
from .auth import AuthDep

router = APIRouter(tags=["chat"], dependencies=[AuthDep])

TITLE_MAX_LEN = 60
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# Sheets are kept inline (base64) so the agent can import them later.
INLINE_KEEP_BYTES = 2 * 1024 * 1024


class ChatBody(BaseModel):
    conversation_id: int | None = None
    content: str
    attachment_ids: list[int] = []
    # Where the user is in the app, e.g. {"view": "orders"}. Lets questions
    # like "¿y esto?" resolve against the screen being viewed.
    context: dict | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@router.get("/api/conversations")
def list_conversations(db: Session = Depends(get_db)):
    items = db.scalars(
        select(Conversation).order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(20)
    ).all()
    return {"items": [{"id": c.id, "title": c.title, "created_at": c.created_at} for c in items]}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conversation = db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversación no encontrada.")
    return {
        "id": conversation.id, "title": conversation.title, "created_at": conversation.created_at,
        "messages": [{"role": m.role, "content": m.content, "blocks": m.blocks or [],
                      "created_at": m.created_at} for m in conversation.messages],
    }


class DeleteConversationsBody(BaseModel):
    """Empty `ids` means "delete every conversation"."""
    ids: list[int] = []


@router.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conversation = db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversación no encontrada.")
    db.delete(conversation)  # messages cascade
    db.commit()
    return {"deleted": [conversation_id]}


@router.post("/api/conversations/delete")
def delete_conversations(body: DeleteConversationsBody, db: Session = Depends(get_db)):
    """Bulk delete: the selected ids, or all of them when the list is empty.

    Uses POST because several HTTP clients drop bodies on DELETE requests.
    """
    query = select(Conversation)
    if body.ids:
        query = query.where(Conversation.id.in_(body.ids))
    conversations = db.scalars(query).all()
    deleted = [conversation.id for conversation in conversations]
    for conversation in conversations:
        db.delete(conversation)
    db.commit()
    return {"deleted": deleted, "count": len(deleted)}


@router.post("/api/chat")
def chat(body: ChatBody, db: Session = Depends(get_db)):
    question = body.content.strip()
    if not question and not body.attachment_ids:
        raise HTTPException(status_code=400, detail="El mensaje está vacío.")
    if not question:
        question = "Lee el archivo adjunto."

    history: list[dict] = []
    # A stale id (e.g. the conversation was deleted in another tab) must not
    # block the message: fall through and open a new conversation instead.
    conversation = db.get(Conversation, body.conversation_id) if body.conversation_id else None
    if conversation:
        history = [{"role": m.role, "content": m.content} for m in conversation.messages]
    else:
        # First message becomes the conversation title (truncated).
        title = question if len(question) <= TITLE_MAX_LEN else f"{question[:TITLE_MAX_LEN - 3]}…"
        conversation = Conversation(title=title, created_at=_now())
        db.add(conversation)
        db.flush()

    db.add(Message(conversation_id=conversation.id, role="user", content=question,
                   blocks=[], created_at=_now()))
    db.commit()

    answer = run_agent(db, question, history=history, conversation_id=conversation.id,
                       attachment_ids=body.attachment_ids or None, context=body.context)

    db.add(Message(conversation_id=conversation.id, role="agent", content=answer.text,
                   blocks=answer.blocks, created_at=_now()))
    db.commit()

    # True when a tool wrote to the database, so the UI can refresh its views
    # immediately instead of waiting for a reload.
    mutated = any(step.get("node") == "execute" and step.get("mutates") and step.get("ok")
                  for step in answer.trace)

    return {
        "conversation_id": conversation.id,
        "reply": answer.text,
        "blocks": answer.blocks,
        "mutated": mutated,
        # Exposed so the UI (and audits) can see how the answer was produced.
        "intent": answer.intent,
        "planner": answer.planner,
        "trace": answer.trace,
    }


@router.post("/api/chat/attachments", status_code=201)
async def upload_attachment(file: UploadFile, db: Session = Depends(get_db)):
    """Store a chat attachment and extract its content deterministically."""
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera 15 MB.")

    filename = file.filename or "archivo"
    extracted = documents.extract(content, filename, file.content_type or "")

    try:
        stored = storage.save(content, filename, subfolder="attachments")
        url = stored["url"]
    except Exception:  # noqa: BLE001 — storage is best effort; extraction still works
        url = None

    meta = dict(extracted["meta"])
    # Spreadsheets keep their bytes so the import tool can replay them.
    if extracted["kind"] == "sheet" and len(content) <= INLINE_KEEP_BYTES:
        meta["content_b64"] = base64.b64encode(content).decode("ascii")

    attachment = Attachment(
        filename=filename, mime_type=file.content_type or "application/octet-stream",
        size_bytes=len(content), url=url, kind=extracted["kind"],
        extracted_text=extracted["text"], meta=meta, created_at=_now(),
    )
    db.add(attachment)
    db.commit()

    return {
        "id": attachment.id, "filename": attachment.filename, "kind": attachment.kind,
        "size_bytes": attachment.size_bytes, "url": attachment.url,
        "summary": extracted["summary"],
    }
