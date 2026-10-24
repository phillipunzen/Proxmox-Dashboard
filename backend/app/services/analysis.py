"""Load analysis: "Where does the load come from?"

Metrics are compared in absolute units so guests and hosts are directly comparable:
CPU in *host threads in use* (a guest using 50 % of 4 vCPUs = 2 threads), RAM in bytes,
I/O and network in bytes/s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from app.collector.models import ClusterSnapshot, GuestView
from app.services.history import Window, selector
from app.tsdb.client import Series, TsdbClient

AnalysisMetric = Literal["cpu", "mem", "diskread", "diskwrite", "netin", "netout"]


@dataclass(frozen=True)
class MetricDef:
    guest: str
    unit: Literal["cores", "bytes", "bps"]
    label: str
    # Host total: a node metric, or None = sum of all guests (no host-level series exists).
    node_total: str | None
    node_capacity: str | None


METRICS: dict[str, MetricDef] = {
    "cpu": MetricDef("pve_guest_cpu_cores", "cores", "CPU",
                     "pve_node_cpu_cores_used", "pve_node_cpus"),
    "mem": MetricDef("pve_guest_mem_used_bytes", "bytes", "RAM",
                     "pve_node_mem_used_bytes", "pve_node_mem_total_bytes"),
    "diskread": MetricDef("pve_guest_diskread_bps", "bps", "Disk lesen", None, None),
    "diskwrite": MetricDef("pve_guest_diskwrite_bps", "bps", "Disk schreiben", None, None),
    "netin": MetricDef("pve_guest_netin_bps", "bps", "Netz eingehend", None, None),
    "netout": MetricDef("pve_guest_netout_bps", "bps", "Netz ausgehend", None, None),
}


class GuestFilter(BaseModel):
    type: Literal["qemu", "lxc"] | None = None
    tags: list[str] = []
    pool: str | None = None
    status: str | None = None
    q: str | None = None

    @property
    def needs_snapshot(self) -> bool:
        return bool(self.tags or self.pool or self.status or self.q)

    def matches(self, g: GuestView) -> bool:
        if self.type and g.type != self.type:
            return False
        if self.tags and not set(self.tags).issubset(g.tags):
            return False
        if self.pool and g.pool != self.pool:
            return False
        if self.status and g.status != self.status:
            return False
        if self.q:
            needle = self.q.lower()
            if needle not in g.name.lower() and needle not in str(g.vmid):
                return False
        return True


def vmid_regex(snaps: list[ClusterSnapshot], flt: GuestFilter) -> str | None:
    """Restrict queries to guests matching snapshot-only attributes (tags, pool, ...).

    Returns None when no such filter is active, and ``"^$"`` (match nothing) when the
    filter excludes every guest.
    """
    if not flt.needs_snapshot:
        return None
    ids = sorted({g.vmid for s in snaps for g in s.guests if flt.matches(g)})
    return "|".join(str(i) for i in ids) if ids else "^$"


# --- response models ----------------------------------------------------------------------------


class GuestSeries(BaseModel):
    vmid: int
    name: str
    type: str
    cluster: str | None = None
    avg: float
    max: float
    points: list[list[float]]


class Contribution(BaseModel):
    metric: str
    unit: str
    start: float
    end: float
    step: int
    total: list[list[float]]
    capacity: list[list[float]] | None
    guests: list[GuestSeries]
    other: list[list[float]]
    other_count: int


class RankedGuest(BaseModel):
    cluster: str
    vmid: int
    name: str
    type: str
    node: str | None = None
    value: float
    share_of_total: float | None = None
    share_of_capacity: float | None = None


class Ranking(BaseModel):
    metric: str
    unit: str
    at: float
    window: int
    total: float | None
    capacity: float | None
    guests: list[RankedGuest]


class TopGuest(BaseModel):
    cluster: str
    vmid: int
    name: str
    type: str
    node: str | None = None
    avg: float
    p95: float
    max: float


# --- pure helpers (unit-tested) -----------------------------------------------------------------


def split_top(series: list[Series], top: int) -> tuple[list[GuestSeries], list[list[float]], int]:
    """Rank guest series by average, keep ``top``, sum the rest into one "other" series."""
    ranked: list[GuestSeries] = []
    for s in series:
        values = [v for _, v in s.points]
        if not values:
            continue
        ranked.append(GuestSeries(
            vmid=int(s.labels.get("vmid", 0)), name=s.labels.get("name", "?"),
            type=s.labels.get("type", "?"), cluster=s.labels.get("cluster"),
            avg=sum(values) / len(values), max=max(values),
            points=[[t, v] for t, v in s.points],
        ))
    ranked.sort(key=lambda g: g.avg, reverse=True)
    keep, rest = ranked[:top], ranked[top:]
    other: dict[float, float] = {}
    for g in rest:
        for t, v in g.points:
            other[t] = other.get(t, 0.0) + v
    return keep, [[t, other[t]] for t in sorted(other)], len(rest)


def rank_instant(series: list[Series], total: float | None,
                 capacity: float | None) -> list[RankedGuest]:
    out = []
    for s in series:
        value = s.points[0][1]
        out.append(RankedGuest(
            cluster=s.labels.get("cluster", ""), vmid=int(s.labels.get("vmid", 0)),
            name=s.labels.get("name", "?"), type=s.labels.get("type", "?"),
            node=s.labels.get("node"), value=value,
            share_of_total=value / total if total else None,
            share_of_capacity=value / capacity if capacity else None,
        ))
    out.sort(key=lambda g: g.value, reverse=True)
    return out


# --- queries ------------------------------------------------------------------------------------


def _guest_sel(m: MetricDef, cluster: str | None, node: str | None, flt: GuestFilter,
               vmids: str | None) -> str:
    return selector(m.guest, cluster=cluster, node=node, type=flt.type, vmid__re=vmids)


async def contribution(tsdb: TsdbClient, metric: str, cluster: str, node: str | None,
                       w: Window, flt: GuestFilter, vmids: str | None, top: int) -> Contribution:
    m = METRICS[metric]
    roll = w.rollup
    guest_q = (f"sum by (cluster,vmid,name,type) "
               f"(avg_over_time({_guest_sel(m, cluster, node, flt, vmids)}[{roll}]))")
    # Host total: node metric if one exists, otherwise all guests (unfiltered).
    total_sel = selector(m.node_total or m.guest, cluster=cluster, node=node)
    total_q = f"sum(avg_over_time({total_sel}[{roll}]))"
    cap_q = (f"sum(avg_over_time({selector(m.node_capacity, cluster=cluster, node=node)}[{roll}]))"
             if m.node_capacity else None)

    guests = await tsdb.query_range(guest_q, w.start, w.end, w.step)
    total = await tsdb.query_range(total_q, w.start, w.end, w.step)
    cap = await tsdb.query_range(cap_q, w.start, w.end, w.step) if cap_q else []
    keep, other, other_count = split_top(guests, top)
    return Contribution(
        metric=metric, unit=m.unit, start=w.start, end=w.end, step=w.step,
        total=[[t, v] for t, v in total[0].points] if total else [],
        capacity=[[t, v] for t, v in cap[0].points] if cap else (None if not cap_q else []),
        guests=keep, other=other, other_count=other_count,
    )


async def ranking_at(tsdb: TsdbClient, metric: str, cluster: str, node: str | None, at: float,
                     window: int, flt: GuestFilter, vmids: str | None) -> Ranking:
    """Which guests were responsible at time ``at`` (averaged over ``window`` seconds)?"""
    m = METRICS[metric]
    win = f"{max(window, 20)}s"
    # Chart points come from query_range: the value at T covers (T - window, T], so
    # querying at the clicked timestamp with the same window reproduces that bucket.
    t = at
    guests = await tsdb.query(
        f"sum by (cluster,vmid,name,type,node) "
        f"(avg_over_time({_guest_sel(m, cluster, node, flt, vmids)}[{win}]))", t)
    total_metric = m.node_total or m.guest
    total = await tsdb.query(
        f"sum(avg_over_time({selector(total_metric, cluster=cluster, node=node)}[{win}]))", t)
    cap = (await tsdb.query(
        f"sum(avg_over_time({selector(m.node_capacity, cluster=cluster, node=node)}[{win}]))", t)
        if m.node_capacity else [])
    total_v = total[0].points[0][1] if total else None
    cap_v = cap[0].points[0][1] if cap else None
    return Ranking(metric=metric, unit=m.unit, at=at, window=window, total=total_v,
                   capacity=cap_v, guests=rank_instant(guests, total_v, cap_v))


async def top_guests(tsdb: TsdbClient, metric: str, cluster: str | None, node: str | None,
                     w: Window, flt: GuestFilter, vmids: str | None,
                     limit: int) -> list[TopGuest]:
    """Average, p95 and maximum per guest over the whole window.

    Grouping drops the node label so a migrated guest is one entry; ``max by`` takes the
    per-node series with the higher value (good enough for ranking).
    """
    m = METRICS[metric]
    sel = _guest_sel(m, cluster, node, flt, vmids)
    by = "max by (cluster,vmid,name,type)"
    span = w.span
    avg = await tsdb.query(f"{by} (avg_over_time({sel}[{span}]))", w.end)
    p95 = await tsdb.query(f"{by} (quantile_over_time(0.95, {sel}[{span}]))", w.end)
    mx = await tsdb.query(f"{by} (max_over_time({sel}[{span}]))", w.end)

    def key(s: Series) -> tuple[str, str]:
        return s.labels.get("cluster", ""), s.labels.get("vmid", "")

    p95_map = {key(s): s.points[0][1] for s in p95}
    max_map = {key(s): s.points[0][1] for s in mx}
    out = [TopGuest(cluster=s.labels.get("cluster", ""), vmid=int(s.labels.get("vmid", 0)),
                    name=s.labels.get("name", "?"), type=s.labels.get("type", "?"),
                    avg=s.points[0][1], p95=p95_map.get(key(s), 0.0),
                    max=max_map.get(key(s), 0.0)) for s in avg]
    out.sort(key=lambda g: g.avg, reverse=True)
    return out[:limit]


# --- overcommitment (snapshot only) ---------------------------------------------------------


class NodeAllocation(BaseModel):
    cluster: str
    node: str
    online: bool
    threads: int
    mem_total: int
    vcpus_running: int
    vcpus_configured: int
    mem_running: int
    mem_configured: int
    cpu_ratio_running: float | None
    cpu_ratio_configured: float | None
    mem_ratio_running: float | None
    mem_ratio_configured: float | None


def allocations(snaps: list[ClusterSnapshot]) -> list[NodeAllocation]:
    """Allocated vCPU/RAM vs. physical capacity per node.

    *running* counts only running guests (actual overcommit right now), *configured* also
    stopped guests (what happens if everything starts, e.g. after a failover).
    """
    out = []
    for s in snaps:
        for n in s.nodes:
            guests = [g for g in s.guests if g.node == n.node and not g.template]
            run = [g for g in guests if g.status == "running"]
            vr, vc = sum(g.maxcpu for g in run), sum(g.maxcpu for g in guests)
            mr, mc = sum(g.maxmem for g in run), sum(g.maxmem for g in guests)
            out.append(NodeAllocation(
                cluster=s.name, node=n.node, online=n.status == "online",
                threads=n.maxcpu, mem_total=n.maxmem,
                vcpus_running=vr, vcpus_configured=vc, mem_running=mr, mem_configured=mc,
                cpu_ratio_running=vr / n.maxcpu if n.maxcpu else None,
                cpu_ratio_configured=vc / n.maxcpu if n.maxcpu else None,
                mem_ratio_running=mr / n.maxmem if n.maxmem else None,
                mem_ratio_configured=mc / n.maxmem if n.maxmem else None,
            ))
    return out
