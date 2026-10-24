"""Periodic collection of hardware and guest configuration (slow-changing data)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.collector.models import GuestView
from app.collector.state import SnapshotStore
from app.config import ClusterConfig
from app.inventory import parse
from app.inventory.models import ClusterInventory, GuestInventory, NodeHardware, PassthroughRef
from app.pve import PveApi

log = logging.getLogger(__name__)

# Parallel PVE requests per cluster during an inventory run.
CONCURRENCY = 6
MIN_REFRESH_GAP = 30.0


class InventoryStore:
    def __init__(self) -> None:
        self.clusters: dict[str, ClusterInventory] = {}

    def get(self, name: str) -> ClusterInventory | None:
        return self.clusters.get(name)

    def all(self) -> list[ClusterInventory]:
        return list(self.clusters.values())


class InventoryCollector:
    def __init__(self, clusters: list[ClusterConfig], clients: dict[str, PveApi],
                 snapshots: SnapshotStore, store: InventoryStore, interval: float):
        self.clusters = clusters
        self.clients = clients
        self.snapshots = snapshots
        self.store = store
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._last_run = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="inventory")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)

    async def refresh(self, force: bool = False) -> bool:
        """Collect all clusters. Returns False if skipped (another run, or too soon)."""
        if self._lock.locked() or (force and time.monotonic() - self._last_run < MIN_REFRESH_GAP):
            return False
        async with self._lock:
            self._last_run = time.monotonic()
            await asyncio.gather(*(self._collect_cluster(c) for c in self.clusters))
        return True

    async def _collect_cluster(self, cfg: ClusterConfig) -> None:
        started = time.monotonic()
        client = self.clients[cfg.name]
        sem = asyncio.Semaphore(CONCURRENCY)

        async def get(path: str) -> Any:
            async with sem:
                return await client.get(path)

        snap = self.snapshots.get(cfg.name)
        if snap is None or not snap.reachable:
            log.info("Inventory for %s skipped: no live snapshot yet", cfg.name)
            return
        try:
            mappings_raw = await get("/cluster/mapping/pci")
        except Exception as exc:  # noqa: BLE001 - optional, needs Mapping.Audit
            log.debug("PCI mappings unavailable for %s: %s", cfg.name, exc)
            mappings_raw = []

        nodes = await asyncio.gather(*(self._collect_node(cfg.name, n.node, n.status, get)
                                       for n in snap.nodes))
        known_pci = {n.node: [d.id for d in n.pci] for n in nodes}
        guests = await asyncio.gather(*(
            self._collect_guest(cfg.name, g, get, known_pci.get(g.node, []),
                                parse.parse_mappings(mappings_raw, g.node))
            for g in snap.guests))

        _link_passthrough(nodes, guests)
        now = datetime.now(UTC)
        self.store.clusters[cfg.name] = ClusterInventory(
            cluster=cfg.name, nodes=list(nodes), guests=sorted(guests, key=lambda g: g.vmid),
            collected_at=now, duration_s=round(time.monotonic() - started, 2))
        log.info("Inventory %s: %d nodes, %d guests in %.1f s", cfg.name, len(nodes),
                 len(guests), time.monotonic() - started)

    async def _collect_node(self, cluster: str, node: str, status: str, get: Any) -> NodeHardware:
        hw = NodeHardware(cluster=cluster, node=node, online=status == "online",
                          collected_at=datetime.now(UTC))
        if not hw.online:
            # Keep the last known hardware of an offline node.
            prev = self.store.get(cluster)
            old = next((n for n in prev.nodes if n.node == node), None) if prev else None
            if old:
                return old.model_copy(update={"online": False})
            return hw

        async def safe(path: str) -> Any:
            try:
                return await get(path)
            except Exception as exc:  # noqa: BLE001 - one failing call must not drop the node
                hw.errors.append(f"{path}: {exc}")
                return None

        st, disks, net, pci = await asyncio.gather(
            safe(f"/nodes/{node}/status"),
            safe(f"/nodes/{node}/disks/list"),
            safe(f"/nodes/{node}/network"),
            safe(f"/nodes/{node}/hardware/pci"),
        )
        if st:
            cpu = st.get("cpuinfo") or {}
            hw.cpu_model = cpu.get("model")
            hw.cpu_sockets = parse._int(cpu.get("sockets"))
            hw.cpu_cores = parse._int(cpu.get("cores"))
            hw.cpu_threads = parse._int(cpu.get("cpus"))
            with contextlib.suppress(TypeError, ValueError):
                hw.cpu_mhz = float(cpu.get("mhz"))
            hw.mem_total = parse._int((st.get("memory") or {}).get("total"))
            hw.kernel = (st.get("current-kernel") or {}).get("release") or st.get("kversion")
            pv = st.get("pveversion") or ""
            hw.pve_version = pv.split("/")[1] if pv.count("/") >= 1 else pv or None
            boot = st.get("boot-info") or {}
            hw.boot_mode = boot.get("mode")
            if "secureboot" in boot:
                hw.secure_boot = bool(boot["secureboot"])
        hw.disks = parse.parse_disks(disks or [])
        hw.network = parse.parse_network(net or [])
        hw.pci = parse.parse_pci(pci or [])
        return hw

    async def _collect_guest(self, cluster: str, g: GuestView, get: Any, known_pci: list[str],
                             mappings: dict[str, list[str]]) -> GuestInventory:
        inv = GuestInventory(cluster=cluster, vmid=g.vmid, name=g.name, type=g.type, node=g.node,
                             status=g.status, template=g.template, tags=g.tags,
                             collected_at=datetime.now(UTC))
        base = f"/nodes/{g.node}/{g.type}/{g.vmid}"
        try:
            parse.apply_guest_config(inv, await get(f"{base}/config"), known_pci, mappings)
        except Exception as exc:  # noqa: BLE001
            inv.error = f"config: {exc}"
            return inv
        if g.status != "running":
            return inv
        if g.type == "qemu" and inv.agent_enabled:
            try:
                osinfo = await get(f"{base}/agent/get-osinfo")
                result = (osinfo or {}).get("result", {})
                inv.os_name = result.get("pretty-name") or result.get("name")
                inv.agent_running = True
                ifaces = await get(f"{base}/agent/network-get-interfaces")
                inv.ip_addresses = parse.agent_ips(ifaces)
            except Exception:  # noqa: BLE001 - agent not running / no permission
                inv.agent_running = False
        elif g.type == "lxc":
            # The endpoint may be missing on old PVE versions.
            with contextlib.suppress(Exception):
                inv.ip_addresses = parse.lxc_ips(await get(f"{base}/interfaces"))
        return inv


def _link_passthrough(nodes: list[NodeHardware] | tuple[NodeHardware, ...],
                      guests: list[GuestInventory] | tuple[GuestInventory, ...]) -> None:
    """Mark PCI devices that are assigned to guests (hostpciN)."""
    by_node = {n.node: {d.id: d for d in n.pci} for n in nodes}
    for d in (d for n in nodes for d in n.pci):
        d.assigned_to = []
    for g in guests:
        for hp in g.hostpci:
            for dev_id in hp.devices:
                dev = by_node.get(g.node, {}).get(dev_id)
                if dev:
                    dev.assigned_to.append(PassthroughRef(cluster=g.cluster, vmid=g.vmid,
                                                          name=g.name, key=hp.key))
