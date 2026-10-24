import time
from datetime import timedelta

import pyotp
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.config import mock_clusters
from app.core.crypto import hash_password, password_problem, verify_password
from app.core.ratelimit import RateLimiter
from app.db.models import Session, User, utcnow
from app.main import create_app
from app.pve import MockPveClient
from tests.conftest import ADMIN_PASSWORD, client_for, logged_in, login


def _app(settings, clock):
    clients = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    return create_app(settings, clients=clients)


@pytest.fixture
async def app(settings, clock):
    application = _app(settings, clock)
    async with application.router.lifespan_context(application):
        yield application


async def _add_user(app, username, role, password="user-password-123"):
    user, _ = await app.state.auth.create_user(username, role, password)
    return user


# --- unit ---------------------------------------------------------------------------------------


def test_password_hash_and_policy():
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "anything")  # unknown user path
    assert password_problem("short") and password_problem("admin-secret-pw", "admin")
    assert password_problem("a-long-enough-password") is None


def test_rate_limiter():
    t = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: t[0])
    assert rl.hit("k") and rl.hit("k") and not rl.hit("k")
    assert rl.retry_after("k") > 0
    t[0] = 61
    assert rl.hit("k")


# --- login flow ---------------------------------------------------------------------------------


async def test_login_sets_strict_httponly_cookie(app):
    async with client_for(app) as c:
        r = await login(c)
        assert r.status_code == 200 and r.json()["status"] == "ok"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "secure" not in cookie  # plain http in tests, cookie_secure=auto
        me = (await c.get("/api/v1/auth/me")).json()
        assert me["username"] == "admin" and me["role"] == "admin" and me["pending"] is None
        assert (await c.post("/api/v1/auth/logout")).status_code == 204
        assert (await c.get("/api/v1/clusters")).status_code == 401


async def test_wrong_password_and_lockout(app):
    async with client_for(app) as c:
        for _ in range(5):
            assert (await login(c, password="nope-nope-nope")).status_code == 401
        # Locked now - even the right password fails, with the same message.
        r = await login(c)
        assert r.status_code == 401 and "falsch" in r.json()["detail"]


async def test_unknown_user_same_error(app):
    async with client_for(app) as c:
        r = await login(c, username="ghost", password="whatever-123")
        assert r.status_code == 401 and r.json()["detail"] == "Benutzername oder Passwort falsch"


async def test_rate_limit_per_username(app):
    async with client_for(app) as c:
        codes = [(await login(c, username="ghost", password="x")).status_code for _ in range(11)]
        assert codes[-1] == 429


