"""Backups/forecast, OIDC, guest updates, whoami, schema migration."""

import asyncio
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from sqlalchemy import text

from app.backups.service import classify, forecast, linear_trend, resolve_job
from app.collector.models import ClusterSnapshot, GuestView
from app.config import mock_clusters
from app.db.engine import Database
from app.main import create_app
from app.pve import MockPveClient
from app.pve.mock_ssh import MockSshRunner
from app.tsdb.client import Series
from tests.conftest import client_for, logged_in
from tests.test_phase4 import AutoClock

ISSUER = "https://idp.test/app/"


# --- backups: pure logic --------------------------------------------------------------------


def _g(vmid, node="n1", pool=None, template=False):
    return GuestView(cluster="c", vmid=vmid, name=f"g{vmid}", type="qemu", node=node,
                     status="running", pool=pool, template=template)


def test_resolve_job_selection():
    snap = ClusterSnapshot(name="c", guests=[_g(100), _g(101, "n2"), _g(102, pool="k8s"),
                                             _g(900, template=True)])
    assert resolve_job({"all": 1, "exclude": "101"}, snap) == ([100, 102],
                                                               "alle Gäste ohne 101")
    assert resolve_job({"all": 1, "node": "n2"}, snap)[0] == [101]
    assert resolve_job({"pool": "k8s"}, snap)[0] == [102]
    assert resolve_job({"vmid": "100,101,555"}, snap)[0] == [100, 101]


def test_trend_and_forecast():
    day = 86400
    points = [(i * day, 100.0 + 10 * i) for i in range(10)]  # +10 per day
    slope = linear_trend(points)
    assert slope == pytest.approx(10 / day)
    full, ninety = forecast(190, 290, slope)
    assert full == pytest.approx(10) and ninety == pytest.approx(7.1)
    assert forecast(100, 200, -1.0) == (None, None)
    assert linear_trend([(0, 1), (1, 2)]) is None


def test_classify_states():
    from datetime import UTC, datetime

    from app.backups.service import BackupVolume

    now = time.time()
    fresh = BackupVolume(volid="x", storage="pbs", ctime=datetime.fromtimestamp(now - 3600, UTC),
                         size=1, verified="ok")
    old = fresh.model_copy(update={"ctime": datetime.fromtimestamp(now - 80 * 3600, UTC)})
    bad = fresh.model_copy(update={"verified": "failed"})
    g = _g(1)
    assert classify(g, fresh, ["job"], 48, now)[0] == "ok"
    assert classify(g, old, ["job"], 48, now)[0] == "old"
    assert classify(g, None, ["job"], 48, now)[0] == "missing"
    assert classify(g, None, [], 48, now)[0] == "not_covered"
    assert classify(g, bad, ["job"], 48, now)[0] == "failed_verify"


# --- backups: API against the mock ----------------------------------------------------------


@pytest.fixture
async def env(settings):
    settings.read_only = False
    clock = AutoClock()
    mocks = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    app = create_app(settings, clients=mocks, ssh_runner=MockSshRunner(mocks, line_delay=0))
    async with app.router.lifespan_context(app):
        app.state.nodes.poll_sleep = 0.01
        async with logged_in(app) as api:
            yield app, api


async def test_backup_overview_api(env):
    app, api = env
    await api.post("/api/v1/backups/refresh")
    data = (await api.get("/api/v1/backups", params={"cluster": "homelab"})).json()
    by_name = {g["name"]: g for g in data["guests"]}
    assert by_name["pbs-test"]["state"] == "not_covered"
    assert by_name["k3s-master"]["state"] == "old" and by_name["k3s-master"]["age_hours"] > 48
    assert by_name["homeassistant"]["state"] == "ok"
    assert by_name["homeassistant"]["last_backup"]["storage"] == "pbs"
    assert by_name["gitlab"]["covered_by"] == ["backup-daily-pbs", "backup-weekly-nas"]
    assert by_name["gitlab"]["oldest_snapshot_days"] > 90
    assert data["counts"]["not_covered"] == 2
    jobs = (await api.get("/api/v1/backups/jobs", params={"cluster": "homelab"})).json()
    old = next(j for j in jobs if j["id"] == "backup-old")
    assert old["enabled"] is False and old["selection"] == "Pool k8s"

    # Alert rules use the same data
    await app.state.alerts.evaluate()
    names = {e["rule_name"] for e in (await api.get("/api/v1/alerts")).json()}
    assert {"Kein aktuelles Backup", "Alter Snapshot"} <= names
    assert "Gast ohne Backup-Job" not in names  # seeded disabled


