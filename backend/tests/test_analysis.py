import httpx
import pytest
import respx

from app.collector.models import ClusterSnapshot, GuestView, NodeView
from app.config import mock_clusters
from app.main import create_app
from app.pve import MockPveClient
from app.services.analysis import (
    GuestFilter,
    allocations,
    rank_instant,
    split_top,
    vmid_regex,
)
from app.tsdb.client import Series, TsdbClient

VM = "http://vm:8428"


def _s(vmid, name, values):
    return Series({"vmid": str(vmid), "name": name, "type": "qemu", "cluster": "c"},
                  [(float(i * 10), v) for i, v in enumerate(values)])


def test_split_top_ranks_by_average_and_sums_rest():
    series = [_s(1, "a", [1, 1]), _s(2, "b", [5, 3]), _s(3, "c", [0, 2]), _s(4, "d", [0, 1])]
    keep, other, count = split_top(series, 2)
    assert [g.name for g in keep] == ["b", "a"]
    assert keep[0].avg == 4 and keep[0].max == 5
    assert count == 2
    assert other == [[0.0, 0.0], [10.0, 3.0]]


def test_rank_instant_shares():
    series = [Series({"vmid": "1", "name": "a", "cluster": "c"}, [(0, 2.0)]),
              Series({"vmid": "2", "name": "b", "cluster": "c"}, [(0, 6.0)])]
    ranked = rank_instant(series, total=10.0, capacity=16.0)
    assert [g.name for g in ranked] == ["b", "a"]
    assert ranked[0].share_of_total == 0.6 and ranked[0].share_of_capacity == 0.375
    assert rank_instant(series, None, None)[0].share_of_total is None


def _guest(vmid, node="n1", status="running", tags=(), maxcpu=2, maxmem=1024, template=False,
           pool=None):
    return GuestView(cluster="c", vmid=vmid, name=f"g{vmid}", type="qemu", node=node,
                     status=status, tags=list(tags), maxcpu=maxcpu, maxmem=maxmem,
                     template=template, pool=pool)


def test_vmid_regex_from_snapshot_filters():
    snap = ClusterSnapshot(name="c", guests=[_guest(1, tags=["prod"]), _guest(2, tags=["lab"]),
                                             _guest(3, tags=["prod", "db"], pool="p")])
    assert vmid_regex([snap], GuestFilter()) is None  # no snapshot filter -> no restriction
    assert vmid_regex([snap], GuestFilter(tags=["prod"])) == "1|3"
    assert vmid_regex([snap], GuestFilter(tags=["prod"], pool="p")) == "3"
    assert vmid_regex([snap], GuestFilter(tags=["nope"])) == "^$"


def test_allocations_running_vs_configured():
    snap = ClusterSnapshot(
        name="c",
        nodes=[NodeView(cluster="c", node="n1", status="online", maxcpu=4, maxmem=4096)],
        guests=[_guest(1, maxcpu=4, maxmem=2048), _guest(2, status="stopped", maxcpu=4,
                                                         maxmem=4096),
                _guest(3, maxcpu=8, template=True)],
    )
    a = allocations([snap])[0]
    assert (a.vcpus_running, a.vcpus_configured) == (4, 8)
    assert a.cpu_ratio_configured == 2.0 and a.mem_ratio_running == 0.5


# --- API with a mocked VictoriaMetrics ------------------------------------------------------


@pytest.fixture
async def api_with_vm(settings, clock):
    clients = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    tsdb = TsdbClient(VM)
    app = create_app(settings, clients=clients, tsdb_client=tsdb)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{VM}/api/v1/import/prometheus").mock(return_value=httpx.Response(204))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                yield client, mock


def _matrix(*series):
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix",
                                                                     "result": list(series)}})


def _vector(*series):
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector",
                                                                     "result": list(series)}})


async def test_contribution_endpoint_builds_queries(api_with_vm):
    api, mock = api_with_vm
    seen = []

    def responder(request):
        q = request.url.params["query"]
        seen.append(q)
        if "by (cluster,vmid,name,type)" in q:
            return _matrix(
                {"metric": {"vmid": "109", "name": "gitlab", "type": "qemu", "cluster": "homelab"},
                 "values": [[100, "2"], [200, "3"]]},
                {"metric": {"vmid": "103", "name": "truenas", "type": "qemu",
                            "cluster": "homelab"}, "values": [[100, "1"], [200, "1"]]})
        return _matrix({"metric": {}, "values": [[100, "4"], [200, "5"]]})

    mock.get(f"{VM}/api/v1/query_range").mock(side_effect=responder)
    r = await api.get("/api/v1/analysis/contribution",
                      params={"cluster": "homelab", "node": "pve1", "metric": "cpu",
                              "range": "24h", "top": 1, "tag": "prod"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "cores" and body["step"] == 300
    assert [g["name"] for g in body["guests"]] == ["gitlab"]
    assert body["other_count"] == 1 and body["other"] == [[100, 1], [200, 1]]
    assert body["total"] == [[100, 4], [200, 5]] and body["capacity"] == [[100, 4], [200, 5]]
    guest_q = next(q for q in seen if "by (cluster" in q)
    assert 'node="pve1"' in guest_q and 'vmid=~"' in guest_q and "[300s]" in guest_q
    assert any("pve_node_cpus" in q for q in seen)


async def test_ranking_at_and_top(api_with_vm):
    api, mock = api_with_vm

    def instant(request):
        q = request.url.params["query"]
        if q.startswith("sum by"):
            return _vector({"metric": {"vmid": "109", "name": "gitlab", "type": "qemu",
                                       "cluster": "homelab", "node": "pve1"}, "value": [1, "3"]})
        if "max by" in q:
            return _vector({"metric": {"vmid": "109", "name": "gitlab", "type": "qemu",
                                       "cluster": "homelab"}, "value": [1, "2"]})
        return _vector({"metric": {}, "value": [1, "6"]})

    mock.get(f"{VM}/api/v1/query").mock(side_effect=instant)
    at = (await api.get("/api/v1/analysis/at", params={"cluster": "homelab", "node": "pve1",
                                                       "at": 1000, "window": 300})).json()
    assert at["guests"][0]["share_of_total"] == 0.5 and at["total"] == 6
    top = (await api.get("/api/v1/analysis/top", params={"metric": "netin"})).json()
    assert top[0]["name"] == "gitlab" and top[0]["node"] == "pve1"  # node from live snapshot


async def test_history_without_tsdb_returns_503(settings, clock):
    clients = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    app = create_app(settings, clients=clients)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as api:
            r = await api.get("/api/v1/history/clusters/homelab")
            assert r.status_code == 503
            assert (await api.get("/api/v1/meta")).json()["history"] is False
            # allocation works from the snapshot alone
            alloc = (await api.get("/api/v1/analysis/allocation")).json()
            assert {a["node"] for a in alloc} == {"pve1", "pve2", "pve3", "edge"}


async def test_tsdb_down_gives_502(api_with_vm):
    api, mock = api_with_vm
    mock.get(f"{VM}/api/v1/query_range").mock(side_effect=httpx.ConnectError("down"))
    r = await api.get("/api/v1/history/clusters/homelab", params={"range": "1h"})
    assert r.status_code == 502
