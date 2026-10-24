import pytest

from app.config import mock_clusters
from app.main import create_app
from app.pve import MockPveClient
from tests.conftest import logged_in

G = "/api/v1/clusters/homelab/guests"


@pytest.fixture
def live_settings(settings):
    settings.read_only = False
    return settings


async def _app_ctx(settings, clock):
    clients = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    return create_app(settings, clients=clients)


@pytest.fixture
async def ctx(live_settings, clock):
    app = await _app_ctx(live_settings, clock)
    async with app.router.lifespan_context(app), logged_in(app) as api:
        yield app, api


async def _settle(app, clock, seconds=20):
    """Let mock tasks finish, update the audit log and the live snapshot."""
    clock.advance(seconds)
    await app.state.task_watcher.check_once()
    for p in app.state.collector.pollers:
        await p.poll_once()


async def _audit(api, **params):
    return (await api.get("/api/v1/audit", params=params)).json()


async def test_read_only_blocks_actions(settings, clock):
    app = await _app_ctx(settings, clock)  # read_only=True (default)
    async with app.router.lifespan_context(app), logged_in(app) as api:
        r = await api.post(f"{G}/100/power", json={"action": "shutdown"})
        assert r.status_code == 409 and "READ_ONLY" in r.json()["detail"]
        caps = (await api.get("/api/v1/actions/capabilities")).json()
        assert not any(c["enabled"] for c in caps)


