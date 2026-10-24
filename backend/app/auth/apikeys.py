"""API keys with scopes for external tools.

Key format: ``pvd_<43 random url-safe chars>``. Only its SHA-256 is stored; the plain key
is shown exactly once when it is created.

Scopes are hierarchical and map onto dashboard roles:
``read`` → viewer, ``control`` → operator, ``admin`` → admin.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Literal

from sqlalchemy import select

from app.core.crypto import token_hash
from app.db.engine import Database
from app.db.models import ApiKey, utcnow

Scope = Literal["read", "control", "admin"]
SCOPE_ROLE = {"read": "viewer", "control": "operator", "admin": "admin"}
SCOPE_ORDER = ("read", "control", "admin")
PREFIX = "pvd_"


def role_for(scopes: list[str]) -> str:
    best = max((SCOPE_ORDER.index(s) for s in scopes if s in SCOPE_ORDER), default=0)
    return SCOPE_ROLE[SCOPE_ORDER[best]]


class ApiKeyService:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, name: str, scopes: list[Scope], created_by: str,
                     expires_days: int | None) -> tuple[ApiKey, str]:
        key = PREFIX + secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(days=expires_days) if expires_days else None
        async with self.db.session() as s:
            if await s.scalar(select(ApiKey).where(ApiKey.name == name)):
                raise ValueError(f"Ein API-Key '{name}' existiert bereits")
            row = ApiKey(name=name, prefix=key[:12], key_hash=token_hash(key),
                         scopes=sorted(set(scopes), key=SCOPE_ORDER.index), created_by=created_by,
                         expires_at=expires)
            s.add(row)
            await s.commit()
        return row, key

    async def verify(self, key: str, ip: str | None) -> ApiKey | None:
        if not key.startswith(PREFIX) or len(key) > 128:
            return None
        async with self.db.session() as s:
            row = await s.scalar(select(ApiKey).where(ApiKey.key_hash == token_hash(key)))
            now = utcnow()
            if row is None or row.revoked or (row.expires_at and row.expires_at <= now):
                return None
            # Record usage at most once a minute (keeps SQLite writes low under polling).
            if row.last_used_at is None or now - row.last_used_at > timedelta(seconds=60):
                row.last_used_at = now
                row.last_used_ip = ip
                await s.commit()
            return row

    async def list(self) -> list[ApiKey]:
        async with self.db.session() as s:
            return list(await s.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))

    async def revoke(self, key_id: int) -> ApiKey:
        async with self.db.session() as s:
            row = await s.get(ApiKey, key_id)
            if row is None:
                raise LookupError(key_id)
            row.revoked = True
            await s.commit()
            return row
