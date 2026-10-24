"""API keys, Prometheus, node updates/maintenance/reboot, alerting."""

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.alerts.notify import MASK, Notification, Notifier
from app.api.prometheus import build
from app.collector.models import ClusterSnapshot, NodeView
from app.config import mock_clusters
from app.main import create_app
from app.nodes.service import max_temperature, quorum_after_reboot
from app.pve import MockPveClient
from app.pve.mock_ssh import MockSshRunner
from tests.conftest import client_for, logged_in

N = "/api/v1/clusters/homelab/nodes"


class AutoClock:
    """Advances on every read, so mock tasks (which finish by mock time) complete quickly."""

    def __init__(self, start: float | None = None, step: float = 1.0):
        self.now = time.time() if start is None else start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class Captured:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200)


@pytest.fixture
async def env(settings):
    settings.read_only = False
    clock = AutoClock()
    mocks = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    sent = Captured()
    app = create_app(settings, clients=mocks, ssh_runner=MockSshRunner(mocks, line_delay=0),
                     notifier=Notifier(transport=httpx.MockTransport(sent.handler)))
    async with app.router.lifespan_context(app):
        app.state.nodes.poll_sleep = 0.01
        async with logged_in(app) as api:
            yield app, api, mocks, sent


async def _poll(app):
    for p in app.state.collector.pollers:
        await p.poll_once()


async def _wait_job(app, api, job_id, poll=False, timeout=20):
    """Wait for a job; optionally keep the collector polling (reboot detection)."""
    for _ in range(int(timeout / 0.02)):
        if poll:
            await _poll(app)
        j = (await api.get(f"/api/v1/jobs/{job_id}")).json()
        if j["status"] != "running":
            return j, (await api.get(f"/api/v1/jobs/{job_id}/log")).json()["lines"]
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


async def _trust(api, node="pve1"):
    scan = (await api.post("/api/v1/admin/ssh/scan", json={"cluster": "homelab", "node": node})).json()
    r = await api.post("/api/v1/admin/ssh/trust",
                       json={"cluster": "homelab", "node": node, "key": scan["key"]})
    assert r.status_code == 200


# --- API keys ---------------------------------------------------------------------------------


async def test_api_keys_scopes_and_revocation(env):
    app, api, _, _ = env
    r = await api.post("/api/v1/admin/api-keys", json={"name": "ha", "scopes": ["read"]})
    assert r.status_code == 201
    read_key, read_id = r.json()["secret"], r.json()["key"]["id"]
    assert read_key.startswith("pvd_") and r.json()["key"]["prefix"] == read_key[:12]
    ctl_key = (await api.post("/api/v1/admin/api-keys",
                              json={"name": "mcp", "scopes": ["control"]})).json()["secret"]
    assert (await api.post("/api/v1/admin/api-keys",
                           json={"name": "ha", "scopes": ["read"]})).status_code == 422

    async with client_for(app) as tool:
        h = {"Authorization": f"Bearer {read_key}"}
        assert (await tool.get("/api/v1/clusters", headers=h)).status_code == 200
        r = await tool.post("/api/v1/clusters/homelab/guests/100/power", headers=h,
                            json={"action": "reboot"})
        assert r.status_code == 403 and "control" in r.json()["detail"]
        # control key works without CSRF token (not cookie based)
        r = await tool.post("/api/v1/clusters/homelab/guests/100/power",
                            headers={"X-API-Key": ctl_key}, json={"action": "reboot"})
        assert r.status_code == 200
        # keys never manage users or other keys
        assert (await tool.get("/api/v1/admin/users", headers=h)).status_code == 401
        assert (await tool.get("/api/v1/clusters",
                               headers={"Authorization": "Bearer pvd_wrong"})).status_code == 401

        await api.delete(f"/api/v1/admin/api-keys/{read_id}")
        assert (await tool.get("/api/v1/clusters", headers=h)).status_code == 401

    entry = (await api.get("/api/v1/audit", params={"action": "guest.reboot"})).json()[0]
    assert entry["actor"] == "apikey:mcp" and entry["actor_role"] == "operator"
    keys = (await api.get("/api/v1/admin/api-keys")).json()
    assert {k["name"]: k["revoked"] for k in keys} == {"ha": True, "mcp": False}
    assert all("secret" not in k and "key_hash" not in k for k in keys)


# --- Prometheus -------------------------------------------------------------------------------


