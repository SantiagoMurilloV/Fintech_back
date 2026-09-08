"""User management. Admin only.

An admin creates the accounts, hands over the one-time password the endpoint
returns, and the person is forced to change it on first sign-in. There is no
public sign-up.

Two rules protect the panel from being locked out or hijacked: the last active
admin cannot be removed or demoted, and no one can disable or demote
themselves by accident.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ADMIN_ROLE, ROLES, RefreshToken, User
from ..services import security
from .auth import AdminDep, _revoke_all, user_view

router = APIRouter(tags=["users"], dependencies=[AdminDep])


class NewUserBody(BaseModel):
    email: str
    name: str | None = None
    role: str = "miembro"
    # Optional: without it the endpoint generates one and returns it once.
    password: str | None = None


class UpdateUserBody(BaseModel):
    """Partial edit: only the fields present are written."""
    email: str | None = None
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None


def _validated_email(db: Session, raw: str, current_id: int | None = None) -> str:
    """Normalise an address and make sure no one else already has it."""
    email = (raw or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=400, detail="El correo no es válido.")

    query = select(User).where(User.email == email)
    if current_id is not None:
        query = query.where(User.id != current_id)
    if db.scalar(query):
        raise HTTPException(status_code=409, detail="Ya existe una cuenta con ese correo.")
    return email


def _active_admins(db: Session, excluding: int | None = None) -> int:
    query = select(func.count(User.id)).where(User.role == ADMIN_ROLE, User.is_active.is_(True))
    if excluding is not None:
        query = query.where(User.id != excluding)
    return db.scalar(query) or 0


def _open_sessions(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count(RefreshToken.id)).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))) or 0


@router.get("/api/users")
def list_users(db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.created_at.desc(), User.id.desc())).all()
    return {
        "items": [{**user_view(user), "open_sessions": _open_sessions(db, user.id)}
                  for user in users],
        "roles": ROLES,
    }


@router.post("/api/users", status_code=201)
def create_user(body: NewUserBody, db: Session = Depends(get_db)):
    email = _validated_email(db, body.email)
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"El rol «{body.role}» no existe.")

    # Generated passwords are shown once, here, and never stored in the clear.
    generated = body.password is None
    password = body.password or security.random_password()
    try:
        password = security.validate_password(password)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None

    user = User(
        email=email, name=(body.name or "").strip() or None, role=body.role,
        password_hash=security.hash_password(password), is_active=True,
        must_change_password=True, created_at=security.stamp(),
    )
    db.add(user)
    db.commit()

    return {"item": user_view(user),
            "temporary_password": password if generated else None}


@router.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UpdateUserBody,
                admin: User = AdminDep, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    if body.role is not None and body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"El rol «{body.role}» no existe.")

    losing_admin = (
        (body.role is not None and body.role != ADMIN_ROLE and user.role == ADMIN_ROLE)
        or (body.is_active is False and user.role == ADMIN_ROLE)
    )
    if losing_admin and _active_admins(db, excluding=user.id) == 0:
        raise HTTPException(
            status_code=400,
            detail="Es el único administrador activo: nombre a otro antes de cambiarlo.")
    if user.id == admin.id and body.is_active is False:
        raise HTTPException(status_code=400, detail="No puede desactivar su propia cuenta.")

    if body.email is not None:
        user.email = _validated_email(db, body.email, current_id=user.id)
    if body.name is not None:
        user.name = body.name.strip() or None
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.must_change_password is not None:
        user.must_change_password = body.must_change_password
    db.commit()

    # A disabled account must stop working now, not when its token expires.
    if body.is_active is False:
        _revoke_all(db, user.id)
    return {"item": user_view(user)}


@router.post("/api/users/{user_id}/password")
def reset_password(user_id: int, db: Session = Depends(get_db)):
    """Issue a new one-time password and close every session of that user."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    password = security.random_password()
    user.password_hash = security.hash_password(password)
    user.must_change_password = True
    db.commit()
    closed = _revoke_all(db, user.id)

    return {"item": user_view(user), "temporary_password": password, "closed_sessions": closed}


@router.post("/api/users/{user_id}/sessions/revoke")
def revoke_sessions(user_id: int, db: Session = Depends(get_db)):
    """Sign a user out everywhere without touching their password."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    return {"closed_sessions": _revoke_all(db, user.id)}


@router.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: User = AdminDep, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="No puede eliminar su propia cuenta.")
    if user.role == ADMIN_ROLE and _active_admins(db, excluding=user.id) == 0:
        raise HTTPException(status_code=400, detail="Es el único administrador activo.")

    removed = user_view(user)
    db.delete(user)          # sessions cascade
    db.commit()
    return {"deleted": removed}
