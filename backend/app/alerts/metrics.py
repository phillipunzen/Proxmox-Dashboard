"""Alert metric catalogue: turns current state into (target, value) samples.

Each metric yields ``Sample`` objects for all matching targets. Rules compare the value
against a threshold; selectors (cluster/node/vmid/tag/storage) narrow the targets.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.collector.models import ClusterSnapshot
from app.inventory.models import ClusterInventory


@dataclass
class Sample:
    target: str  # stable key, e.g. node/homelab/pve1
    value: float
    label: str  # human readable target, e.g. "Node pve1 (homelab)"
    attrs: dict[str, Any] = field(default_factory=dict)  # for selectors: cluster/node/vmid/...


@dataclass
class Context:
    snapshots: list[ClusterSnapshot]
    inventory: list[ClusterInventory]
    temps: dict[tuple[str, str], float]
    updates: dict[tuple[str, str], int]
    reboot_required: dict[tuple[str, str], bool]
    # (cluster, "<vmid>" or "node:<node>" for multi-guest jobs) -> error text
    failed_backups: dict[tuple[str, str], str]
    backups: list[Any] = field(default_factory=list)  # GuestBackups
    forecasts: list[Any] = field(default_factory=list)  # StorageForecast


class MetricInfo(BaseModel):
    key: str
    label: str
    unit: str  # ratio | celsius | count | bool | percent
    kind: str  # cluster | node | guest | storage | disk
    default_operator: str
    default_threshold: float


Extractor = Callable[[Context], Iterator[Sample]]
REGISTRY: dict[str, tuple[MetricInfo, Extractor]] = {}


def metric(key: str, label: str, unit: str, kind: str, op: str = ">=",
           threshold: float = 1) -> Callable[[Extractor], Extractor]:
    def deco(fn: Extractor) -> Extractor:
        REGISTRY[key] = (MetricInfo(key=key, label=label, unit=unit, kind=kind,
                                    default_operator=op, default_threshold=threshold), fn)
        return fn
    return deco


def _reachable(ctx: Context) -> Iterator[ClusterSnapshot]:
    # Stale data of an unreachable cluster must neither fire nor resolve node/guest alerts.
    return (s for s in ctx.snapshots if s.reachable)


def _node_attrs(c: str, n: str) -> dict[str, Any]:
    return {"cluster": c, "node": n}


# --- cluster ---------------------------------------------------------------------------------


@metric("cluster_unreachable", "Cluster nicht erreichbar", "bool", "cluster")
def _cluster_unreachable(ctx: Context) -> Iterator[Sample]:
    for s in ctx.snapshots:
        yield Sample(f"cluster/{s.name}", 0 if s.reachable else 1, f"Cluster {s.name}",
                     {"cluster": s.name})


@metric("cluster_no_quorum", "Cluster ohne Quorum", "bool", "cluster")
def _cluster_no_quorum(ctx: Context) -> Iterator[Sample]:
    for s in _reachable(ctx):
        if s.is_cluster:
            yield Sample(f"cluster/{s.name}", 0 if s.quorate else 1, f"Cluster {s.name}",
                         {"cluster": s.name})


# --- nodes -----------------------------------------------------------------------------------


@metric("node_offline", "Node offline", "bool", "node")
def _node_offline(ctx: Context) -> Iterator[Sample]:
    for s in _reachable(ctx):
        for n in s.nodes:
            yield Sample(f"node/{s.name}/{n.node}", 0 if n.status == "online" else 1,
                         f"Node {n.node}", _node_attrs(s.name, n.node))


def _node_ratio(ctx: Context, fn: Callable[[Any], float | None]) -> Iterator[Sample]:
    for s in _reachable(ctx):
        for n in s.nodes:
            if n.status != "online":
                continue
            v = fn(n)
            if v is not None:
                yield Sample(f"node/{s.name}/{n.node}", v, f"Node {n.node}",
                             _node_attrs(s.name, n.node))


@metric("node_cpu", "Node CPU-Last", "ratio", "node", ">", 0.9)
def _node_cpu(ctx: Context) -> Iterator[Sample]:
    return _node_ratio(ctx, lambda n: n.cpu)


@metric("node_mem", "Node RAM-Belegung", "ratio", "node", ">", 0.9)
def _node_mem(ctx: Context) -> Iterator[Sample]:
    return _node_ratio(ctx, lambda n: n.mem / n.maxmem if n.maxmem else None)


@metric("node_swap", "Node Swap-Belegung", "ratio", "node", ">", 0.5)
def _node_swap(ctx: Context) -> Iterator[Sample]:
    return _node_ratio(ctx, lambda n: n.swap_used / n.swap_total if n.swap_total else None)


@metric("node_iowait", "Node IO-Delay", "ratio", "node", ">", 0.15)
def _node_iowait(ctx: Context) -> Iterator[Sample]:
    return _node_ratio(ctx, lambda n: n.iowait)


@metric("node_temperature", "Node Temperatur (SSH, lm-sensors)", "celsius", "node", ">", 80)
def _node_temp(ctx: Context) -> Iterator[Sample]:
    for (c, n), t in ctx.temps.items():
        yield Sample(f"node/{c}/{n}", t, f"Node {n}", _node_attrs(c, n))


@metric("node_updates", "Verfügbare Updates", "count", "node", ">=", 1)
def _node_updates(ctx: Context) -> Iterator[Sample]:
    for (c, n), count in ctx.updates.items():
        yield Sample(f"node/{c}/{n}", count, f"Node {n}", _node_attrs(c, n))


@metric("node_reboot_required", "Neustart erforderlich", "bool", "node")
def _node_reboot(ctx: Context) -> Iterator[Sample]:
    for (c, n), req in ctx.reboot_required.items():
        yield Sample(f"node/{c}/{n}", 1 if req else 0, f"Node {n}", _node_attrs(c, n))


# --- storage & disks -------------------------------------------------------------------------


@metric("storage_usage", "Storage-Belegung", "ratio", "storage", ">", 0.85)
def _storage(ctx: Context) -> Iterator[Sample]:
    for s in _reachable(ctx):
        seen: set[str] = set()
        for st in s.storages:
            if st.status != "available" or not st.total:
                continue
            if st.shared:
                if st.storage in seen:
                    continue
                seen.add(st.storage)
            node = "shared" if st.shared else st.node
            label = f"Storage {st.storage}" + ("" if st.shared else f" auf {st.node}")
            yield Sample(f"storage/{s.name}/{node}/{st.storage}", st.used / st.total, label,
                         {"cluster": s.name, "node": None if st.shared else st.node,
                          "storage": st.storage})


@metric("disk_smart_failed", "SMART-Status fehlerhaft", "bool", "disk")
def _smart(ctx: Context) -> Iterator[Sample]:
    for inv in ctx.inventory:
        for n in inv.nodes:
            for d in n.disks:
                if d.health:
                    bad = d.health.upper() not in ("PASSED", "OK", "UNKNOWN")
                    yield Sample(f"disk/{inv.cluster}/{n.node}/{d.devpath}", 1 if bad else 0,
                                 f"Disk {d.devpath} ({d.model or '?'}) auf {n.node}",
                                 _node_attrs(inv.cluster, n.node))


@metric("disk_wearout", "SSD-Verschleiß (verbraucht)", "percent", "disk", ">", 80)
def _wearout(ctx: Context) -> Iterator[Sample]:
    for inv in ctx.inventory:
        for n in inv.nodes:
            for d in n.disks:
                if d.wearout_used is not None:
                    yield Sample(f"disk/{inv.cluster}/{n.node}/{d.devpath}", d.wearout_used,
                                 f"Disk {d.devpath} ({d.model or '?'}) auf {n.node}",
                                 _node_attrs(inv.cluster, n.node))


# --- guests ----------------------------------------------------------------------------------


def _guest_samples(ctx: Context, fn: Callable[[Any], float | None]) -> Iterator[Sample]:
    for s in _reachable(ctx):
        for g in s.guests:
            if g.template:
                continue
            v = fn(g)
            if v is not None:
                yield Sample(f"guest/{s.name}/{g.vmid}", v, f"{g.vmid} {g.name}",
                             {"cluster": s.name, "node": g.node, "vmid": g.vmid,
                              "tags": g.tags})


@metric("guest_cpu", "Gast CPU-Last (eigene vCPUs)", "ratio", "guest", ">", 0.95)
def _guest_cpu(ctx: Context) -> Iterator[Sample]:
    return _guest_samples(ctx, lambda g: g.cpu if g.status == "running" else None)


@metric("guest_mem", "Gast RAM-Belegung", "ratio", "guest", ">", 0.95)
def _guest_mem(ctx: Context) -> Iterator[Sample]:
    return _guest_samples(ctx, lambda g: g.mem / g.maxmem
                          if g.status == "running" and g.maxmem else None)


@metric("guest_down", "Gast mit Autostart läuft nicht", "bool", "guest")
def _guest_down(ctx: Context) -> Iterator[Sample]:
    onboot = {(inv.cluster, g.vmid) for inv in ctx.inventory for g in inv.guests if g.onboot}
    return _guest_samples(ctx, lambda g: (1 if g.status != "running" else 0)
                          if (g.cluster, g.vmid) in onboot else None)


@metric("backup_failed", "Backup fehlgeschlagen (letzte 26 h)", "bool", "guest")
def _backup_failed(ctx: Context) -> Iterator[Sample]:
    for s in _reachable(ctx):
        for g in s.guests:
            if g.template:
                continue
            err = ctx.failed_backups.get((s.name, str(g.vmid)))
            yield Sample(f"guest/{s.name}/{g.vmid}", 1 if err else 0, f"{g.vmid} {g.name}",
                         {"cluster": s.name, "node": g.node, "vmid": g.vmid, "tags": g.tags,
                          "detail": err})
        # Scheduled jobs for several guests run as one task without VMID.
        for n in s.nodes:
            err = ctx.failed_backups.get((s.name, f"node:{n.node}"))
            yield Sample(f"backupjob/{s.name}/{n.node}", 1 if err else 0,
                         f"Backup-Job auf {n.node}", {**_node_attrs(s.name, n.node),
                                                      "detail": err})


def catalogue() -> list[MetricInfo]:
    return [info for info, _ in REGISTRY.values()]


def matches(selector: dict[str, Any], attrs: dict[str, Any]) -> bool:
    for key in ("cluster", "node", "storage"):
        if selector.get(key) and attrs.get(key) != selector[key]:
            return False
    if selector.get("vmid") not in (None, "") and attrs.get("vmid") != int(selector["vmid"]):
        return False
    return not (selector.get("tag") and selector["tag"] not in (attrs.get("tags") or []))


def compare(value: float, op: str, threshold: float) -> bool:
    return {">": value > threshold, ">=": value >= threshold, "<": value < threshold,
            "<=": value <= threshold}.get(op, False)


# --- backups, snapshots, forecast (from the backup service) ------------------------------------


@metric("backup_age_hours", "Letztes Backup älter als (Stunden)", "hours", "guest", ">", 48)
def _backup_age(ctx: Context) -> Iterator[Sample]:
    for b in ctx.backups:
        if not b.covered_by:  # guests no job covers are reported by backup_not_covered
            continue
        age = b.age_hours if b.age_hours is not None else 99999.0
        detail = "noch nie gesichert" if b.age_hours is None else None
        yield Sample(f"guest/{b.cluster}/{b.vmid}", age, f"{b.vmid} {b.name}",
                     {"cluster": b.cluster, "node": b.node, "vmid": b.vmid, "detail": detail})


@metric("backup_not_covered", "Gast von keinem Backup-Job erfasst", "bool", "guest")
def _not_covered(ctx: Context) -> Iterator[Sample]:
    for b in ctx.backups:
        yield Sample(f"guest/{b.cluster}/{b.vmid}", 0 if b.covered_by else 1,
                     f"{b.vmid} {b.name}", {"cluster": b.cluster, "node": b.node,
                                             "vmid": b.vmid})


@metric("snapshot_age_days", "Ältester Snapshot (Tage)", "days", "guest", ">", 30)
def _snapshot_age(ctx: Context) -> Iterator[Sample]:
    for b in ctx.backups:
        if b.oldest_snapshot_days is not None:
            yield Sample(f"guest/{b.cluster}/{b.vmid}", b.oldest_snapshot_days,
                         f"{b.vmid} {b.name}", {"cluster": b.cluster, "node": b.node,
                                                 "vmid": b.vmid})


@metric("storage_days_until_full", "Storage voll in (Tagen, Prognose)", "days", "storage", "<",
        14)
def _storage_full(ctx: Context) -> Iterator[Sample]:
    for f in ctx.forecasts:
        if f.days_until_full is not None:
            yield Sample(f"storage/{f.cluster}/{f.node}/{f.storage}", f.days_until_full,
                         f"Storage {f.storage}" + ("" if f.node == "shared" else f" auf {f.node}"),
                         {"cluster": f.cluster, "node": None if f.node == "shared" else f.node,
                          "storage": f.storage})