async def test_storage_forecast_from_tsdb(settings):
    class FakeTsdb:
        async def query_range(self, q, start, end, step):
            now = time.time()
            return [Series({"cluster": "homelab", "node": "shared", "storage": "pbs",
                            "type": "pbs"}, [(now - (13 - i) * 86400, 1000.0 + 50 * i)
                                             for i in range(14)])]

        async def query(self, q, at=None):
            return [Series({"cluster": "homelab", "node": "shared", "storage": "pbs"},
                           [(time.time(), 2000.0)])]

    from app.backups.service import BackupService
    from app.collector.state import SnapshotStore

    svc = BackupService(SnapshotStore(), {}, FakeTsdb())  # type: ignore[arg-type]
    await svc.refresh()
    f = svc.forecasts[0]
    assert f.storage == "pbs" and f.growth_per_day == pytest.approx(50)
    assert f.days_until_full == pytest.approx((2000 - 1650) / 50)


# --- seeding & migration --------------------------------------------------------------------


async def test_new_default_rules_seeded_once_and_respect_deletions(env):
    app, api = env
    rules = (await api.get("/api/v1/alerts/rules")).json()
    snap = next(r for r in rules if r["name"] == "Alter Snapshot")
    await api.delete(f"/api/v1/alerts/rules/{snap['id']}")
    await app.state.alerts.seed_defaults()  # e.g. next start
    names = [r["name"] for r in (await api.get("/api/v1/alerts/rules")).json()]
    assert "Alter Snapshot" not in names and names.count("Node offline") == 1


async def test_additive_migration_on_old_schema(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with db.engine.begin() as conn:  # users table as created by an older version
        await conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) UNIQUE, "
            "password_hash VARCHAR(255), role VARCHAR(16), totp_secret_enc VARCHAR(255), "
            "totp_enabled BOOLEAN, totp_last_step INTEGER, disabled BOOLEAN, "
            "must_change_password BOOLEAN, failed_logins INTEGER, locked_until DATETIME, "
            "created_at DATETIME, last_login_at DATETIME)")
        await conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) "
                                   "VALUES ('alt', 'x', 'admin')")
    await db.create_all()
    async with db.engine.connect() as conn:
        row = (await conn.execute(text("SELECT username, auth_source, oidc_sub FROM users"))
               ).one()
    assert tuple(row) == ("alt", "local", None)
    await db.dispose()


# --- whoami -----------------------------------------------------------------------------------


