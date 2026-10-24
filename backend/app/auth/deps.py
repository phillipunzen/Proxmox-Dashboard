"""FastAPI dependencies: session cookie or API key, CSRF, roles."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request

from app.audit import Actor
from app.auth.apikeys import ApiKeyService, role_for
from app.auth.service import ROLE_LEVEL, AuthService, Role, SessionInfo

COOKIE = "pvd_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Principal:
    """Who is calling: a logged-in user (session) or an API key."""

    name: str
    role: str
    kind: Literal["session", "apikey"]
    session: SessionInfo | None = None
    key_id: int | None = None


def client_ip(request: Request) -> str | None:
    # uvicorn already applied X-Forwarded-For for trusted proxies (FORWARDED_ALLOW_IPS).
    return request.client.host if request.client else None


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth


def api_key_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


async def _resolve(request: Request) -> SessionInfo | None:
    if not hasattr(request.state, "session_info"):
        request.state.session_info = await auth_service(request).resolve(
            request.cookies.get(COOKIE))
    return request.state.session_info


def _check_csrf(request: Request, info: SessionInfo) -> None:
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get(CSRF_HEADER, "")
    if not sent or not secrets.compare_digest(sent, info.session.csrf_token):
        raise HTTPException(403, "CSRF-Token fehlt oder ist ungültig")


def session(allow_pending: bool = False) -> Callable[[Request], Awaitable[SessionInfo]]:
    """Require a logged-in *browser session* (account endpoints). ``allow_pending`` also
    accepts sessions that still need TOTP / TOTP setup / a password change."""

    async def dep(request: Request) -> SessionInfo:
        info = await _resolve(request)
        if info is None:
            raise HTTPException(401, "Nicht angemeldet")
        if info.pending and not allow_pending:
            raise HTTPException(403, f"Anmeldung unvollständig: {info.pending}")
        _check_csrf(request, info)
        return info

    return dep


async def principal(request: Request) -> Principal:
    """Session cookie or API key (``Authorization: Bearer pvd_…`` / ``X-API-Key``)."""
    if hasattr(request.state, "principal"):
        return request.state.principal
    key = api_key_from(request)
    if key:
        keys: ApiKeyService = request.app.state.api_keys
        row = await keys.verify(key, client_ip(request))
        if row is None:
            raise HTTPException(401, "API-Key ungültig, abgelaufen oder widerrufen",
                                headers={"WWW-Authenticate": "Bearer"})
        p = Principal(name=f"apikey:{row.name}", role=role_for(row.scopes), kind="apikey",
                      key_id=row.id)
    else:
        info = await session()(request)
        p = Principal(name=info.user.username, role=info.user.role, kind="session", session=info)
    request.state.principal = p
    return p


def require_role(role: Role) -> Callable[..., Awaitable[Principal]]:
    async def dep(p: Principal = Depends(principal)) -> Principal:  # noqa: B008
        if ROLE_LEVEL.get(p.role, -1) < ROLE_LEVEL[role]:
            scope = {"viewer": "read", "operator": "control", "admin": "admin"}[role]
            raise HTTPException(403, f"Rolle '{role}' bzw. Scope '{scope}' erforderlich")
        return p

    return dep


def require_admin_session() -> Callable[..., Awaitable[SessionInfo]]:
    """User and API-key management: only a logged-in admin, never an API key."""

    async def dep(info: SessionInfo = Depends(session())) -> SessionInfo:  # noqa: B008
        if info.user.role != "admin":
            raise HTTPException(403, "Rolle 'admin' erforderlich")
        return info

    return dep


def actor(request: Request, who: Principal | SessionInfo) -> Actor:
    if isinstance(who, SessionInfo):
        return Actor(name=who.user.username, role=who.user.role, ip=client_ip(request))
    return Actor(name=who.name, role=who.role, ip=client_ip(request))


# Shorthands for routers
viewer = Depends(require_role("viewer"))
operator = Depends(require_role("operator"))
admin = Depends(require_role("admin"))
admin_session = Depends(require_admin_session())
