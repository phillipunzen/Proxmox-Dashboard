"""Turn raw PVE responses into a :class:`ClusterSnapshot`.

Pure functions only (no I/O) so the logic is easy to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.collector.models import (
    ClusterSnapshot,
    ClusterTotals,
    ClusterWarning,
    GuestView,
    NodeView,
    StorageView,
)
from app.config import ClusterConfig

# Thresholds for the built-in overview warnings. Configurable alert rules follow in phase 4.
NODE_CPU_WARN = 0.90
NODE_MEM_WARN = 0.90
IOWAIT_WARN = 0.10
STORAGE_WARN = 0.85
STORAGE_CRIT = 0.95

COUNTER_FIELDS = ("diskread", "diskwrite", "netin", "netout")


@dataclass(frozen=True)
class CounterSample:
    """Cumulative I/O counters of one guest at a point in time (for rate calculation)."""

    ts: float
    values: tuple[int, int, int, int]


def compute_rates(
    prev: CounterSample | None, cur: CounterSample
) -> tuple[float | None, float | None, float | None, float | None]:
    """Bytes/s per counter. ``None`` if there is no usable previous sample.

    A counter that went backwards (guest restarted, counters reset) yields ``None``
    for that field instead of a bogus negative or huge value.
    """
    if prev is None or cur.ts <= prev.ts:
        return (None, None, None, None)
    dt = cur.ts - prev.ts
    out: list[float | None] = []
    for old, new in zip(prev.values, cur.values, strict=True):
        out.append(None if new < old else (new - old) / dt)
    return tuple(out)  # type: ignore[return-value]


def console_url(ui_url: str, guest_type: str, vmid: int, name: str, node: str) -> str:
    """Deep link into the Proxmox web UI console (noVNC for VMs, xterm.js for LXC)."""
    params: dict[str, Any] = {"vmid": vmid, "vmname": name, "node": node, "resize": "off"}
    if guest_type == "qemu":
        params = {"console": "kvm", "novnc": 1, **params}
    else:
        params = {"console": "lxc", "xtermjs": 1, **params}
    return f"{ui_url}/?{urlencode(params)}"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_snapshot(
    cfg: ClusterConfig,
    resources: list[dict[str, Any]],
    cluster_status: list[dict[str, Any]],
    node_status: dict[str, dict[str, Any] | None],
    prev_counters: dict[int, CounterSample],
    now: float,
    api_url: str | None = None,
) -> tuple[ClusterSnapshot, dict[int, CounterSample]]:
    """Build a snapshot and return it together with the new counter samples."""
    cluster_entry = next((e for e in cluster_status if e.get("type") == "cluster"), None)
    status_nodes = {e["name"]: e for e in cluster_status if e.get("type") == "node"}

    nodes: dict[str, NodeView] = {}
    for r in resources:
        if r.get("type") != "node":
            continue
        name = r["node"]
        st = status_nodes.get(name, {})
        status = r.get("status", "unknown")
        if status not in ("online", "offline"):
            status = "unknown"
        nodes[name] = NodeView(
            cluster=cfg.name,
            node=name,
            status=status,
            ip=st.get("ip"),
            cpu=_f(r.get("cpu")),
            maxcpu=_i(r.get("maxcpu")),
            mem=_i(r.get("mem")),
            maxmem=_i(r.get("maxmem")),
            rootfs_used=_i(r.get("disk")) if "disk" in r else None,
            rootfs_total=_i(r.get("maxdisk")) if "maxdisk" in r else None,
            uptime=_i(r.get("uptime")),
        )
        _apply_node_status(nodes[name], node_status.get(name))

    # --- guests -------------------------------------------------------------------------------
    guests: list[GuestView] = []
    new_counters: dict[int, CounterSample] = {}
    for r in resources:
        if r.get("type") not in ("qemu", "lxc"):
            continue
        vmid = _i(r.get("vmid"))
        node = nodes.get(r.get("node", ""))
        host_cpus = node.maxcpu if node and node.maxcpu else 0
        cpu = _f(r.get("cpu"))
        maxcpu = _i(r.get("maxcpu"))
        running = r.get("status") == "running"

        rates: tuple[float | None, ...] = (None, None, None, None)
        if running:
            sample = CounterSample(now, tuple(_i(r.get(k)) for k in COUNTER_FIELDS))  # type: ignore[arg-type]
            rates = compute_rates(prev_counters.get(vmid), sample)
            new_counters[vmid] = sample

        name = r.get("name") or f"{r['type']}-{vmid}"
        guests.append(
            GuestView(
                cluster=cfg.name,
                vmid=vmid,
                name=name,
                type=r["type"],
                node=r.get("node", ""),
                status=r.get("status", "unknown"),
                template=bool(_i(r.get("template"))),
                cpu=cpu,
                # PVE reports guest CPU relative to its vCPUs; scale to host capacity.
                cpu_host=(cpu * maxcpu / host_cpus) if host_cpus else 0.0,
                maxcpu=maxcpu,
                mem=_i(r.get("mem")),
                maxmem=_i(r.get("maxmem")),
                disk=_i(r.get("disk")),
                maxdisk=_i(r.get("maxdisk")),
                diskread_bps=rates[0],
                diskwrite_bps=rates[1],
                netin_bps=rates[2],
                netout_bps=rates[3],
                uptime=_i(r.get("uptime")),
                tags=[t for t in str(r.get("tags", "")).replace(",", ";").split(";") if t],
                pool=r.get("pool"),
                lock=r.get("lock"),
                console_url=console_url(cfg.ui_url, r["type"], vmid, name, r.get("node", "")),
            )
        )
    guests.sort(key=lambda g: g.vmid)

    for g in guests:
        n = nodes.get(g.node)
        if n is None or g.template:
            continue
        n.guests_total += 1
        if g.status == "running":
            n.guests_running += 1
            n.vcpus_allocated += g.maxcpu
            n.mem_allocated += g.maxmem

    # --- storage ------------------------------------------------------------------------------
    storages = [
        StorageView(
            cluster=cfg.name,
            node=r.get("node", ""),
            storage=r.get("storage", ""),
            type=r.get("plugintype", ""),
            status=r.get("status", "unknown"),
            shared=bool(_i(r.get("shared"))),
            used=_i(r.get("disk")),
            total=_i(r.get("maxdisk")),
            content=[c for c in str(r.get("content", "")).split(",") if c],
        )
        for r in resources
        if r.get("type") == "storage"
    ]
    storages.sort(key=lambda s: (s.storage, s.node))

    node_list = sorted(nodes.values(), key=lambda n: n.node)
    totals = compute_totals(node_list, guests, storages)
    quorate = bool(_i(cluster_entry.get("quorate"))) if cluster_entry else None
    versions = {n.pve_version for n in node_list if n.pve_version}

    snap = ClusterSnapshot(
        name=cfg.name,
        is_cluster=cluster_entry is not None,
        reachable=True,
        quorate=quorate,
        api_url=api_url,
        ui_url=cfg.ui_url,
        pve_version=sorted(versions)[-1] if versions else None,
        updated_at=datetime.fromtimestamp(now, UTC),
        totals=totals,
        nodes=node_list,
        guests=guests,
        storages=storages,
    )
    snap.warnings = compute_warnings(snap, versions)
    return snap, new_counters


def _apply_node_status(view: NodeView, st: dict[str, Any] | None) -> None:
    """Enrich a node with data from the slower ``/nodes/{node}/status`` call."""
    if not st:
        return
    if "wait" in st:
        view.iowait = _f(st["wait"])
    if st.get("loadavg"):
        view.loadavg = [_f(x) for x in st["loadavg"]]
    swap = st.get("swap") or {}
    if swap:
        view.swap_used = _i(swap.get("used"))
        view.swap_total = _i(swap.get("total"))
    rootfs = st.get("rootfs") or {}
    if rootfs:
        view.rootfs_used = _i(rootfs.get("used"))
        view.rootfs_total = _i(rootfs.get("total"))
    pveversion = st.get("pveversion")  # "pve-manager/9.0.10/abcdef"
    if pveversion:
        parts = pveversion.split("/")
        view.pve_version = parts[1] if len(parts) > 1 else pveversion
    kernel = (st.get("current-kernel") or {}).get("release")
    view.kernel = kernel or st.get("kversion")
    cpuinfo = st.get("cpuinfo") or {}
    view.cpu_model = cpuinfo.get("model")
    view.cpu_sockets = _i(cpuinfo.get("sockets")) or None
    view.cpu_cores = _i(cpuinfo.get("cores")) or None


def compute_totals(
    nodes: list[NodeView], guests: list[GuestView], storages: list[StorageView]
) -> ClusterTotals:
    t = ClusterTotals(nodes_total=len(nodes))
    online = [n for n in nodes if n.status == "online"]
    t.nodes_online = len(online)
    t.cpus = sum(n.maxcpu for n in online)
    if t.cpus:
        t.cpu = sum(n.cpu * n.maxcpu for n in online) / t.cpus
    t.mem = sum(n.mem for n in online)
    t.maxmem = sum(n.maxmem for n in online)
    t.swap_used = sum(n.swap_used or 0 for n in online)
    t.swap_total = sum(n.swap_total or 0 for n in online)
    waits = [n.iowait for n in online if n.iowait is not None]
    t.iowait = sum(waits) / len(waits) if waits else None
    loads = [n.loadavg[0] for n in online if n.loadavg]
    t.load1 = sum(loads) if loads else None

    for g in guests:
        if g.template:
            t.templates += 1
            continue
        running = g.status == "running"
        if g.type == "qemu":
            t.vms_running += running
            t.vms_stopped += not running
        else:
            t.lxc_running += running
            t.lxc_stopped += not running
        if running:
            t.vcpus_allocated += g.maxcpu
            t.mem_allocated += g.maxmem
            t.netin_bps += g.netin_bps or 0.0
            t.netout_bps += g.netout_bps or 0.0
            t.diskread_bps += g.diskread_bps or 0.0
            t.diskwrite_bps += g.diskwrite_bps or 0.0

    # Shared storages (NFS, PBS, Ceph ...) appear once per node - count them once.
    seen_shared: set[str] = set()
    for s in storages:
        if s.status != "available" or not s.total:
            continue
        if s.shared:
            if s.storage in seen_shared:
                continue
            seen_shared.add(s.storage)
        t.storage_used += s.used
        t.storage_total += s.total
    return t


def compute_warnings(snap: ClusterSnapshot, versions: set[str]) -> list[ClusterWarning]:
    w: list[ClusterWarning] = []
    if snap.quorate is False:
        w.append(ClusterWarning(severity="critical", target="cluster", message="Kein Quorum"))
    for n in snap.nodes:
        target = f"node/{n.node}"
        if n.status != "online":
            w.append(ClusterWarning(severity="critical", target=target,
                                    message=f"Node {n.node} ist {n.status}"))
            continue
        if n.cpu >= NODE_CPU_WARN:
            w.append(ClusterWarning(severity="warning", target=target,
                                    message=f"CPU-Last auf {n.node} bei {n.cpu:.0%}"))
        if n.maxmem and n.mem / n.maxmem >= NODE_MEM_WARN:
            w.append(ClusterWarning(severity="warning", target=target,
                                    message=f"RAM auf {n.node} zu {n.mem / n.maxmem:.0%} belegt"))
        if n.iowait is not None and n.iowait >= IOWAIT_WARN:
            w.append(ClusterWarning(severity="warning", target=target,
                                    message=f"IO-Delay auf {n.node} bei {n.iowait:.0%}"))
    seen_shared: set[str] = set()
    for s in snap.storages:
        if s.status != "available" or not s.total:
            continue
        if s.shared:
            if s.storage in seen_shared:
                continue
            seen_shared.add(s.storage)
        ratio = s.used / s.total
        label = s.storage if s.shared else f"{s.storage} ({s.node})"
        if ratio >= STORAGE_CRIT:
            sev = "critical"
        elif ratio >= STORAGE_WARN:
            sev = "warning"
        else:
            continue
        w.append(ClusterWarning(severity=sev, target=f"storage/{s.node}/{s.storage}",
                                message=f"Storage {label} zu {ratio:.0%} belegt"))
    if len(versions) > 1:
        listed = ", ".join(sorted(versions))
        w.append(ClusterWarning(severity="info", target="cluster",
                                message=f"Unterschiedliche PVE-Versionen: {listed}"))
    return w
