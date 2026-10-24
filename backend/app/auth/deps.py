"""FastAPI dependencies: session cookie, CSRF, roles."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request

from app.audit import Actor
from app.auth.service import ROLE_LEVEL, AuthService, Role, SessionInfo

COOKIE = "pvd_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_ip(request: Request) -> str | None:
    # uvicorn already applied X-Forwarded-For for trusted proxies (FORWARDED_ALLOW_IPS).
    return request.client.host if request.client else None


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth


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
    """Require a logged-in session. ``allow_pending`` also accepts sessions that still need
    TOTP / TOTP setup / a password change (only for the endpoints that complete those steps)."""

    async def dep(request: Request) -> SessionInfo:
        info = await _resolve(request)
        if info is None:
            raise HTTPException(401, "Nicht angemeldet")
        if info.pending and not allow_pending:
            raise HTTPException(403, f"Anmeldung unvollständig: {info.pending}")
        _check_csrf(request, info)
        return info

    return dep


def require_role(role: Role) -> Callable[..., Awaitable[SessionInfo]]:
    async def dep(info: SessionInfo = Depends(session())) -> SessionInfo:  # noqa: B008
        if ROLE_LEVEL.get(info.user.role, -1) < ROLE_LEVEL[role]:
            raise HTTPException(403, f"Rolle '{role}' erforderlich")
        return info

    return dep


def actor(request: Request, info: SessionInfo) -> Actor:
    return Actor(name=info.user.username, role=info.user.role, ip=client_ip(request))


# Shorthands for routers
viewer = Depends(require_role("viewer"))
operator = Depends(require_role("operator"))
admin = Depends(require_role("admin"))
