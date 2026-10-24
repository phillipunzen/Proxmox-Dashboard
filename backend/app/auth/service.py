"""Users, sessions and TOTP."""

from __future__ import annotations

import io
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pyotp
import segno
from sqlalchemy import delete, func, select

from app.config import Settings
from app.core.crypto import (
    Encryptor,
    hash_password,
    needs_rehash,
    new_token,
    password_problem,
    token_hash,
    verify_password,
)
from app.db.engine import Database
from app.db.models import Session, User, utcnow

log = logging.getLogger(__name__)

Role = Literal["viewer", "operator", "admin"]
ROLES: tuple[Role, ...] = ("viewer", "operator", "admin")
ROLE_LEVEL = {r: i for i, r in enumerate(ROLES)}
Pending = Literal["totp", "totp_setup", "password_change"] | None

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
TOTP_ISSUER = "Proxmox Dashboard"


class AuthError(Exception):
    """Login failed. The message is safe to show (never reveals whether the user exists)."""


@dataclass
class SessionInfo:
    session: Session
    user: User
    pending: Pending


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; everything we store is UTC.
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


class AuthService:
    def __init__(self, db: Database, settings: Settings, encryptor: Encryptor):
        self.db = db
        self.settings = settings
        self.enc = encryptor

    # --- bootstrap ------------------------------------------------------------------------------

    async def bootstrap_admin(self) -> str | None:
        """Create the first admin if there are no users. Returns a generated password, if any."""
        async with self.db.session() as s:
            if await s.scalar(select(func.count(User.id))):
                return None
            password = self.settings.admin_password.get_secret_value()
            generated = None
            if not password:
                generated = password = secrets.token_urlsafe(15)
            s.add(User(username=self.settings.admin_username, password_hash=hash_password(password),
                       role="admin", must_change_password=generated is not None))
            await s.commit()
        return generated

    # --- login ----------------------------------------------------------------------------------

    async def authenticate(self, username: str, password: str) -> User:
        async with self.db.session() as s:
            user = await s.scalar(select(User).where(User.username == username))
            now = utcnow()
            locked = user is not None and (_aware(user.locked_until) or now) > now
            ok = verify_password(user.password_hash if user else None, password)
            sso_only = user is not None and user.auth_source == "oidc"
            if user is None or user.disabled or locked or not ok or sso_only:
                if user is not None and not locked and not ok:
                    user.failed_logins += 1
                    if user.failed_logins >= MAX_FAILED_LOGINS:
                        user.locked_until = now + LOCKOUT
                        user.failed_logins = 0
                        log.warning("User %s locked for %s after failed logins", username, LOCKOUT)
                    await s.commit()
                raise AuthError("Benutzername oder Passwort falsch")
            user.failed_logins = 0
            user.locked_until = None
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)
            await s.commit()
            return user

    def pending_for(self, user: User, mfa_ok: bool) -> Pending:
        if user.auth_source == "oidc":
            return None  # MFA and password policy are the identity provider's job
        if user.totp_enabled and not mfa_ok:
            return "totp"
        if not user.totp_enabled and user.role in self.settings.totp_roles:
            return "totp_setup"
        if user.must_change_password:
            return "password_change"
        return None

    async def login_oidc(self, sub: str, username: str, role: str, link_existing: bool) -> User:
        """Find or create the user for an IdP identity; role follows the IdP groups."""
        async with self.db.session() as s:
            user = await s.scalar(select(User).where(User.oidc_sub == sub))
            if user is None:
                user = await s.scalar(select(User).where(User.username == username))
                if user is not None and user.auth_source != "oidc" and not link_existing:
                    raise AuthError(f"Lokaler Benutzer '{username}' existiert bereits – "
                                    "OIDC_LINK_EXISTING=true setzen, um ihn zu verknüpfen")
                if user is None:
                    user = User(username=username, role=role,
                                password_hash=hash_password(secrets.token_urlsafe(32)))
                    s.add(user)
                user.oidc_sub = sub
                user.auth_source = "oidc"
            if user.disabled:
                raise AuthError("Benutzer ist deaktiviert")
            user.role = role
            user.must_change_password = False
            await s.commit()
            return user

    async def create_session(self, user: User, ip: str | None, user_agent: str | None) -> str:
        token = new_token()
        now = utcnow()
        async with self.db.session() as s:
            s.add(Session(id=token_hash(token), user_id=user.id, csrf_token=new_token(),
                          mfa_ok=not user.totp_enabled or user.auth_source == "oidc",
                          created_at=now, last_seen_at=now,
                          expires_at=now + timedelta(hours=self.settings.session_max_hours),
                          ip=ip, user_agent=(user_agent or "")[:255]))
            u = await s.get(User, user.id)
            assert u is not None
            u.last_login_at = now
            await s.commit()
        return token

    async def resolve(self, token: str | None) -> SessionInfo | None:
        """Validate a session token (expiry, idle timeout, disabled user)."""
        if not token:
            return None
        async with self.db.session() as s:
            sess = await s.get(Session, token_hash(token))
            if sess is None:
                return None
            user = await s.get(User, sess.user_id)
            now = utcnow()
            idle = timedelta(minutes=self.settings.session_idle_minutes)
            expired = (_aware(sess.expires_at) or now) <= now or \
                (_aware(sess.last_seen_at) or now) + idle <= now
            if user is None or user.disabled or expired:
                await s.delete(sess)
                await s.commit()
                return None
            # Touch at most once a minute to keep writes low.
            if now - (_aware(sess.last_seen_at) or now) > timedelta(seconds=60):
                sess.last_seen_at = now
                await s.commit()
            return SessionInfo(sess, user, self.pending_for(user, sess.mfa_ok))

    async def logout(self, token: str | None) -> None:
        if token:
            async with self.db.session() as s:
                await s.execute(delete(Session).where(Session.id == token_hash(token)))
                await s.commit()

    async def list_sessions(self, user_id: int) -> list[Session]:
        async with self.db.session() as s:
            res = await s.scalars(select(Session).where(Session.user_id == user_id)
                                  .order_by(Session.last_seen_at.desc()))
            return list(res)

    async def revoke_sessions(self, user_id: int, keep_id: str | None = None,
                              only_prefix: str | None = None) -> int:
        async with self.db.session() as s:
            sessions = await s.scalars(select(Session).where(Session.user_id == user_id))
            n = 0
            for sess in sessions:
                if sess.id == keep_id or (only_prefix and not sess.id.startswith(only_prefix)):
                    continue
                await s.delete(sess)
                n += 1
            await s.commit()
            return n

    async def cleanup_sessions(self) -> None:
        async with self.db.session() as s:
            await s.execute(delete(Session).where(Session.expires_at <= utcnow()))
            await s.commit()

    # --- TOTP -----------------------------------------------------------------------------------

    def _totp(self, user: User) -> pyotp.TOTP | None:
        if not user.totp_secret_enc:
            return None
        return pyotp.TOTP(self.enc.decrypt(user.totp_secret_enc))

    async def verify_totp(self, user_id: int, code: str, session_id: str | None = None,
                          require_enabled: bool = True) -> bool:
        """Check a code (±30 s), reject replays, mark the session as MFA-complete."""
        code = code.strip().replace(" ", "")
        if not code.isdigit() or len(code) != 6:
            return False
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if user is None or (require_enabled and not user.totp_enabled):
                return False
            totp = self._totp(user)
            if totp is None:
                return False
            step_now = int(time.time()) // 30
            matched = next((step for step in (step_now - 1, step_now, step_now + 1)
                            if secrets.compare_digest(totp.at(step * 30), code)), None)
            if matched is None or matched <= user.totp_last_step:
                return False
            user.totp_last_step = matched
            if session_id:
                sess = await s.get(Session, session_id)
                if sess:
                    sess.mfa_ok = True
            await s.commit()
            return True

    async def start_totp_setup(self, user_id: int) -> dict[str, str]:
        """New secret (not active until confirmed with a code). Returns URI + QR as SVG."""
        secret = pyotp.random_base32()
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            assert user is not None
            if user.totp_enabled:
                raise AuthError("TOTP ist bereits aktiv")
            user.totp_secret_enc = self.enc.encrypt(secret)
            user.totp_last_step = 0
            await s.commit()
            uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=TOTP_ISSUER)
        buf = io.BytesIO()
        segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#000",
                                        light="#fff", xmldecl=False, svgns=True, omitsize=True)
        return {"secret": secret, "uri": uri, "qr_svg": buf.getvalue().decode()}

    async def enable_totp(self, user_id: int, code: str, session_id: str | None) -> bool:
        if not await self.verify_totp(user_id, code, session_id, require_enabled=False):
            return False
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            assert user is not None
            user.totp_enabled = True
            await s.commit()
        # Other sessions were created without the second factor - end them.
        await self.revoke_sessions(user_id, keep_id=session_id)
        return True

    async def disable_totp(self, user_id: int) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            assert user is not None
            user.totp_enabled = False
            user.totp_secret_enc = None
            user.totp_last_step = 0
            await s.commit()

    # --- users ----------------------------------------------------------------------------------

    async def list_users(self) -> list[User]:
        async with self.db.session() as s:
            return list(await s.scalars(select(User).order_by(User.username)))

    async def get_user(self, user_id: int) -> User | None:
        async with self.db.session() as s:
            return await s.get(User, user_id)

    async def create_user(self, username: str, role: Role,
                          password: str | None) -> tuple[User, str]:
        temp = None
        if password is None:
            temp = password = secrets.token_urlsafe(15)
        elif problem := password_problem(password, username):
            raise ValueError(problem)
        async with self.db.session() as s:
            if await s.scalar(select(User).where(User.username == username)):
                raise ValueError(f"Benutzer '{username}' existiert bereits")
            user = User(username=username, password_hash=hash_password(password), role=role,
                        must_change_password=temp is not None)
            s.add(user)
            await s.commit()
            return user, temp or ""

    async def update_user(self, user_id: int, role: Role | None = None,
                          disabled: bool | None = None) -> User:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise LookupError(user_id)
            removing_admin = user.role == "admin" and (
                (role is not None and role != "admin") or disabled)
            if removing_admin and await self._admin_count(s) <= 1:
                raise ValueError("Der letzte aktive Admin kann nicht herabgestuft oder "
                                 "deaktiviert werden")
            if role is not None:
                user.role = role
            if disabled is not None:
                user.disabled = disabled
                if not disabled:
                    user.locked_until = None
            await s.commit()
        if disabled:
            await self.revoke_sessions(user_id)
        return user

    async def delete_user(self, user_id: int) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise LookupError(user_id)
            if user.role == "admin" and await self._admin_count(s) <= 1:
                raise ValueError("Der letzte Admin kann nicht gelöscht werden")
            await s.delete(user)
            await s.commit()

    async def set_password(self, user_id: int, password: str, temporary: bool = False) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise LookupError(user_id)
            if not temporary and (problem := password_problem(password, user.username)):
                raise ValueError(problem)
            user.password_hash = hash_password(password)
            user.must_change_password = temporary
            await s.commit()

    async def change_own_password(self, user_id: int, current: str, new: str,
                                  keep_session: str) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            assert user is not None
            if not verify_password(user.password_hash, current):
                raise AuthError("Aktuelles Passwort ist falsch")
            if secrets.compare_digest(current, new):
                raise ValueError("Neues Passwort muss sich vom alten unterscheiden")
        await self.set_password(user_id, new)
        await self.revoke_sessions(user_id, keep_id=keep_session)

    @staticmethod
    async def _admin_count(s) -> int:  # type: ignore[no-untyped-def]
        return int(await s.scalar(select(func.count(User.id)).where(
            User.role == "admin", User.disabled.is_(False))) or 0)
