"""Passwords, access tokens and sessions.

Two different things, on purpose:

  access token   a signed JWT (HS256) the API verifies without touching the
                 database. Short-lived, so a stolen one stops working soon.
  refresh token  an opaque random string, stored only as a SHA-256 hash. It
                 can be revoked the instant an admin disables an account,
                 which a stateless JWT could not be.

Refreshing rotates the token: the old one dies with the new one issued. If a
rotated token is presented again — the signature of a stolen copy being
replayed — every session of that user is dropped.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from ..config import (
    ACCESS_TOKEN_MINUTES,
    JWT_ALGORITHM,
    JWT_ISSUER,
    JWT_SECRET,
    MIN_PASSWORD_LENGTH,
    REFRESH_TOKEN_DAYS,
)

# bcrypt truncates silently past 72 bytes; refuse instead of hashing a prefix.
MAX_PASSWORD_BYTES = 72


class AuthError(Exception):
    """Raised for anything the caller should see as 401."""


def now() -> datetime:
    """The current instant, in UTC. Used for signature and lockout math."""
    return datetime.now(timezone.utc)


def stamp(moment: datetime | None = None) -> str:
    """A timestamp for the database: local wall clock, 19 characters.

    Same shape as every other `created_at` in the schema, so auth rows read
    like the rest of the tables instead of being five hours off.
    """
    return (moment or datetime.now()).isoformat(timespec="seconds")


# ----------------------------------------------------------------- passwords

def validate_password(password: str) -> str:
    """Check a new password before it is hashed."""
    password = (password or "").strip()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError("La contraseña es demasiado larga (máximo 72 bytes).")
    return password


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check that never raises on malformed input."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# A real hash to check against when the email does not exist, so a failed
# login costs the same whether or not the account is there.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def random_password(length: int = 14) -> str:
    """A readable one-off password for an account an admin just created."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# -------------------------------------------------------------- access token

def create_access_token(user) -> tuple[str, int]:
    """Sign a JWT for `user`; returns the token and its lifetime in seconds."""
    issued = now()
    expires = issued + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iss": JWT_ISSUER,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "typ": "access",
        # Without an id, two tokens issued in the same second are byte
        # identical: one could not be told apart from another in a log.
        "jti": secrets.token_urlsafe(8),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, ACCESS_TOKEN_MINUTES * 60


def decode_access_token(token: str) -> dict:
    """Verify signature and expiry; raises AuthError with a readable reason."""
    try:
        payload = jwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALGORITHM], issuer=JWT_ISSUER,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("La sesión expiró.") from None
    except jwt.InvalidTokenError:
        raise AuthError("Token inválido.") from None
    if payload.get("typ") != "access":
        raise AuthError("Token inválido.")
    return payload


# ------------------------------------------------------------- refresh token

def new_refresh_token() -> tuple[str, str, str]:
    """Create one: returns (raw token, its hash, expiry timestamp)."""
    raw = secrets.token_urlsafe(48)
    expires = datetime.now() + timedelta(days=REFRESH_TOKEN_DAYS)
    return raw, hash_token(raw), stamp(expires)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_expired(timestamp: str) -> bool:
    """Compare a stored expiry against the same clock that wrote it."""
    try:
        return datetime.fromisoformat(timestamp) <= datetime.now()
    except ValueError:
        return True