async def test_metrics_endpoint(env):
    app, api, _, _ = env
    await app.state.nodes.check_all_updates()
    async with client_for(app) as prom:
        assert (await prom.get("/metrics")).status_code == 401
        key = (await api.post("/api/v1/admin/api-keys",
                              json={"name": "prom", "scopes": ["read"]})).json()["secret"]
        r = await prom.get("/metrics", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "# TYPE pve_node_up gauge" in text
    assert 'pve_node_up{cluster="homelab",node="pve1"} 1' in text
    assert 'pve_guest_info{cluster="homelab",node="pve1",vmid="100",name="homeassistant"' in text
    assert 'pve_storage_used_bytes{cluster="homelab",node="shared",storage="pbs"' in text
    assert 'pve_alerts_firing{severity="critical"}' in text
    assert 'pve_node_updates_pending{cluster="homelab",node="pve1"} 7' in text


async def test_metrics_public_setting(settings):
    settings.metrics_public = True
    mocks = {c.name: MockPveClient(c.name) for c in mock_clusters()}
    app = create_app(settings, clients=mocks)
    async with app.router.lifespan_context(app), client_for(app) as c:
        assert (await c.get("/metrics")).status_code == 200


def test_prometheus_escaping():
    snap = ClusterSnapshot(name='we"ird', reachable=True,
                           nodes=[NodeView(cluster="x", node="n\\1", status="offline")])
    text = build([snap], set(), {}, {})
    assert 'pve_cluster_up{cluster="we\\"ird"} 1' in text
    assert 'node="n\\\\1"' in text


# --- updates & upgrade ------------------------------------------------------------------------


async def test_updates_list_and_changelog(env):
    app, api, _, _ = env
    upd = (await api.get(f"{N}/pve1/updates")).json()
    assert len(upd["packages"]) == 7 and upd["error"] is None
    assert {"package", "old_version", "new_version"} <= set(upd["packages"][0])
    cl = (await api.get(f"{N}/pve1/updates/changelog", params={"package": "pve-manager"})).json()
    assert "pve-manager" in cl["changelog"]
    bad = await api.get(f"{N}/pve1/updates/changelog", params={"package": "x; rm -rf /"})
    assert bad.status_code == 422
    job = (await api.post(f"{N}/pve1/updates/refresh")).json()["job_id"]
    j, lines = await _wait_job(app, api, job)
    assert j["status"] == "ok" and "7 Updates" in j["summary"]


async def test_upgrade_needs_trusted_host_key_and_confirmation(env):
    app, api, mocks, _ = env
    r = await api.post(f"{N}/pve1/upgrade", json={"confirm": "pve1"})
    assert r.status_code == 409 and "Host-Key" in r.json()["detail"]
    await _trust(api)
    assert (await api.post(f"{N}/pve1/upgrade", json={"confirm": "pve2"})).status_code == 422

    job = (await api.post(f"{N}/pve1/upgrade", json={"confirm": "pve1"})).json()["job_id"]
    j, lines = await _wait_job(app, api, job)
    assert j["status"] == "ok" and "Neustart erforderlich" in j["summary"]
    assert any("Setting up pve-manager" in line for line in lines)
    st = next(n for n in (await api.get("/api/v1/node-status")).json() if n["node"] == "pve1")
    assert st["updates"] == 0 and st["reboot_required"] is True
    audit = (await api.get("/api/v1/audit", params={"action": "node.upgrade"})).json()[0]
    assert audit["status"] == "ok" and audit["node"] == "pve1"


async def test_ssh_test_reports_mainboard(env):
    _, api, _, _ = env
    r = (await api.post("/api/v1/admin/ssh/test", json={"cluster": "homelab", "node": "pve3"})).json()
    assert r["ok"] is False and "Host-Key" in r["message"]
    await _trust(api, "pve3")
    r = (await api.post("/api/v1/admin/ssh/test", json={"cluster": "homelab", "node": "pve3"})).json()
    assert r["ok"] and r["mainboard"].startswith("Fujitsu D3802-A1")
    overview = (await api.get("/api/v1/admin/ssh")).json()
    assert overview["available"] and overview["public_key"].startswith("ssh-ed25519")


async def test_viewer_cannot_manage_nodes(env):
    app, api, _, _ = env
    await app.state.auth.create_user("betrachter", "viewer", "Nur-Lesen-2026!")
    async with logged_in(app, "betrachter", "Nur-Lesen-2026!") as v:
        assert (await v.get("/api/v1/node-status")).status_code == 200
        assert (await v.post(f"{N}/pve1/upgrade", json={"confirm": "pve1"})).status_code == 403
        assert (await v.get("/api/v1/admin/ssh")).status_code == 403


# --- maintenance & reboot ---------------------------------------------------------------------


async def test_maintenance_evacuates_and_returns(env):
    app, api, _, _ = env
    plan = (await api.get(f"{N}/pve1/maintenance/plan")).json()
    truenas = next(p for p in plan if p["name"] == "truenas")
    assert truenas["target"] is None and "hostpci0" in truenas["reason"]
    assert all(p["target"] in ("pve2", "pve3") for p in plan if p["name"] != "truenas")

    assert (await api.post(f"{N}/pve1/maintenance",
                           json={"enable": True, "confirm": "x"})).status_code == 422
    job = (await api.post(f"{N}/pve1/maintenance", json={
        "enable": True, "confirm": "pve1", "shutdown_unmigratable": True})).json()["job_id"]
    j, lines = await _wait_job(app, api, job)
    assert j["status"] == "ok", lines
    await _poll(app)
    guests = (await api.get("/api/v1/guests", params={"cluster": "homelab", "node": "pve1"})).json()
    running = [g["name"] for g in guests if g["status"] == "running"]
    assert running == []  # migrated away or (truenas) shut down
    st = next(n for n in (await api.get("/api/v1/node-status")).json() if n["node"] == "pve1")
    assert st["maintenance"] and st["evacuated"]

    # Reboot precheck now clean
    pre = (await api.get(f"{N}/pve1/reboot/precheck")).json()
    assert pre["ok"] and not pre["running_guests"]

    job = (await api.post(f"{N}/pve1/maintenance",
                          json={"enable": False, "return_guests": True})).json()["job_id"]
    j, lines = await _wait_job(app, api, job)
    assert j["status"] == "ok", lines
    await _poll(app)
    back = (await api.get("/api/v1/guests", params={"cluster": "homelab", "node": "pve1"})).json()
    assert "truenas" in [g["name"] for g in back if g["status"] == "running"]
    assert len([g for g in back if g["status"] == "running"]) == len(st["evacuated"])


async def test_reboot_flow(env):
    app, api, mocks, _ = env
    pre = (await api.get(f"{N}/pve2/reboot/precheck")).json()
    assert pre["ok"] and pre["running_guests"] and "2 von 3" in pre["quorum"]
    r = await api.post(f"{N}/pve2/reboot", json={"confirm": "pve2"})
    assert r.status_code == 409 and "Gäste" in r.json()["detail"]
    job = (await api.post(f"{N}/pve2/reboot", json={"confirm": "pve2", "force": True})).json()
    j, lines = await _wait_job(app, api, job["job_id"], poll=True, timeout=30)
    assert j["status"] == "ok", lines
    assert any("offline" in line for line in lines) and any("wieder online" in line for line in lines)


def test_quorum_check():
    nodes = [NodeView(cluster="c", node=f"n{i}", status=s)
             for i, s in enumerate(["online", "online", "offline"])]
    snap = ClusterSnapshot(name="c", is_cluster=True, nodes=nodes)
    ok, msg = quorum_after_reboot(snap, "n0")
    assert not ok and "Nur 1 von 3" in msg
    snap.nodes[2].status = "online"
    assert quorum_after_reboot(snap, "n0")[0]
    assert quorum_after_reboot(ClusterSnapshot(name="s", is_cluster=False), "x")[0]


def test_max_temperature():
    sensors = {"k10temp": {"Tctl": {"temp1_input": 61.5}},
               "nvme": {"Composite": {"temp1_input": 44, "temp1_max": 84.8},
                        "Sensor 2": {"temp3_input": 200}}}  # bogus value ignored
    assert max_temperature(sensors) == 61.5
    assert max_temperature({}) is None


# --- alerting ---------------------------------------------------------------------------------


async def test_alert_lifecycle_with_notifications(env):
    app, api, mocks, sent = env
    await app.state.inventory_collector.refresh()
    r = await api.post("/api/v1/alerts/channels", json={
        "name": "hook", "type": "webhook", "config": {"url": "https://hooks.example/x",
                                                       "secret": "s3cret"}})
    assert r.status_code == 201
    ch = r.json()
    assert ch["config"]["secret"] == MASK
    rules = (await api.get("/api/v1/alerts/rules")).json()
    smart = next(x for x in rules if x["metric"] == "disk_smart_failed")
    await api.put(f"/api/v1/alerts/rules/{smart['id']}", json={**smart, "channel_ids": [ch["id"]]})

    engine = app.state.alerts
    await engine.evaluate()
    firing = (await api.get("/api/v1/alerts")).json()
    by_rule = {e["rule_name"]: e for e in firing}
    assert by_rule["SMART-Fehler"]["target"] == "disk/homelab/pve3//dev/sda"
    assert by_rule["SMART-Fehler"]["severity"] == "critical"
    assert "k3s-master" in by_rule["Backup fehlgeschlagen"]["message"]  # seeded failed vzdump
    # Already firing before the channel was attached: no duplicate on re-evaluation
    await engine.evaluate()
    assert sent.requests == []

    from app.pve import mock_inventory
    disk = mock_inventory.DISKS["pve3"][1]
    disk["health"] = "PASSED"  # disk replaced -> resolves
    try:
        await app.state.inventory_collector.refresh()
        await engine.evaluate()
    finally:
        disk["health"] = "FAILED"
    resolved = (await api.get("/api/v1/alerts", params={"state": "resolved"})).json()
    assert any(e["rule_name"] == "SMART-Fehler" for e in resolved)
    assert [json.loads(r.content)["event"] for r in sent.requests] == ["resolved"]

    await app.state.inventory_collector.refresh()  # fails again -> new firing event
    await engine.evaluate()
    req = sent.requests[-1]
    body = json.loads(req.content)
    assert body["event"] == "firing" and body["title"].startswith("[KRITISCH] SMART-Fehler")
    expected = hmac.new(b"s3cret", req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-Signature-256"] == f"sha256={expected}"
    assert (await engine.firing_counts())["critical"] == 1


async def test_alert_for_duration_and_temperature(env):
    app, api, _, _ = env
    await _trust(api, "pve3")
    await app.state.nodes.read_all_sensors()
    engine = app.state.alerts
    t = [1000.0]
    engine.clock = lambda: t[0]
    await engine.evaluate()
    names = {e["rule_name"] for e in (await api.get("/api/v1/alerts")).json()}
    assert "Temperatur hoch" not in names  # pending (for 120 s)
    t[0] += 121
    await engine.evaluate()
    temp = next(e for e in (await api.get("/api/v1/alerts")).json()
                if e["rule_name"] == "Temperatur hoch")
    assert temp["target"] == "node/homelab/pve3" and "83 °C" in temp["message"]
    ack = await api.post(f"/api/v1/alerts/{temp['id']}/ack")
    assert ack.json()["acknowledged_by"] == "admin"


async def test_disabling_a_rule_resolves_its_alerts(env):
    app, api, _, _ = env
    await app.state.inventory_collector.refresh()
    await app.state.alerts.evaluate()
    rules = (await api.get("/api/v1/alerts/rules")).json()
    smart = next(x for x in rules if x["metric"] == "disk_smart_failed")
    await api.put(f"/api/v1/alerts/rules/{smart['id']}", json={**smart, "enabled": False})
    await app.state.alerts.evaluate()
    assert "SMART-Fehler" not in {e["rule_name"] for e in (await api.get("/api/v1/alerts")).json()}


async def test_rule_and_channel_validation(env):
    _, api, _, _ = env
    base = {"name": "x", "metric": "node_cpu", "threshold": 0.9}
    assert (await api.post("/api/v1/alerts/rules", json={**base, "metric": "nope"})).status_code == 422
    assert (await api.post("/api/v1/alerts/rules",
                           json={**base, "selector": {"evil": 1}})).status_code == 422
    assert (await api.post("/api/v1/alerts/rules", json=base)).status_code == 201
    bad = await api.post("/api/v1/alerts/channels",
                         json={"name": "n", "type": "ntfy", "config": {"url": "ftp://x"}})
    assert bad.status_code == 422
    ch = (await api.post("/api/v1/alerts/channels", json={
        "name": "n", "type": "gotify", "config": {"url": "https://gotify.lan", "token": "tok"}})).json()
    # Updating with the masked token keeps the stored one
    r = await api.put(f"/api/v1/alerts/channels/{ch['id']}", json={
        "name": "n", "type": "gotify", "config": {"url": "https://gotify2.lan", "token": MASK}})
    assert r.json()["config"] == {"url": "https://gotify2.lan", "token": MASK}
    metrics = (await api.get("/api/v1/alerts/metrics")).json()
    assert {"node_temperature", "backup_failed", "disk_wearout"} <= {m["key"] for m in metrics}


async def test_channel_payload_formats():
    sent = Captured()
    n = Notifier(transport=httpx.MockTransport(sent.handler))
    note = Notification(kind="firing", rule="Storage voll", severity="critical",
                        target="storage/homelab/shared/pbs", message="Storage pbs zu 96 %",
                        url="https://dash.lan/alerts")
    await n.send("ntfy", {"url": "https://ntfy.sh", "topic": "pve", "token": "t"}, note)
    await n.send("gotify", {"url": "https://g.lan", "token": "abc"}, note)
    await n.send("homeassistant", {"url": "http://ha.lan:8123", "webhook_id": "pve"}, note)
    ntfy, gotify, ha = sent.requests
    body = json.loads(ntfy.content)
    assert ntfy.url == "https://ntfy.sh" and body["topic"] == "pve" and body["priority"] == 5
    assert body["title"] == "[KRITISCH] Storage voll: storage/homelab/shared/pbs"
    assert ntfy.headers["Authorization"] == "Bearer t"
    assert gotify.url == "https://g.lan/message" and gotify.headers["X-Gotify-Key"] == "abc"
    assert ha.url == "http://ha.lan:8123/api/webhook/pve"
    await n.aclose()
