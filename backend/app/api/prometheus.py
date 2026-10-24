"""Prometheus exposition endpoint ``/metrics``.

Authentication: an API key with scope ``read`` (Prometheus: ``authorization: {credentials: …}``)
or a logged-in session. ``METRICS_PUBLIC=true`` disables auth for this endpoint only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from app import __version__
from app.auth.deps import require_role
from app.collector.models import ClusterSnapshot

_HELP: dict[str, tuple[str, str]] = {
    "pve_dashboard_info": ("gauge", "Dashboard version"),
    "pve_cluster_up": ("gauge", "1 if the Proxmox API of the cluster is reachable"),
    "pve_cluster_quorate": ("gauge", "1 if the cluster has quorum"),
    "pve_cluster_last_update_timestamp_seconds": ("gauge", "Time of the last successful poll"),
    "pve_node_up": ("gauge", "1 if the node is online"),
    "pve_node_info": ("gauge", "Node versions (always 1)"),
    "pve_node_cpu_ratio": ("gauge", "CPU utilisation 0..1"),
    "pve_node_cpus": ("gauge", "Logical CPUs (threads)"),
    "pve_node_memory_used_bytes": ("gauge", "Used RAM"),
    "pve_node_memory_total_bytes": ("gauge", "Total RAM"),
    "pve_node_swap_used_bytes": ("gauge", "Used swap"),
    "pve_node_iowait_ratio": ("gauge", "IO delay 0..1"),
    "pve_node_load1": ("gauge", "1-minute load average"),
    "pve_node_uptime_seconds": ("gauge", "Uptime"),
    "pve_node_maintenance": ("gauge", "1 if the node is in dashboard maintenance mode"),
    "pve_node_updates_pending": ("gauge", "Upgradable packages"),
    "pve_guest_up": ("gauge", "1 if the guest is running"),
    "pve_guest_info": ("gauge", "Guest metadata (always 1)"),
    "pve_guest_cpu_ratio": ("gauge", "CPU utilisation relative to the guest's vCPUs, 0..1"),
    "pve_guest_cpu_host_ratio": ("gauge", "Share of the host CPU capacity, 0..1"),
    "pve_guest_cpus": ("gauge", "Configured vCPUs"),
    "pve_guest_memory_used_bytes": ("gauge", "Used RAM"),
    "pve_guest_memory_max_bytes": ("gauge", "Configured RAM"),
    "pve_guest_disk_read_bytes_per_second": ("gauge", "Disk read rate"),
    "pve_guest_disk_write_bytes_per_second": ("gauge", "Disk write rate"),
    "pve_guest_network_receive_bytes_per_second": ("gauge", "Network in"),
    "pve_guest_network_transmit_bytes_per_second": ("gauge", "Network out"),
    "pve_guest_uptime_seconds": ("gauge", "Uptime"),
    "pve_storage_used_bytes": ("gauge", "Used storage (shared storages: node=\"shared\")"),
    "pve_storage_total_bytes": ("gauge", "Storage capacity"),
    "pve_alerts_firing": ("gauge", "Currently firing alerts by severity"),
}


def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Exposition:
    def __init__(self) -> None:
        self._samples: dict[str, list[str]] = defaultdict(list)

    def add(self, metric: str, value: float | int | bool | None, **labels: object) -> None:
        if value is None:
            return
        lbl = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items() if v is not None)
        self._samples[metric].append(f"{metric}{{{lbl}}} {float(value):.12g}")

    def render(self) -> str:
        out: list[str] = []
        for metric, lines in self._samples.items():
            mtype, text = _HELP.get(metric, ("gauge", metric))
            out += [f"# HELP {metric} {text}", f"# TYPE {metric} {mtype}", *lines]
        return "\n".join(out) + "\n"


def build(snaps: Iterable[ClusterSnapshot], maintenance: set[tuple[str, str]],
          updates: dict[tuple[str, str], int], firing: dict[str, int]) -> str:
    e = Exposition()
    e.add("pve_dashboard_info", 1, version=__version__)
    for s in snaps:
        c = s.name
        e.add("pve_cluster_up", s.reachable, cluster=c)
        if s.quorate is not None:
            e.add("pve_cluster_quorate", s.quorate, cluster=c)
        if s.updated_at:
            e.add("pve_cluster_last_update_timestamp_seconds", s.updated_at.timestamp(), cluster=c)
        for n in s.nodes:
            lbl = {"cluster": c, "node": n.node}
            e.add("pve_node_up", n.status == "online", **lbl)
            e.add("pve_node_maintenance", (c, n.node) in maintenance, **lbl)
            if (c, n.node) in updates:
                e.add("pve_node_updates_pending", updates[(c, n.node)], **lbl)
            if n.status != "online":
                continue
            e.add("pve_node_info", 1, **lbl, pve_version=n.pve_version, kernel=n.kernel,
                  cpu_model=n.cpu_model)
            e.add("pve_node_cpu_ratio", n.cpu, **lbl)
            e.add("pve_node_cpus", n.maxcpu, **lbl)
            e.add("pve_node_memory_used_bytes", n.mem, **lbl)
            e.add("pve_node_memory_total_bytes", n.maxmem, **lbl)
            e.add("pve_node_swap_used_bytes", n.swap_used, **lbl)
            e.add("pve_node_iowait_ratio", n.iowait, **lbl)
            e.add("pve_node_load1", n.loadavg[0] if n.loadavg else None, **lbl)
            e.add("pve_node_uptime_seconds", n.uptime, **lbl)
        for g in s.guests:
            if g.template:
                continue
            lbl = {"cluster": c, "node": g.node, "vmid": g.vmid, "name": g.name, "type": g.type}
            running = g.status == "running"
            e.add("pve_guest_up", running, **lbl)
            e.add("pve_guest_info", 1, **lbl, status=g.status, tags=";".join(g.tags),
                  pool=g.pool or "")
            e.add("pve_guest_cpus", g.maxcpu, **lbl)
            e.add("pve_guest_memory_max_bytes", g.maxmem, **lbl)
            if not running:
                continue
            e.add("pve_guest_cpu_ratio", g.cpu, **lbl)
            e.add("pve_guest_cpu_host_ratio", g.cpu_host, **lbl)
            e.add("pve_guest_memory_used_bytes", g.mem, **lbl)
            e.add("pve_guest_disk_read_bytes_per_second", g.diskread_bps, **lbl)
            e.add("pve_guest_disk_write_bytes_per_second", g.diskwrite_bps, **lbl)
            e.add("pve_guest_network_receive_bytes_per_second", g.netin_bps, **lbl)
            e.add("pve_guest_network_transmit_bytes_per_second", g.netout_bps, **lbl)
            e.add("pve_guest_uptime_seconds", g.uptime, **lbl)
        seen: set[str] = set()
        for st in s.storages:
            if st.status != "available" or not st.total:
                continue
            node = "shared" if st.shared else st.node
            if st.shared and st.storage in seen:
                continue
            seen.add(st.storage)
            lbl = {"cluster": c, "node": node, "storage": st.storage, "type": st.type}
            e.add("pve_storage_used_bytes", st.used, **lbl)
            e.add("pve_storage_total_bytes", st.total, **lbl)
    for sev in ("info", "warning", "critical"):
        e.add("pve_alerts_firing", firing.get(sev, 0), severity=sev)
    return e.render()


async def _render(request: Request) -> PlainTextResponse:
    state = request.app.state
    nodes = getattr(state, "nodes", None)
    maintenance = await nodes.maintenance_set() if nodes else set()
    updates = nodes.pending_counts() if nodes else {}
    firing = await state.alerts.firing_counts() if hasattr(state, "alerts") else {}
    text = build(state.store.all(), maintenance, updates, firing)
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")


def make_router(public: bool) -> APIRouter:
    r = APIRouter(tags=["prometheus"])
    deps = [] if public else [Depends(require_role("viewer"))]

    @r.get("/metrics", dependencies=deps, response_class=PlainTextResponse,
           summary="Prometheus metrics (API key with scope 'read')")
    async def metrics(request: Request) -> PlainTextResponse:
        return await _render(request)

    return r
