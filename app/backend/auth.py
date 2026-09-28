"""
Auth utilities: bcrypt password hashing, HS256 JWT issue/verify, and the
`current_user` FastAPI dependency that decodes the Bearer token → user row.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "720"))  # 30 days by default

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# auto_error=False so we can raise a consistent 401 ourselves.
bearer_scheme = HTTPBearer(auto_error=False)


# ---------- Password hashing ----------
def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


# ---------- JWT ----------
def create_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str:
    """Return the user id (sub) from a valid token, or raise 401."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token payload")
    return sub


# ---------- Dependency ----------
async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """Decode the Bearer JWT and load the user row. 401 on any failure."""
    # Imported here to avoid a circular import (db imports auth for hashing).
    from db import get_pool

    if creds is None or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    user_id = _decode_token(creds.credentials)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, email, role FROM users WHERE id = $1", user_id
        )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return dict(row)