async def test_power_action_tracked_until_done(ctx, clock):
    app, api = ctx
    r = await api.post(f"{G}/100/power", json={"action": "shutdown"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["upid"].startswith("UPID:pve1:") and res["node"] == "pve1"
    entry = (await _audit(api, action="guest.shutdown"))[0]
    assert entry["status"] == "submitted" and entry["vmid"] == 100 and entry["actor"] == "admin"

    await _settle(app, clock)
    entry = (await _audit(api, action="guest.shutdown"))[0]
    assert entry["status"] == "ok" and entry["finished_at"]
    g = (await api.get(f"{G}/100")).json()
    assert g["status"] == "stopped"

    # Shutting down again: Proxmox task fails -> error with the task's exit status
    await api.post(f"{G}/100/power", json={"action": "shutdown"})
    await _settle(app, clock)
    entry = (await _audit(api, action="guest.shutdown"))[0]
    assert entry["status"] == "error" and "not running" in entry["message"]


async def test_hard_stop_needs_guest_name(ctx):
    _, api = ctx
    r = await api.post(f"{G}/101/power", json={"action": "stop"})
    assert r.status_code == 422 and "win11-desk" in r.json()["detail"]
    r = await api.post(f"{G}/101/power", json={"action": "stop", "confirm": "win11"})
    assert r.status_code == 422
    r = await api.post(f"{G}/101/power", json={"action": "stop", "confirm": "win11-desk"})
    assert r.status_code == 200


async def test_dry_run_changes_nothing(live_settings, clock):
    live_settings.dry_run = True
    app = await _app_ctx(live_settings, clock)
    async with app.router.lifespan_context(app), logged_in(app) as api:
        r = await api.post(f"{G}/100/power", json={"action": "shutdown"})
        assert r.json()["dry_run"] and r.json()["upid"] is None
        await _settle(app, clock)
        assert (await api.get(f"{G}/100")).json()["status"] == "running"
        entry = (await _audit(api, action="guest.shutdown"))[0]
        assert entry["dry_run"] and entry["status"] == "ok"


async def test_templates_and_unknown_guests(ctx):
    _, api = ctx
    assert (await api.post(f"{G}/111/power", json={"action": "start"})).status_code == 409
    assert (await api.post(f"{G}/9999/power", json={"action": "start"})).status_code == 404


async def test_snapshot_lifecycle(ctx, clock):
    app, api = ctx
    snaps = (await api.get(f"{G}/100/snapshots")).json()
    assert [s["name"] for s in snaps] == ["vor-update-2026-10", "vor-update-2026-09"]
    assert (await api.post(f"{G}/100/snapshots", json={"name": "1bad name"})).status_code == 422
    r = await api.post(f"{G}/100/snapshots", json={"name": "vor-test", "description": "x",
                                                   "vmstate": True})
    assert r.status_code == 200
    await _settle(app, clock)
    assert "vor-test" in [s["name"] for s in (await api.get(f"{G}/100/snapshots")).json()]

    r = await api.post(f"{G}/100/snapshots/vor-test/rollback", json={"confirm": "falsch"})
    assert r.status_code == 422
    r = await api.post(f"{G}/100/snapshots/vor-test/rollback", json={"confirm": "homeassistant"})
    assert r.status_code == 200
    r = await api.delete(f"{G}/100/snapshots/vor-test", params={"confirm": "homeassistant"})
    assert r.status_code == 200
    await _settle(app, clock)
    assert "vor-test" not in [s["name"] for s in (await api.get(f"{G}/100/snapshots")).json()]
    actions = {e["action"] for e in await _audit(api, action="guest.snapshot")}
    assert actions == {"guest.snapshot.create", "guest.snapshot.rollback",
                       "guest.snapshot.delete"}


async def test_backup(ctx):
    _, api = ctx
    storages = (await api.get(f"{G}/100/backup-storages")).json()
    assert storages == ["local", "nfs-nas", "pbs"]
    assert (await api.post(f"{G}/100/backup", json={"storage": "local-zfs"})).status_code == 422
    r = await api.post(f"{G}/100/backup", json={"storage": "pbs", "mode": "snapshot"})
    assert r.status_code == 200 and ":vzdump:100:" in r.json()["upid"]


async def test_migration(ctx, clock):
    app, api = ctx
    blocked = (await api.get(f"{G}/103/migrate")).json()
    assert blocked["allowed_nodes"] == [] and "hostpci0" in blocked["local_resources"]
    r = await api.post(f"{G}/103/migrate", json={"target": "pve2"})
    assert r.status_code == 502 and "local devices" in r.json()["detail"]

    pre = (await api.get(f"{G}/100/migrate")).json()
    assert set(pre["allowed_nodes"]) == {"pve2", "pve3"} and pre["online"]
    assert (await api.post(f"{G}/100/migrate", json={"target": "pve1"})).status_code == 422
    assert (await api.post(f"{G}/100/migrate", json={"target": "pve2"})).status_code == 200
    await _settle(app, clock)
    assert (await api.get(f"{G}/100")).json()["node"] == "pve2"

    # Running container: restart migration needs the name
    r = await api.post(f"{G}/200/migrate", json={"target": "pve3"})
    assert r.status_code == 422


async def test_bulk_by_tag(ctx, clock):
    app, api = ctx
    guests = (await api.get("/api/v1/guests", params={"cluster": "homelab", "tag": "media"})).json()
    running = [g for g in guests if g["status"] == "running"]
    body = {"cluster": "homelab", "action": "stop", "tag": "media"}
    r = await api.post("/api/v1/actions/bulk", json=body)
    assert r.status_code == 422 and f"stop {len(running)}" in r.json()["detail"]
    r = await api.post("/api/v1/actions/bulk", json={**body, "confirm": f"stop {len(running)}"})
    results = r.json()
    assert len(results) == len(guests)
    assert sum(1 for x in results if x.get("upid")) == len(running)
    assert all(x.get("skipped") for x in results if x["vmid"] not in {g["vmid"] for g in running})
    await _settle(app, clock)
    after = (await api.get("/api/v1/guests", params={"cluster": "homelab", "tag": "media"})).json()
    assert all(g["status"] == "stopped" for g in after)


async def test_tasks_and_logs(ctx, clock):
    app, api = ctx
    r = await api.post(f"{G}/100/power", json={"action": "reboot"})
    upid = r.json()["upid"]
    tasks = (await api.get("/api/v1/tasks", params={"cluster": "homelab"})).json()
    assert tasks[0]["upid"] == upid and tasks[0]["status"] is None  # running
    failed = (await api.get("/api/v1/tasks", params={"errors_only": True})).json()
    assert failed and all(t["status"] not in (None, "OK") for t in failed)
    await _settle(app, clock)
    st = (await api.get(f"/api/v1/tasks/homelab/{upid}/status")).json()
    assert st["status"] == "stopped" and st["exitstatus"] == "OK"
    log = (await api.get(f"/api/v1/tasks/homelab/{upid}/log")).json()
    assert log[-1]["t"] == "TASK OK"
    assert (await api.get("/api/v1/tasks/homelab/../../etc/log")).status_code in (404, 422)
    assert (await api.get("/api/v1/tasks/homelab/UPID:x/log")).status_code == 422


async def test_operator_can_act_viewer_cannot(ctx):
    app, api = ctx
    await app.state.auth.create_user("op", "operator", "Kl4r-und-sicher-2026")
    async with logged_in(app, "op", "Kl4r-und-sicher-2026") as op:
        r = await op.post(f"{G}/100/power", json={"action": "reboot"})
        assert r.status_code == 200
        assert (await op.get("/api/v1/admin/users")).status_code == 403
    entry = (await _audit(api, action="guest.reboot"))[0]
    assert entry["actor"] == "op" and entry["actor_role"] == "operator"
