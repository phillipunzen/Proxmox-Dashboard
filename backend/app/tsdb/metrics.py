"""Snapshot -> time series (Prometheus text exposition format with timestamps).

Metric catalogue (all gauges, written once per poll):

==================================  ================================  =========================
metric                              labels                            meaning
==================================  ================================  =========================
pve_cluster_cpu_ratio               cluster                           capacity-weighted CPU 0..1
pve_cluster_cpus                    cluster                           threads of online nodes
pve_cluster_mem_used_bytes          cluster
pve_cluster_mem_total_bytes         cluster
pve_cluster_{netin,netout}_bps      cluster                           sum of guests
pve_cluster_{diskread,diskwrite}_bps cluster                          sum of guests
pve_node_up                         cluster,node                      1 online / 0 otherwise
pve_node_cpu_ratio                  cluster,node                      0..1
pve_node_cpus                       cluster,node                      threads
pve_node_cpu_cores_used             cluster,node                      cpu_ratio * threads
pve_node_mem_{used,total}_bytes     cluster,node
pve_node_swap_used_bytes            cluster,node
pve_node_iowait_ratio               cluster,node
pve_node_load1                      cluster,node
pve_guest_cpu_cores                 cluster,node,vmid,name,type       host threads in use
pve_guest_cpu_ratio                 cluster,node,vmid,name,type       relative to own vCPUs
pve_guest_mem_used_bytes            cluster,node,vmid,name,type
pve_guest_{diskread,diskwrite}_bps  cluster,node,vmid,name,type
pve_guest_{netin,netout}_bps        cluster,node,vmid,name,type
pve_storage_{used,total}_bytes      cluster,node,storage,type         shared storage: node="shared"
==================================  ================================  =========================

Guest series carry the node label, so "which guest loaded *this* node at time t" stays
correct after migrations. Only running guests are written (no flat zero lines).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.collector.models import ClusterSnapshot


def _esc(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    return ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())


def line(metric: str, labels: str, value: float, ts_ms: int) -> str:
    return f"{metric}{{{labels}}} {value:.12g} {ts_ms}"


def snapshot_lines(snap: ClusterSnapshot) -> list[str]:
    """All series for one cluster snapshot (empty if the snapshot is stale/unreachable)."""
    if not snap.reachable or snap.updated_at is None:
        return []
    ts = int(snap.updated_at.timestamp() * 1000)
    out: list[str] = []
    add = out.append
    c = _labels(cluster=snap.name)
    t = snap.totals

    for metric, value in (
        ("pve_cluster_cpu_ratio", t.cpu),
        ("pve_cluster_cpus", t.cpus),
        ("pve_cluster_mem_used_bytes", t.mem),
        ("pve_cluster_mem_total_bytes", t.maxmem),
        ("pve_cluster_netin_bps", t.netin_bps),
        ("pve_cluster_netout_bps", t.netout_bps),
        ("pve_cluster_diskread_bps", t.diskread_bps),
        ("pve_cluster_diskwrite_bps", t.diskwrite_bps),
    ):
        add(line(metric, c, value, ts))

    for n in snap.nodes:
        lbl = _labels(cluster=snap.name, node=n.node)
        online = n.status == "online"
        add(line("pve_node_up", lbl, 1 if online else 0, ts))
        if not online:
            continue
        add(line("pve_node_cpu_ratio", lbl, n.cpu, ts))
        add(line("pve_node_cpus", lbl, n.maxcpu, ts))
        add(line("pve_node_cpu_cores_used", lbl, n.cpu * n.maxcpu, ts))
        add(line("pve_node_mem_used_bytes", lbl, n.mem, ts))
        add(line("pve_node_mem_total_bytes", lbl, n.maxmem, ts))
        if n.swap_used is not None:
            add(line("pve_node_swap_used_bytes", lbl, n.swap_used, ts))
        if n.iowait is not None:
            add(line("pve_node_iowait_ratio", lbl, n.iowait, ts))
        if n.loadavg:
            add(line("pve_node_load1", lbl, n.loadavg[0], ts))

    for g in snap.guests:
        if g.status != "running" or g.template:
            continue
        lbl = _labels(cluster=snap.name, node=g.node, vmid=g.vmid, name=g.name, type=g.type)
        add(line("pve_guest_cpu_cores", lbl, g.cpu * g.maxcpu, ts))
        add(line("pve_guest_cpu_ratio", lbl, g.cpu, ts))
        add(line("pve_guest_mem_used_bytes", lbl, g.mem, ts))
        for metric, value in (
            ("pve_guest_diskread_bps", g.diskread_bps),
            ("pve_guest_diskwrite_bps", g.diskwrite_bps),
            ("pve_guest_netin_bps", g.netin_bps),
            ("pve_guest_netout_bps", g.netout_bps),
        ):
            if value is not None:
                add(line(metric, lbl, value, ts))

    out.extend(storage_lines(snap.name, ((s.node, s.storage, s.type, s.shared, s.used, s.total)
                                         for s in snap.storages if s.status == "available"), ts))
    return out


def storage_lines(cluster: str, rows: Iterable[tuple[str, str, str, bool, int, int]],
                  ts: int) -> list[str]:
    out: list[str] = []
    seen_shared: set[str] = set()
    for node, storage, stype, shared, used, total in rows:
        if not total:
            continue
        if shared:
            if storage in seen_shared:
                continue
            seen_shared.add(storage)
            node = "shared"
        lbl = _labels(cluster=cluster, node=node, storage=storage, type=stype)
        out.append(line("pve_storage_used_bytes", lbl, used, ts))
        out.append(line("pve_storage_total_bytes", lbl, total, ts))
    return out