async def test_whoami_for_session_and_key(env):
    app, api = env
    me = (await api.get("/api/v1/auth/whoami")).json()
    assert me == {"name": "admin", "role": "admin", "kind": "session",
                  "scopes": ["read", "control", "admin"]}
    key = (await api.post("/api/v1/admin/api-keys",
                          json={"name": "mcp", "scopes": ["control"]})).json()["secret"]
    async with client_for(app) as c:
        r = (await c.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {key}"})).json()
    assert r == {"name": "apikey:mcp", "role": "operator", "kind": "apikey",
                 "scopes": ["read", "control"]}


# --- guest updates ----------------------------------------------------------------------------


async def test_guest_updates_disabled_by_default(env):
    _, api = env
    r = await api.get("/api/v1/clusters/homelab/guests/108/os-updates")
    assert r.status_code == 409 and "GUEST_UPDATES" in r.json()["detail"]


async def test_guest_updates_vm_and_lxc(settings):
    settings.read_only = False
    settings.guest_updates = True
    clock = AutoClock(step=2)
    mocks = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    app = create_app(settings, clients=mocks, ssh_runner=MockSshRunner(mocks, line_delay=0))
    async with app.router.lifespan_context(app):
        app.state.nodes.poll_sleep = 0.01
        async with logged_in(app) as api:
            pending = mocks["homelab"].actions.guest_apt[108]
            chk = (await api.get("/api/v1/clusters/homelab/guests/108/os-updates")).json()
            assert chk["os"] == "debian" and chk["updates"] == pending
            assert (await api.post("/api/v1/clusters/homelab/guests/108/os-upgrade",
                                   json={"confirm": "x"})).status_code == 422
            job = (await api.post("/api/v1/clusters/homelab/guests/108/os-upgrade",
                                  json={"confirm": "docker-host"})).json()["job_id"]
            for _ in range(500):
                j = (await api.get(f"/api/v1/jobs/{job}")).json()
                if j["status"] != "running":
                    break
                await asyncio.sleep(0.01)
            assert j["status"] == "ok", (await api.get(f"/api/v1/jobs/{job}/log")).json()
            assert mocks["homelab"].actions.guest_apt[108] == 0

            # LXC via the node helper - needs a trusted SSH host key on the node
            r = await api.get("/api/v1/clusters/homelab/guests/208/os-updates")
            assert r.status_code == 409 and "Host-Key" in r.json()["detail"]
            scan = (await api.post("/api/v1/admin/ssh/scan",
                                   json={"cluster": "homelab", "node": "pve1"})).json()
            await api.post("/api/v1/admin/ssh/trust", json={"cluster": "homelab", "node": "pve1",
                                                            "key": scan["key"]})
            chk = (await api.get("/api/v1/clusters/homelab/guests/208/os-updates")).json()
            assert chk["updates"] == mocks["homelab"].actions.guest_apt[208]
            # Windows VM is refused
            r = await api.get("/api/v1/clusters/homelab/guests/101/os-updates")
            assert r.status_code == 409 and "Debian/Ubuntu" in r.json()["detail"]


def test_ssh_command_validation():
    from app.ssh import valid_command

    assert valid_command("upgrade") and valid_command("lxc-upgrade 205")
    for bad in ("lxc-upgrade", "lxc-upgrade 2x", "lxc-upgrade 205; rm -rf /",
                "upgrade now", "bash", "lxc-check 5"):
        assert not valid_command(bad), bad


# --- OIDC -------------------------------------------------------------------------------------


class FakeIdp:
    """Minimal OpenID provider: discovery, JWKS and a token endpoint issuing signed tokens."""

    def __init__(self, groups: list[str], sub: str = "u-1", username: str = "anna"):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.groups, self.sub, self.username = groups, sub, username
        self.nonce: str | None = None
        self.token_requests: list[httpx.Request] = []

    def jwks(self) -> dict:
        jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}

    def id_token(self, **overrides) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "dash", "sub": self.sub, "iat": now, "exp": now + 300,
                  "nonce": self.nonce, "preferred_username": self.username,
                  "groups": self.groups, **overrides}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def mount(self, router: respx.MockRouter, **token_overrides) -> None:
        router.get(f"{ISSUER}.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}authorize",
                "token_endpoint": f"{ISSUER}token", "jwks_uri": f"{ISSUER}jwks"}))
        router.get(f"{ISSUER}jwks").mock(return_value=httpx.Response(200, json=self.jwks()))

        def token(request: httpx.Request) -> httpx.Response:
            self.token_requests.append(request)
            return httpx.Response(200, json={"id_token": self.id_token(**token_overrides),
                                             "access_token": "at", "token_type": "Bearer"})

        router.post(f"{ISSUER}token").mock(side_effect=token)


def _oidc_settings(settings):
    settings.oidc_issuer = ISSUER
    settings.oidc_client_id = "dash"
    settings.oidc_client_secret = SecretStr("s3")
    settings.oidc_admin_groups = "pve-admins"
    settings.oidc_viewer_groups = "pve-viewers"
    settings.dashboard_url = "https://dash.lan"
    settings.totp_required_roles = "admin,operator"
    return settings


