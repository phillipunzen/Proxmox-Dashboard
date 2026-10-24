import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from pve_dashboard_mcp.client import DashboardClient
from pve_dashboard_mcp.server import BearerAuth, build_server, parse_time

URL = "http://dash.test"
GUESTS = [
    {"vmid": 100, "name": "homeassistant", "type": "qemu", "node": "pve1", "cluster": "homelab",
     "status": "running", "cpu": 0.07, "cpu_host": 0.004, "maxcpu": 1, "mem": 2 * 1024**3,
     "maxmem": 4 * 1024**3, "diskread_bps": 2e5, "diskwrite_bps": 1e5, "netin_bps": 5e4,
     "netout_bps": 4e4, "tags": ["prod"], "console_url": "https://pve/..."},
    {"vmid": 109, "name": "gitlab", "type": "qemu", "node": "pve1", "cluster": "homelab",
     "status": "running", "cpu": 0.45, "cpu_host": 0.11, "maxcpu": 4, "mem": 4 * 1024**3,
     "maxmem": 8 * 1024**3, "tags": []},
    {"vmid": 209, "name": "gitlab-runner", "type": "lxc", "node": "pve2", "cluster": "homelab",
     "status": "stopped", "cpu": 0, "cpu_host": 0, "maxcpu": 2, "mem": 0, "maxmem": 2 * 1024**3},
]


def client() -> DashboardClient:
    return DashboardClient(URL, "pvd_test")


async def call(server, name, args=None):
    """(is_error, payload) - in-process the SDK raises ToolError; over the wire it becomes
    an isError result with the same message."""
    try:
        res = await server.call_tool(name, args or {})
    except ToolError as exc:
        return True, str(exc)
    if res.structured_content and "result" in res.structured_content:
        return res.is_error, res.structured_content["result"]
    return res.is_error, json.loads(res.content[0].text)


async def tool_names(server):
    return {t.name for t in await server.list_tools()}


async def test_read_only_key_gets_no_control_tools():
    names = await tool_names(build_server(client(), ["read"]))
    assert {"cluster_overview", "list_guests", "load_at_time", "backup_status"} <= names
    assert not names & {"guest_power", "create_snapshot", "start_backup"}
    control = await tool_names(build_server(client(), ["read", "control"]))
    assert {"guest_power", "create_snapshot", "start_backup", "acknowledge_alert"} <= control
    # Dangerous node-level actions are never exposed via MCP
    assert not any("reboot" in n or "upgrade" in n or "rollback" in n for n in control)


async def test_destructive_annotation():
    tools = {t.name: t for t in await build_server(client(), ["read", "control"]).list_tools()}
    assert tools["guest_power"].annotations.destructive_hint is True
    assert tools["list_guests"].annotations.read_only_hint is True


@respx.mock
async def test_list_guests_compacts_units():
    respx.get(f"{URL}/api/v1/guests").mock(return_value=httpx.Response(200, json=GUESTS[:2]))
    err, rows = await call(build_server(client(), ["read"]), "list_guests", {"limit": 5})
    assert not err
    assert rows[0] == {"vmid": 100, "name": "homeassistant", "type": "qemu", "node": "pve1",
                       "cluster": "homelab", "status": "running", "cpu_pct": 7.0,
                       "host_share_pct": 0.4, "vcpus": 1, "mem_gib": 2.0, "mem_max_gib": 4.0,
                       "disk_read_mbs": 0.2, "disk_write_mbs": 0.1, "net_in_mbs": 0.05,
                       "net_out_mbs": 0.04, "tags": ["prod"]}
    req = respx.calls.last.request
    assert req.url.params["sort"] == "cpu_host" and req.url.params["order"] == "desc"
    assert req.headers["Authorization"] == "Bearer pvd_test"


@respx.mock
async def test_guest_power_resolves_name_and_reports_errors():
    respx.get(f"{URL}/api/v1/guests").mock(return_value=httpx.Response(200, json=GUESTS))
    power = respx.post(f"{URL}/api/v1/clusters/homelab/guests/100/power").mock(
        return_value=httpx.Response(200, json={"upid": "UPID:pve1:1", "dry_run": False}))
    server = build_server(client(), ["read", "control"])
    err, out = await call(server, "guest_power", {"guest": "homeassistant", "action": "shutdown"})
    assert not err and out["task"] == "UPID:pve1:1"
    assert json.loads(power.calls.last.request.content) == {"action": "shutdown", "confirm": None}

    # "gitlab" matches exactly one name; a partial match on several is ambiguous
    err, msg = await call(server, "guest_power", {"guest": "git", "action": "start"})
    assert err and "nicht eindeutig" in msg

    respx.post(f"{URL}/api/v1/clusters/homelab/guests/109/power").mock(
        return_value=httpx.Response(422, json={"detail": "Zur Bestätigung den Gast-Namen"}))
    err, msg = await call(server, "guest_power", {"guest": "109", "action": "stop"})
    assert err and "422" in msg and "Gast-Namen" in msg


@respx.mock
async def test_load_at_time_converts_relative_time():
    respx.get(f"{URL}/api/v1/clusters").mock(return_value=httpx.Response(200, json=[
        {"name": "homelab", "nodes": [{"node": "pve1"}]}]))
    at = respx.get(f"{URL}/api/v1/analysis/at").mock(return_value=httpx.Response(200, json={
        "total": 4.2, "capacity": 16, "guests": [
            {"vmid": 109, "name": "gitlab", "node": "pve1", "value": 1.7, "share_of_total": 0.4},
            {"vmid": 1, "name": "idle", "node": "pve1", "value": 0, "share_of_total": 0}]}))
    err, out = await call(build_server(client(), ["read"]), "load_at_time",
                          {"time": "-2h", "node": "pve1"})
    assert not err and out["cluster"] == "homelab" and len(out["guests"]) == 1
    assert out["guests"][0]["share_pct"] == 40.0
    params = at.calls.last.request.url.params
    assert params["node"] == "pve1" and params["window"] == "300"


def test_parse_time():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert parse_time("-2h", now) == datetime(2026, 10, 24, 10, 0, tzinfo=UTC).timestamp()
    assert parse_time("-30m", now) == datetime(2026, 10, 24, 11, 30, tzinfo=UTC).timestamp()
    assert parse_time("2026-10-24T14:30:00+02:00") == datetime(2026, 10, 24, 12, 30,
                                                               tzinfo=UTC).timestamp()
    with pytest.raises(ToolError):
        parse_time("gestern")


def test_api_key_format_checked():
    with pytest.raises(ValueError):
        DashboardClient(URL, "")


async def test_bearer_middleware():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    transport = httpx.ASGITransport(app=BearerAuth(app, "a" * 32))
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as c:
        assert (await c.post("/mcp")).status_code == 401
        assert (await c.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await c.post("/mcp", headers={"Authorization": "Bearer " + "a" * 32})
        assert ok.status_code == 200