async def test_csrf_and_origin_checks(app):
    async with logged_in(app) as c:
        token = c.headers.pop("X-CSRF-Token")
        r = await c.post("/api/v1/inventory/refresh")
        assert r.status_code == 403 and "CSRF" in r.json()["detail"]
        r = await c.post("/api/v1/inventory/refresh", headers={"X-CSRF-Token": "forged"})
        assert r.status_code == 403
        r = await c.post("/api/v1/inventory/refresh",
                         headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
        assert r.status_code == 403 and "Cross-Origin" in r.json()["detail"]
        r = await c.post("/api/v1/inventory/refresh",
                         headers={"X-CSRF-Token": token, "Origin": "http://test"})
        assert r.status_code == 202


async def test_generated_admin_must_change_password(settings, clock, caplog):
    settings.admin_password = SecretStr("")
    app = _app(settings, clock)
    async with app.router.lifespan_context(app):
        line = next(r.getMessage() for r in caplog.records if "created with password" in r.getMessage())
        generated = line.rsplit(": ", 1)[1]
        async with client_for(app) as c:
            r = await login(c, password=generated)
            assert r.json()["status"] == "password_change"
            assert (await c.get("/api/v1/clusters")).status_code == 403  # incomplete login
            r = await c.post("/api/v1/auth/password", json={"current": generated, "new": "short"})
            assert r.status_code == 422
            r = await c.post("/api/v1/auth/password",
                             json={"current": generated, "new": "a-much-better-password"})
            assert r.status_code == 200 and r.json()["pending"] is None
            assert (await c.get("/api/v1/clusters")).status_code == 200


async def test_session_idle_timeout(app):
    async with logged_in(app) as c:
        async with app.state.db.session() as s:
            await s.execute(update(Session).values(last_seen_at=utcnow() - timedelta(hours=5)))
            await s.commit()
        assert (await c.get("/api/v1/clusters")).status_code == 401


async def test_password_change_revokes_other_sessions(app):
    async with logged_in(app) as a, logged_in(app) as b:
        r = await a.post("/api/v1/auth/password",
                         json={"current": ADMIN_PASSWORD, "new": "another-long-password"})
        assert r.status_code == 200
        assert (await a.get("/api/v1/clusters")).status_code == 200
        assert (await b.get("/api/v1/clusters")).status_code == 401


# --- TOTP ---------------------------------------------------------------------------------------


async def test_totp_enrolment_and_login(settings, clock):
    settings.totp_required_roles = "admin"
    app = _app(settings, clock)
    async with app.router.lifespan_context(app), client_for(app) as c:
        r = await login(c)
        assert r.json()["status"] == "totp_setup"
        assert (await c.get("/api/v1/clusters")).status_code == 403
        setup = (await c.post("/api/v1/auth/totp/setup")).json()
        assert setup["uri"].startswith("otpauth://totp/") and "<svg" in setup["qr_svg"]
        totp = pyotp.TOTP(setup["secret"])
        assert (await c.post("/api/v1/auth/totp/enable", json={"code": "000000"})).status_code == 400
        code = totp.now()
        r = await c.post("/api/v1/auth/totp/enable", json={"code": code})
        assert r.status_code == 200 and r.json()["totp_enabled"] and r.json()["pending"] is None
        assert (await c.get("/api/v1/clusters")).status_code == 200

        # Secret is stored encrypted, not in clear text
        async with app.state.db.session() as s:
            user = await s.scalar(select(User).where(User.username == "admin"))
            assert setup["secret"] not in user.totp_secret_enc

        # New login needs the second factor; replaying the used code is rejected.
        await c.post("/api/v1/auth/logout")
        c.headers.pop("X-CSRF-Token", None)
        r = await login(c)
        assert r.json()["status"] == "totp"
        assert (await c.get("/api/v1/clusters")).status_code == 403
        assert (await c.post("/api/v1/auth/totp/verify", json={"code": code})).status_code == 401
        next_code = totp.at(time.time() + 30)  # still inside the ±1 step window
        r = await c.post("/api/v1/auth/totp/verify", json={"code": next_code})
        assert r.status_code == 200
        assert (await c.get("/api/v1/clusters")).status_code == 200

        # Required role cannot switch TOTP off
        r = await c.post("/api/v1/auth/totp/disable",
                         json={"password": ADMIN_PASSWORD, "code": "123456"})
        assert r.status_code == 403


# --- roles & user management --------------------------------------------------------------------


async def test_roles_enforced(app):
    await _add_user(app, "viewer1", "viewer")
    async with logged_in(app, "viewer1", "user-password-123") as v:
        assert (await v.get("/api/v1/clusters")).status_code == 200
        assert (await v.get("/api/v1/admin/users")).status_code == 403
        r = await v.post("/api/v1/clusters/homelab/guests/100/power", json={"action": "start"})
        assert r.status_code == 403 and "operator" in r.json()["detail"]


async def test_user_management(app):
    async with logged_in(app) as a:
        r = await a.post("/api/v1/admin/users", json={"username": "op", "role": "operator"})
        assert r.status_code == 201
        temp = r.json()["temporary_password"]
        uid = r.json()["user"]["id"]
        assert temp and r.json()["user"]["must_change_password"]
        assert (await a.post("/api/v1/admin/users",
                             json={"username": "op", "role": "viewer"})).status_code == 422
        r = await a.patch(f"/api/v1/admin/users/{uid}", json={"role": "viewer"})
        assert r.json()["role"] == "viewer"

        async with logged_in(app, "op", temp) as op:
            assert (await op.get("/api/v1/auth/me")).json()["pending"] == "password_change"
            # Disabling ends the user's sessions immediately
            await a.patch(f"/api/v1/admin/users/{uid}", json={"disabled": True})
            assert (await op.get("/api/v1/auth/me")).status_code == 401

        me = (await a.get("/api/v1/auth/me")).json()
        admin_id = next(u["id"] for u in (await a.get("/api/v1/admin/users")).json()
                        if u["username"] == me["username"])
        r = await a.patch(f"/api/v1/admin/users/{admin_id}", json={"role": "viewer"})
        assert r.status_code == 422  # last admin
        assert (await a.delete(f"/api/v1/admin/users/{admin_id}")).status_code == 422
        assert (await a.delete(f"/api/v1/admin/users/{uid}")).status_code == 204

        audit = (await a.get("/api/v1/audit", params={"action": "user."})).json()
        assert {e["action"] for e in audit} >= {"user.create", "user.update", "user.delete"}
        assert all(e["actor"] == "admin" for e in audit)


async def test_audit_visibility(app):
    await _add_user(app, "viewer1", "viewer")
    async with logged_in(app, "viewer1", "user-password-123") as v:
        entries = (await v.get("/api/v1/audit")).json()
        assert entries and all(e["actor"] == "viewer1" for e in entries)  # own logins only
        assert entries[0]["ts"].endswith(("Z", "+00:00"))  # timezone-aware timestamps
    async with logged_in(app) as a:
        actors = {e["actor"] for e in (await a.get("/api/v1/audit")).json()}
        assert {"admin", "viewer1"} <= actors