async def _sso(app, idp, router, expect_ok=True):
    async with client_for(app) as c:
        r = await c.get("/api/v1/auth/oidc/login", params={"next": "/nodes"})
        assert r.status_code == 302
        auth_url = urlparse(r.headers["location"])
        q = parse_qs(auth_url.query)
        assert auth_url.path == "/app/authorize" and q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == ["https://dash.lan/api/v1/auth/oidc/callback"]
        idp.nonce = q["nonce"][0]
        state = q["state"][0]
        r = await c.get("/api/v1/auth/oidc/callback", params={"code": "c0de", "state": state})
        assert r.status_code == 302
        if expect_ok:
            assert r.headers["location"] == "/nodes", r.headers["location"]
            me = (await c.get("/api/v1/auth/me")).json()
            return me, r
        return None, r


async def test_oidc_login_admin_without_totp(settings, clock):
    _oidc_settings(settings)
    idp = FakeIdp(["pve-admins"])
    with respx.mock(assert_all_called=False) as router:
        idp.mount(router)
        mocks = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
        app = create_app(settings, clients=mocks,
                         oidc_transport=httpx.MockTransport(router.handler))
        async with app.router.lifespan_context(app):
            async with client_for(app) as c:
                prov = (await c.get("/api/v1/auth/providers")).json()
                assert prov["oidc"] is True
            me, _ = await _sso(app, idp, router)
            assert me["username"] == "anna" and me["role"] == "admin"
            assert me["auth_source"] == "oidc" and me["pending"] is None  # IdP does MFA
            # PKCE verifier + client secret were sent to the token endpoint
            body = parse_qs(idp.token_requests[0].content.decode())
            assert len(body["code_verifier"][0]) >= 43
            assert idp.token_requests[0].headers["Authorization"].startswith("Basic ")
            # OIDC users cannot use the local password login
            async with client_for(app) as c:
                assert (await c.post("/api/v1/auth/login", json={
                    "username": "anna", "password": "anything-long"})).status_code == 401
            # Group change at the IdP downgrades the role on the next login
            idp.groups = ["pve-viewers"]
            me, _ = await _sso(app, idp, router)
            assert me["role"] == "viewer"


async def test_oidc_rejects_bad_tokens_and_unknown_groups(settings, clock):
    _oidc_settings(settings)
    idp = FakeIdp(["somebody"])
    with respx.mock(assert_all_called=False) as router:
        idp.mount(router)
        mocks = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
        app = create_app(settings, clients=mocks,
                         oidc_transport=httpx.MockTransport(router.handler))
        async with app.router.lifespan_context(app):
            _, r = await _sso(app, idp, router, expect_ok=False)
            assert "keine passende Gruppe" in parse_qs(urlparse(r.headers["location"]).query)[
                "error"][0]

    idp2 = FakeIdp(["pve-admins"])
    with respx.mock(assert_all_called=False) as router:
        idp2.mount(router, aud="other-client")  # token for another application
        app = create_app(settings, clients={c.name: MockPveClient(c.name, clock=clock)
                                            for c in mock_clusters()},
                         oidc_transport=httpx.MockTransport(router.handler))
        async with app.router.lifespan_context(app):
            _, r = await _sso(app, idp2, router, expect_ok=False)
            assert "ID-Token ungültig" in r.headers["location"] or "audience" in \
                r.headers["location"].lower()


async def test_oidc_state_tampering_and_local_name_clash(settings, clock):
    _oidc_settings(settings)
    idp = FakeIdp(["pve-admins"], username="admin")  # same name as the local admin
    with respx.mock(assert_all_called=False) as router:
        idp.mount(router)
        app = create_app(settings, clients={c.name: MockPveClient(c.name, clock=clock)
                                            for c in mock_clusters()},
                         oidc_transport=httpx.MockTransport(router.handler))
        async with app.router.lifespan_context(app):
            async with client_for(app) as c:
                await c.get("/api/v1/auth/oidc/login")
                r = await c.get("/api/v1/auth/oidc/callback",
                                params={"code": "x", "state": "forged"})
                assert "State" in parse_qs(urlparse(r.headers["location"]).query)["error"][0]
            _, r = await _sso(app, idp, router, expect_ok=False)
            assert "existiert bereits" in parse_qs(urlparse(r.headers["location"]).query)[
                "error"][0]
