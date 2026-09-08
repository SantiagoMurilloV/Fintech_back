"""Authentication: sign in, refresh, sign out and the current account.

Accounts live in the database and are created by an admin — there is no
self-registration, so the panel is never open to whoever finds the URL. A
sign-in returns a short-lived JWT for the API plus a long-lived refresh token
that the frontend swaps for a new JWT when it expires.

Failures are deliberately uniform: an unknown email and a wrong password give
the same answer, so the endpoint cannot be used to find out who has an account.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import LOGIN_LOCKOUT_MINUTES, LOGIN_MAX_ATTEMPTS
from ..database import get_db
from ..models import ADMIN_ROLE, RefreshToken, User
from ..services import security

router = APIRouter(tags=["auth"])

INVALID_CREDENTIALS = "Correo o contraseña incorrectos."

# email -> [failures, locked until]. In memory on purpose: a restart clearing
# the counters is acceptable, a shared table on every login attempt is not.
_attempts: dict[str, list] = {}


class LoginBody(BaseModel):
    email: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


def user_view(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "is_active": user.is_active, "must_change_password": user.must_change_password,
        "created_at": user.created_at, "last_login_at": user.last_login_at,
    }


# ------------------------------------------------------------- brute force

def _locked_for(email: str) -> int:
    """Minutes left on the lockout, or 0."""
    record = _attempts.get(email)
    if not record or not record[1]:
        return 0
    remaining = (record[1] - security.now()).total_seconds()
    if remaining <= 0:
        _attempts.pop(email, None)
        return 0
    return max(1, int(remaining // 60) + 1)


def _register_failure(email: str) -> None:
    from datetime import timedelta

    record = _attempts.setdefault(email, [0, None])
    record[0] += 1
    if record[0] >= LOGIN_MAX_ATTEMPTS:
        record[1] = security.now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        record[0] = 0


# ------------------------------------------------------------ current user

def _token_from(request: Request) -> str:
    """Read the access token from the header, or from ?token= for direct links.

    An <iframe> or <img> cannot send an Authorization header, so the PDF viewer
    and receipt previews pass the same JWT as a query parameter.
    """
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.query_params.get("token", "").strip()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _token_from(request)
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado.")
    try:
        payload = security.decode_access_token(token)
    except security.AuthError as err:
        raise HTTPException(status_code=401, detail=str(err)) from None

    user = db.get(User, int(payload["sub"]))
    # Disabling an account takes effect immediately, without waiting for the
    # signature to expire.
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="La cuenta ya no está activa.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """The only role that carries authority: managing who gets in."""
    if user.role != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Necesita permisos de administrador.")
    return user


AuthDep = Depends(current_user)
AdminDep = Depends(require_admin)


# ----------------------------------------------------------------- endpoints

def _issue_session(db: Session, user: User, request: Request) -> dict:
    access_token, expires_in = security.create_access_token(user)
    raw, token_hash, expires_at = security.new_refresh_token()

    db.add(RefreshToken(
        user_id=user.id, token_hash=token_hash, expires_at=expires_at,
        created_at=security.stamp(),
        user_agent=(request.headers.get("user-agent") or "")[:200] or None,
    ))
    user.last_login_at = security.stamp()
    db.commit()

    return {
        "access_token": access_token,
        "refresh_token": raw,
        "expires_in": expires_in,
        "user": user_view(user),
    }


@router.post("/api/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    email = (body.email or "").strip().lower()
    if not email or not body.password:
        raise HTTPException(status_code=400, detail="Correo y contraseña son obligatorios.")

    locked = _locked_for(email)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Intente de nuevo en {locked} minutos.")

    user = db.scalar(select(User).where(User.email == email))
    # The password is always checked, even for an unknown email, so the reply
    # takes the same time either way.
    valid = security.verify_password(
        body.password, user.password_hash if user else security.DUMMY_HASH)

    if user is None or not valid:
        _register_failure(email)
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Su cuenta está desactivada.")

    _attempts.pop(email, None)
    return _issue_session(db, user, request)


@router.post("/api/refresh")
def refresh(body: RefreshBody, request: Request, db: Session = Depends(get_db)):
    """Swap a refresh token for a new pair. The old token is always burned."""
    stored = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_token(body.refresh_token)))
    if stored is None:
        raise HTTPException(status_code=401, detail="Sesión inválida.")

    if stored.revoked_at:
        # A token already rotated is being replayed: assume it was copied and
        # end every session of that account.
        _revoke_all(db, stored.user_id)
        raise HTTPException(status_code=401, detail="Sesión inválida. Vuelva a ingresar.")
    if security.is_expired(stored.expires_at):
        raise HTTPException(status_code=401, detail="La sesión expiró.")

    user = db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="La cuenta ya no está activa.")

    stored.revoked_at = security.stamp()
    return _issue_session(db, user, request)


@router.post("/api/logout")
def logout(body: RefreshBody | None = None, db: Session = Depends(get_db)):
    """Revoke the session being closed; nothing else is touched."""
    if body and body.refresh_token:
        stored = db.scalar(select(RefreshToken).where(
            RefreshToken.token_hash == security.hash_token(body.refresh_token)))
        if stored and not stored.revoked_at:
            stored.revoked_at = security.stamp()
            db.commit()
    return {"ok": True}


@router.get("/api/me")
def me(user: User = AuthDep):
    return user_view(user)


@router.post("/api/me/password")
def change_password(body: PasswordBody, request: Request,
                    user: User = AuthDep, db: Session = Depends(get_db)):
    """Change your own password.

    Every session is closed — including this one, since the old refresh token
    was issued under the old password — and a fresh pair is returned so the
    person changing it stays signed in where they are.
    """
    if not security.verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="La contraseña actual no coincide.")
    try:
        new_password = security.validate_password(body.new_password)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    if security.verify_password(new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="La contraseña nueva es igual a la actual.")

    user.password_hash = security.hash_password(new_password)
    user.must_change_password = False
    closed = _revoke_all(db, user.id)

    session = _issue_session(db, user, request)
    return {"ok": True, "closed_sessions": closed, **session}


def _revoke_all(db: Session, user_id: int) -> int:
    """Close every open session of a user. Returns how many were live."""
    sessions = db.scalars(select(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))).all()
    for session in sessions:
        session.revoked_at = security.stamp()
    db.commit()
    return len(sessions)
