"""End-to-end tests of the API against the mock cluster (collector included)."""

import asyncio
import json

import pytest

from app.collector.poller import ClusterPoller
from app.config import mock_clusters
from app.main import create_app
from app.pve import MockPveClient
from app.pve.client import PveUnavailable
from tests.conftest import client_for, logged_in


@pytest.fixture
async def app(settings, clock):
    clients = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    application = create_app(settings, clients=clients)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def api(app):
    async with logged_in(app) as client:
        yield client


async def test_meta_and_health(api):
    meta = (await api.get("/api/v1/meta")).json()
    assert meta["mock"] is True and meta["read_only"] is True
    assert meta["clusters"] == ["homelab", "standalone"]
    assert (await api.get("/api/v1/health")).json() == {"status": "ok"}


async def test_data_endpoints_require_login(app):
    async with client_for(app) as anon:
        assert (await anon.get("/api/v1/health")).status_code == 200  # public
        for path in ("/api/v1/meta", "/api/v1/clusters", "/api/v1/guests", "/api/v1/stream",
                     "/api/v1/inventory", "/api/v1/analysis/allocation", "/api/v1/tasks"):
            assert (await anon.get(path)).status_code == 401, path


async def test_clusters_overview_matches_mock_inventory(api):
    clusters = (await api.get("/api/v1/clusters")).json()
    homelab = next(c for c in clusters if c["name"] == "homelab")
    assert "guests" not in homelab and "storages" not in homelab
    assert homelab["is_cluster"] and homelab["quorate"]
    assert len(homelab["nodes"]) == 3
    t = homelab["totals"]
    assert t["vms_running"] + t["vms_stopped"] + t["templates"] == 13
    assert t["lxc_running"] + t["lxc_stopped"] == 20
    standalone = next(c for c in clusters if c["name"] == "standalone")
    assert standalone["is_cluster"] is False


async def test_guest_filters_and_sorting(api):
    lxc = (await api.get("/api/v1/guests", params={"cluster": "homelab", "type": "lxc"})).json()
    assert len(lxc) == 20 and all(g["type"] == "lxc" for g in lxc)

    top = (await api.get("/api/v1/guests",
                         params={"sort": "cpu_host", "order": "desc", "limit": 5})).json()
    values = [g["cpu_host"] for g in top]
    assert len(top) == 5 and values == sorted(values, reverse=True)

    node = (await api.get("/api/v1/guests", params={"node": "pve2", "status": "running"})).json()
    assert node and all(g["node"] == "pve2" and g["status"] == "running" for g in node)

    hits = (await api.get("/api/v1/guests", params={"q": "jelly"})).json()
    assert [g["name"] for g in hits] == ["jellyfin"]

    with_tpl = (await api.get("/api/v1/guests", params={"include_templates": True})).json()
    without = (await api.get("/api/v1/guests")).json()
    assert len(with_tpl) == len(without) + 1


async def test_tag_filter_requires_all_tags(api):
    all_guests = (await api.get("/api/v1/guests")).json()
    tagged = next(g for g in all_guests if len(g["tags"]) >= 2)
    params = [("tag", t) for t in tagged["tags"]]
    res = (await api.get("/api/v1/guests", params=params)).json()
    assert tagged["vmid"] in [g["vmid"] for g in res]
    assert all(set(tagged["tags"]) <= set(g["tags"]) for g in res)


async def test_single_resources_and_404(api):
    g = (await api.get("/api/v1/clusters/homelab/guests/100")).json()
    assert g["name"] == "homeassistant" and g["console_url"].startswith("https://")
    assert (await api.get("/api/v1/clusters/homelab/nodes/pve1")).json()["kernel"]
    assert (await api.get("/api/v1/clusters/nope")).status_code == 404
    assert (await api.get("/api/v1/clusters/homelab/guests/99999")).status_code == 404
    storage = (await api.get("/api/v1/storage", params={"node": "pve1"})).json()
    assert {s["storage"] for s in storage} == {"local", "local-zfs", "nfs-nas", "pbs"}


async def test_security_headers(api):
    r = await api.get("/api/v1/meta")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


async def test_openapi_documents_v1(api):
    spec = (await api.get("/api/openapi.json")).json()
    assert "/api/v1/guests" in spec["paths"] and "/api/v1/stream" in spec["paths"]


async def test_sse_stream_sends_snapshot(app):
    async with logged_in(app) as c:
        cookie = f"pvd_session={c.cookies['pvd_session']}".encode()
    # httpx' ASGITransport buffers whole responses, so drive the ASGI app directly.
    received: list[bytes] = []
    done = asyncio.Event()

    async def receive():
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            received.append(message["body"])
            done.set()

    scope = {"type": "http", "method": "GET", "path": "/api/v1/stream", "raw_path": b"",
             "query_string": b"", "headers": [(b"cookie", cookie)], "http_version": "1.1",
             "scheme": "http",
             "server": ("test", 80), "client": ("test", 1), "root_path": "", "app": app}
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    text = received[0].decode()
    assert text.startswith("event: snapshot")
    payload = json.loads(text.split("data: ", 1)[1])
    assert {c["name"] for c in payload["clusters"]} == {"homelab", "standalone"}


async def test_poller_keeps_last_data_when_cluster_unreachable(settings, clock):
    from app.collector.state import SnapshotStore

    cfg = mock_clusters()[0]
    client = MockPveClient(cfg.name, clock=clock)
    store = SnapshotStore()
    poller = ClusterPoller(cfg, client, store, settings, clock=clock)
    await poller.poll_once()
    assert store.get("homelab").reachable

    async def broken(*_a, **_k):
        raise PveUnavailable("all down")

    client.get = broken  # type: ignore[method-assign]
    clock.advance(10)
    await poller.poll_once()
    snap = store.get("homelab")
    assert snap.reachable is False and "all down" in snap.last_error
    assert len(snap.guests) == 33  # stale data still visible


async def test_poller_computes_rates_on_second_poll(settings, clock):
    from app.collector.state import SnapshotStore

    cfg = mock_clusters()[0]
    store = SnapshotStore()
    poller = ClusterPoller(cfg, MockPveClient(cfg.name, clock=clock), store, settings, clock=clock)
    await poller.poll_once()
    assert all(g.netin_bps is None for g in store.get("homelab").guests)
    clock.advance(10)
    await poller.poll_once()
    running = [g for g in store.get("homelab").guests if g.status == "running"]
    assert all(g.netin_bps is not None and g.netin_bps > 0 for g in running)
    # Mock rates are in a sane range (< 100 MB/s per guest), not runaway values.
    assert max(g.netin_bps for g in running) < 100e6
