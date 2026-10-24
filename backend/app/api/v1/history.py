"""History (time series) and load analysis endpoints - require VictoriaMetrics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.collector.state import SnapshotStore
from app.services import analysis as an
from app.services.history import RangeKey, Window, resolve_window, selector
from app.tsdb.client import TsdbClient, TsdbError

router = APIRouter(prefix="/api/v1")

RangeQ = Annotated[RangeKey, Query(alias="range", description="Preset range ending now")]
StartQ = Annotated[float | None, Query(description="Unix seconds; overrides range")]
EndQ = Annotated[float | None, Query(description="Unix seconds, default now")]
TagQ = Annotated[list[str] | None, Query(alias="tag", description="All given tags must match")]


def _tsdb(request: Request) -> TsdbClient:
    tsdb = request.app.state.tsdb
    if tsdb is None:
        raise HTTPException(503, "History disabled: VM_URL (VictoriaMetrics) is not configured")
    return tsdb


def _window(request: Request, range_: str, start: float | None, end: float | None) -> Window:
    return resolve_window(range_, end=end, start=start,
                          poll_interval=request.app.state.settings.poll_interval)


def _store(request: Request) -> SnapshotStore:
    return request.app.state.store


class SeriesSet(BaseModel):
    start: float
    end: float
    step: int
    series: dict[str, list[list[float]]]


async def _fetch(tsdb: TsdbClient, w: Window, queries: dict[str, str]) -> SeriesSet:
    out: dict[str, list[list[float]]] = {}
    try:
        for key, q in queries.items():
            res = await tsdb.query_range(q, w.start, w.end, w.step)
            out[key] = [[t, v] for t, v in res[0].points] if res else []
    except TsdbError as exc:
        raise HTTPException(502, str(exc)) from exc
    return SeriesSet(start=w.start, end=w.end, step=w.step, series=out)


def _avg(sel: str, w: Window) -> str:
    return f"avg_over_time({sel}[{w.rollup}])"


# --- history ------------------------------------------------------------------------------------


@router.get("/history/clusters/{cluster}", response_model=SeriesSet, tags=["history"],
            summary="Cluster totals over time: cpu/mem (0..1), net/disk (bytes/s)")
async def cluster_history(request: Request, cluster: str, range_: RangeQ = "1h",
                          start: StartQ = None, end: EndQ = None) -> SeriesSet:
    w = _window(request, range_, start, end)
    s = lambda m: _avg(selector(m, cluster=cluster), w)  # noqa: E731
    return await _fetch(_tsdb(request), w, {
        "cpu": s("pve_cluster_cpu_ratio"),
        "mem": f"{s('pve_cluster_mem_used_bytes')} / {s('pve_cluster_mem_total_bytes')}",
        "netin": s("pve_cluster_netin_bps"),
        "netout": s("pve_cluster_netout_bps"),
        "diskread": s("pve_cluster_diskread_bps"),
        "diskwrite": s("pve_cluster_diskwrite_bps"),
    })


@router.get("/history/clusters/{cluster}/nodes/{node}", response_model=SeriesSet,
            tags=["history"], summary="Node metrics over time")
async def node_history(request: Request, cluster: str, node: str, range_: RangeQ = "1h",
                       start: StartQ = None, end: EndQ = None) -> SeriesSet:
    w = _window(request, range_, start, end)
    s = lambda m: _avg(selector(m, cluster=cluster, node=node), w)  # noqa: E731
    guests = lambda m: f"sum({_avg(selector(m, cluster=cluster, node=node), w)})"  # noqa: E731
    return await _fetch(_tsdb(request), w, {
        "cpu": s("pve_node_cpu_ratio"),
        "mem": f"{s('pve_node_mem_used_bytes')} / {s('pve_node_mem_total_bytes')}",
        "iowait": s("pve_node_iowait_ratio"),
        "load1": s("pve_node_load1"),
        "swap": s("pve_node_swap_used_bytes"),
        "netin": guests("pve_guest_netin_bps"),
        "netout": guests("pve_guest_netout_bps"),
        "diskread": guests("pve_guest_diskread_bps"),
        "diskwrite": guests("pve_guest_diskwrite_bps"),
    })


@router.get("/history/clusters/{cluster}/guests/{vmid}", response_model=SeriesSet,
            tags=["history"], summary="Guest metrics over time (merged across migrations)")
async def guest_history(request: Request, cluster: str, vmid: int, range_: RangeQ = "1h",
                        start: StartQ = None, end: EndQ = None) -> SeriesSet:
    w = _window(request, range_, start, end)
    # sum() merges the per-node series of a migrated guest into one line.
    s = lambda m: f"sum({_avg(selector(m, cluster=cluster, vmid=str(vmid)), w)})"  # noqa: E731
    return await _fetch(_tsdb(request), w, {
        "cpu": s("pve_guest_cpu_ratio"),
        "cpu_cores": s("pve_guest_cpu_cores"),
        "mem": s("pve_guest_mem_used_bytes"),
        "diskread": s("pve_guest_diskread_bps"),
        "diskwrite": s("pve_guest_diskwrite_bps"),
        "netin": s("pve_guest_netin_bps"),
        "netout": s("pve_guest_netout_bps"),
    })


class StorageSeries(BaseModel):
    cluster: str
    node: str
    storage: str
    type: str
    used: list[list[float]]
    total: float | None


@router.get("/history/storage", response_model=list[StorageSeries], tags=["history"],
            summary="Storage usage over time (shared storages once, node='shared')")
async def storage_history(request: Request, cluster: str | None = None, range_: RangeQ = "7d",
                          start: StartQ = None, end: EndQ = None) -> list[StorageSeries]:
    tsdb = _tsdb(request)
    w = _window(request, range_, start, end)
    try:
        used = await tsdb.query_range(
            _avg(selector("pve_storage_used_bytes", cluster=cluster), w), w.start, w.end, w.step)
        total_sel = selector("pve_storage_total_bytes", cluster=cluster)
        total = await tsdb.query(f"last_over_time({total_sel}[1h])", w.end)
    except TsdbError as exc:
        raise HTTPException(502, str(exc)) from exc
    key = lambda lbl: (lbl.get("cluster"), lbl.get("node"), lbl.get("storage"))  # noqa: E731
    totals = {key(s.labels): s.points[0][1] for s in total}
    return [StorageSeries(cluster=s.labels.get("cluster", ""), node=s.labels.get("node", ""),
                          storage=s.labels.get("storage", ""), type=s.labels.get("type", ""),
                          used=[[t, v] for t, v in s.points], total=totals.get(key(s.labels)))
            for s in used]


# --- load analysis ------------------------------------------------------------------------------


def _filter(type_: str | None, tag: list[str] | None, pool: str | None, status: str | None,
            q: str | None) -> an.GuestFilter:
    return an.GuestFilter(type=type_, tags=tag or [], pool=pool, status=status, q=q)


def _vmids(request: Request, cluster: str | None, flt: an.GuestFilter) -> str | None:
    store = _store(request)
    snaps = [s for s in store.all() if cluster is None or s.name == cluster]
    return an.vmid_regex(snaps, flt)


TypeQ = Annotated[str | None, Query(alias="type", pattern="^(qemu|lxc)$")]


@router.get("/analysis/contribution", response_model=an.Contribution, tags=["analysis"],
            summary="Host load over time, split into the top-N guests + rest")
async def contribution(
    request: Request, cluster: str, metric: an.AnalysisMetric = "cpu",
    node: str | None = None, range_: RangeQ = "24h", start: StartQ = None, end: EndQ = None,
    top: Annotated[int, Query(ge=1, le=30)] = 8,
    type_: TypeQ = None, tag: TagQ = None, pool: str | None = None,
    status: str | None = None, q: str | None = None,
) -> an.Contribution:
    flt = _filter(type_, tag, pool, status, q)
    w = _window(request, range_, start, end)
    try:
        return await an.contribution(_tsdb(request), metric, cluster, node, w, flt,
                                     _vmids(request, cluster, flt), top)
    except TsdbError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/analysis/at", response_model=an.Ranking, tags=["analysis"],
            summary="Drill-down: which guests caused the load at a point in time")
async def ranking_at(
    request: Request, cluster: str, at: float, metric: an.AnalysisMetric = "cpu",
    node: str | None = None,
    window: Annotated[int, Query(ge=10, le=86400, description="Seconds, the chart step")] = 300,
    type_: TypeQ = None, tag: TagQ = None, pool: str | None = None,
    status: str | None = None, q: str | None = None,
) -> an.Ranking:
    flt = _filter(type_, tag, pool, status, q)
    try:
        return await an.ranking_at(_tsdb(request), metric, cluster, node, at, window, flt,
                                   _vmids(request, cluster, flt))
    except TsdbError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/analysis/top", response_model=list[an.TopGuest], tags=["analysis"],
            summary="Top consumers over a range (avg, p95, max)")
async def top(
    request: Request, metric: an.AnalysisMetric = "cpu", cluster: str | None = None,
    node: str | None = None, range_: RangeQ = "24h", start: StartQ = None, end: EndQ = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
    type_: TypeQ = None, tag: TagQ = None, pool: str | None = None,
    status: str | None = None, q: str | None = None,
) -> list[an.TopGuest]:
    flt = _filter(type_, tag, pool, status, q)
    w = _window(request, range_, start, end)
    try:
        result = await an.top_guests(_tsdb(request), metric, cluster, node, w, flt,
                                     _vmids(request, cluster, flt), limit)
    except TsdbError as exc:
        raise HTTPException(502, str(exc)) from exc
    # Attach the current node from the live snapshot.
    where = {(g.cluster, g.vmid): g.node for s in _store(request).all() for g in s.guests}
    for g in result:
        g.node = where.get((g.cluster, g.vmid))
    return result


@router.get("/analysis/allocation", response_model=list[an.NodeAllocation], tags=["analysis"],
            summary="Overcommitment: allocated vCPU/RAM vs. physical per node")
async def allocation(request: Request, cluster: str | None = None) -> list[an.NodeAllocation]:
    snaps = [s for s in _store(request).all() if cluster is None or s.name == cluster]
    return an.allocations(snaps)
