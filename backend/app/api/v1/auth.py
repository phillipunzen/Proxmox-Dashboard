"""Login, logout, TOTP, own account and user management."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import audit
from app.auth.deps import COOKIE, actor, admin, auth_service, client_ip, session
from app.auth.service import AuthError, Pending, Role, SessionInfo, _aware
from app.core.crypto import token_hash
from app.core.ratelimit import RateLimiter
from app.db.models import User, utcnow

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/v1/admin/users", tags=["admin"])


class Limiters:
    """Login throttling per client IP and per username (on top of the account lockout)."""

    def __init__(self) -> None:
        self.ip = RateLimiter(limit=20, window_s=300)
        self.user = RateLimiter(limit=10, window_s=300)
        self.totp = RateLimiter(limit=10, window_s=300)


def _limits(request: Request) -> Limiters:
    return request.app.state.limiters

Username = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")]


class Me(BaseModel):
    username: str
    role: str
    totp_enabled: bool
    totp_required: bool
    must_change_password: bool
    pending: Pending
    csrf_token: str


class LoginRequest(BaseModel):
    username: Username
    password: str = Field(min_length=1, max_length=1024)


class LoginResult(BaseModel):
    status: Literal["ok", "totp", "totp_setup", "password_change"]
    me: Me


class CodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(min_length=1, max_length=1024)


class TotpDisable(BaseModel):
    password: str = Field(max_length=1024)
    code: str = Field(min_length=6, max_length=8)


def _me(request: Request, info: SessionInfo) -> Me:
    return Me(username=info.user.username, role=info.user.role,
              totp_enabled=info.user.totp_enabled,
              totp_required=info.user.role in request.app.state.settings.totp_roles,
              must_change_password=info.user.must_change_password, pending=info.pending,
              csrf_token=info.session.csrf_token)


def _cookie_secure(request: Request) -> bool:
    mode = request.app.state.settings.cookie_secure
    return request.url.scheme == "https" if mode == "auto" else mode == "true"


@router.post("/login", response_model=LoginResult)
async def login(request: Request, response: Response, body: LoginRequest) -> LoginResult:
    ip = client_ip(request) or "?"
    svc = auth_service(request)
    db = request.app.state.db
    lim = _limits(request)
    if not lim.ip.hit(ip) or not lim.user.hit(body.username.lower()):
        await audit.record(db, audit.Actor(name=body.username, ip=ip), "auth.login", "denied",
                           message="Rate-Limit")
        raise HTTPException(429, "Zu viele Anmeldeversuche, bitte später erneut versuchen",
                            headers={"Retry-After": str(lim.ip.retry_after(ip) or 60)})
    try:
        user = await svc.authenticate(body.username, body.password)
    except AuthError as exc:
        await audit.record(db, audit.Actor(name=body.username, ip=ip), "auth.login", "error",
                           message=str(exc))
        raise HTTPException(401, str(exc)) from exc

    lim.user.reset(body.username.lower())
    token = await svc.create_session(user, ip, request.headers.get("user-agent"))
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/",
                        secure=_cookie_secure(request),
                        max_age=int(request.app.state.settings.session_max_hours * 3600))
    info = await svc.resolve(token)
    assert info is not None
    await audit.record(db, audit.Actor(name=user.username, role=user.role, ip=ip), "auth.login",
                       "ok", message=f"weiter: {info.pending}" if info.pending else None)
    return LoginResult(status=info.pending or "ok", me=_me(request, info))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response,
                 info: SessionInfo = Depends(session(allow_pending=True))) -> None:  # noqa: B008
    await auth_service(request).logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")


@router.get("/me", response_model=Me)
async def me(request: Request,
             info: SessionInfo = Depends(session(allow_pending=True))) -> Me:  # noqa: B008
    return _me(request, info)


@router.post("/totp/verify", response_model=Me, summary="Second login step")
async def totp_verify(request: Request, body: CodeRequest,
                      info: SessionInfo = Depends(session(allow_pending=True))) -> Me:  # noqa: B008
    if info.pending != "totp":
        raise HTTPException(409, "Kein TOTP-Schritt offen")
    if not _limits(request).totp.hit(info.user.username):
        raise HTTPException(429, "Zu viele Versuche")
    ok = await auth_service(request).verify_totp(info.user.id, body.code, info.session.id)
    await audit.record(request.app.state.db, actor(request, info), "auth.totp",
                       "ok" if ok else "error", message=None if ok else "falscher Code")
    if not ok:
        raise HTTPException(401, "Code ungültig")
    return await me(request, await _fresh(request))


@router.post("/totp/setup", summary="Start TOTP enrolment (returns secret + QR code)")
async def totp_setup(request: Request,
                     info: SessionInfo = Depends(session(allow_pending=True))) -> dict[str, str]:  # noqa: B008
    if info.pending == "totp":
        raise HTTPException(403, "Erst mit dem bestehenden Code anmelden")
    try:
        return await auth_service(request).start_totp_setup(info.user.id)
    except AuthError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/totp/enable", response_model=Me, summary="Confirm TOTP enrolment with a code")
async def totp_enable(request: Request, body: CodeRequest,
                      info: SessionInfo = Depends(session(allow_pending=True))) -> Me:  # noqa: B008
    if info.pending == "totp":
        raise HTTPException(403, "Erst mit dem bestehenden Code anmelden")
    if not _limits(request).totp.hit(info.user.username):
        raise HTTPException(429, "Zu viele Versuche")
    ok = await auth_service(request).enable_totp(info.user.id, body.code, info.session.id)
    await audit.record(request.app.state.db, actor(request, info), "auth.totp.enable",
                       "ok" if ok else "error")
    if not ok:
        raise HTTPException(400, "Code ungültig - Uhrzeit auf dem Handy prüfen")
    return await me(request, await _fresh(request))


@router.post("/totp/disable", status_code=204)
async def totp_disable(request: Request, body: TotpDisable,
                       info: SessionInfo = Depends(session())) -> None:  # noqa: B008
    svc = auth_service(request)
    if info.user.role in request.app.state.settings.totp_roles:
        raise HTTPException(403, "Für diese Rolle ist TOTP Pflicht")
    try:
        await svc.authenticate(info.user.username, body.password)
    except AuthError as exc:
        raise HTTPException(401, "Passwort falsch") from exc
    if not await svc.verify_totp(info.user.id, body.code):
        raise HTTPException(401, "Code ungültig")
    await svc.disable_totp(info.user.id)
    await audit.record(request.app.state.db, actor(request, info), "auth.totp.disable", "ok")


@router.post("/password", response_model=Me)
async def change_password(request: Request, body: PasswordChange,
                          info: SessionInfo = Depends(session(allow_pending=True))) -> Me:  # noqa: B008
    if info.pending in ("totp", "totp_setup"):
        raise HTTPException(403, "Erst die Zwei-Faktor-Anmeldung abschließen")
    try:
        await auth_service(request).change_own_password(info.user.id, body.current, body.new,
                                                        keep_session=info.session.id)
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit.record(request.app.state.db, actor(request, info), "auth.password", "ok")
    return await me(request, await _fresh(request))


class SessionView(BaseModel):
    id: str
    current: bool
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str | None


@router.get("/sessions", response_model=list[SessionView])
async def my_sessions(request: Request,
                      info: SessionInfo = Depends(session())) -> list[SessionView]:  # noqa: B008
    current = token_hash(request.cookies.get(COOKIE, ""))
    return [SessionView(id=s.id[:16], current=s.id == current, created_at=s.created_at,
                        last_seen_at=s.last_seen_at, ip=s.ip, user_agent=s.user_agent)
            for s in await auth_service(request).list_sessions(info.user.id)]


@router.delete("/sessions/{sid}", status_code=204)
async def revoke_session(request: Request, sid: str,
                         info: SessionInfo = Depends(session())) -> None:  # noqa: B008
    if len(sid) != 16:
        raise HTTPException(422, "Ungültige Session-ID")
    n = await auth_service(request).revoke_sessions(info.user.id, keep_id=info.session.id,
                                                    only_prefix=sid)
    if not n:
        raise HTTPException(404, "Session nicht gefunden")


async def _fresh(request: Request) -> SessionInfo:
    info = await auth_service(request).resolve(request.cookies.get(COOKIE))
    if info is None:
        raise HTTPException(401, "Session abgelaufen")
    return info


# --- user management (admin) --------------------------------------------------------------------


class UserView(BaseModel):
    id: int
    username: str
    role: str
    totp_enabled: bool
    disabled: bool
    must_change_password: bool
    locked: bool
    created_at: datetime
    last_login_at: datetime | None


class UserCreate(BaseModel):
    username: Username
    role: Role = "viewer"
    password: str | None = Field(None, max_length=1024,
                                 description="Empty = random temporary password")


class UserUpdate(BaseModel):
    role: Role | None = None
    disabled: bool | None = None


class TempPassword(BaseModel):
    user: UserView
    temporary_password: str | None


def _user_view(u: User) -> UserView:
    locked = u.locked_until is not None and _aware(u.locked_until) > utcnow()
    return UserView(id=u.id, username=u.username, role=u.role, totp_enabled=u.totp_enabled,
                    disabled=u.disabled, must_change_password=u.must_change_password,
                    locked=locked, created_at=u.created_at, last_login_at=u.last_login_at)


@users_router.get("", response_model=list[UserView])
async def list_users(request: Request, _: SessionInfo = admin) -> list[UserView]:
    return [_user_view(u) for u in await auth_service(request).list_users()]


@users_router.post("", response_model=TempPassword, status_code=201)
async def create_user(request: Request, body: UserCreate,
                      info: SessionInfo = admin) -> TempPassword:
    try:
        user, temp = await auth_service(request).create_user(body.username, body.role,
                                                             body.password or None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit.record(request.app.state.db, actor(request, info), "user.create", "ok",
                       target=body.username, params={"role": body.role})
    return TempPassword(user=_user_view(user), temporary_password=temp or None)


@users_router.patch("/{user_id}", response_model=UserView)
async def update_user(request: Request, user_id: int, body: UserUpdate,
                      info: SessionInfo = admin) -> UserView:
    if user_id == info.user.id and body.disabled:
        raise HTTPException(422, "Du kannst dich nicht selbst deaktivieren")
    try:
        user = await auth_service(request).update_user(user_id, body.role, body.disabled)
    except LookupError as exc:
        raise HTTPException(404, "Benutzer nicht gefunden") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit.record(request.app.state.db, actor(request, info), "user.update", "ok",
                       target=user.username, params=body.model_dump(exclude_none=True))
    return _user_view(user)


@users_router.post("/{user_id}/reset-password", response_model=TempPassword)
async def reset_password(request: Request, user_id: int, info: SessionInfo = admin) -> TempPassword:
    svc = auth_service(request)
    temp = secrets.token_urlsafe(15)
    try:
        await svc.set_password(user_id, temp, temporary=True)
    except LookupError as exc:
        raise HTTPException(404, "Benutzer nicht gefunden") from exc
    await svc.revoke_sessions(user_id)
    user = await svc.get_user(user_id)
    assert user is not None
    await audit.record(request.app.state.db, actor(request, info), "user.reset_password", "ok",
                       target=user.username)
    return TempPassword(user=_user_view(user), temporary_password=temp)


@users_router.post("/{user_id}/reset-totp", response_model=UserView)
async def reset_totp(request: Request, user_id: int, info: SessionInfo = admin) -> UserView:
    svc = auth_service(request)
    user = await svc.get_user(user_id)
    if user is None:
        raise HTTPException(404, "Benutzer nicht gefunden")
    await svc.disable_totp(user_id)
    await svc.revoke_sessions(user_id)
    await audit.record(request.app.state.db, actor(request, info), "user.reset_totp", "ok",
                       target=user.username)
    user = await svc.get_user(user_id)
    return _user_view(user)


@users_router.delete("/{user_id}", status_code=204)
async def delete_user(request: Request, user_id: int, info: SessionInfo = admin) -> None:
    if user_id == info.user.id:
        raise HTTPException(422, "Du kannst dich nicht selbst löschen")
    svc = auth_service(request)
    user = await svc.get_user(user_id)
    try:
        await svc.delete_user(user_id)
    except LookupError as exc:
        raise HTTPException(404, "Benutzer nicht gefunden") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit.record(request.app.state.db, actor(request, info), "user.delete", "ok",
                       target=user.username if user else str(user_id))
