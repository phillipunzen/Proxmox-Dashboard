"""Synthetic Proxmox VE API for development and tests.

Answers the same paths as the real API with plausible, time-varying values:
per-guest CPU follows a daily sine curve plus noise and occasional spikes,
I/O and network counters grow monotonically (like the real cumulative counters).

Values are derived deterministically from (seed, guest, time) so tests are reproducible
when a fixed clock is injected.
"""

from __future__ import annotations

import math
import random
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.pve.client import PveError

GiB = 1024**3


@dataclass
class MockNode:
    name: str
    cpus: int
    mem: int
    cpu_model: str
    online: bool = True


@dataclass
class MockGuest:
    vmid: int
    name: str
    type: str  # "qemu" | "lxc"
    node: str
    cpus: int
    maxmem: int
    maxdisk: int
    base_load: float  # mean CPU utilisation relative to its own vCPUs (0..1)
    io_rate: float  # mean bytes/s disk read+write
    net_rate: float  # mean bytes/s network
    status: str = "running"
    tags: str = ""
    pool: str | None = None
    template: bool = False
    phase: float = field(default=0.0)


@dataclass
class MockStorage:
    storage: str
    type: str
    total: int
    used: int
    shared: bool = False
    content: str = "images,rootdir"


@dataclass
class MockClusterSpec:
    name: str
    is_cluster: bool
    nodes: list[MockNode]
    guests: list[MockGuest]
    storages: list[MockStorage]


def _homelab_spec(rng: random.Random) -> MockClusterSpec:
    nodes = [
        MockNode("pve1", 16, 64 * GiB, "AMD Ryzen 7 5700G with Radeon Graphics"),
        MockNode("pve2", 16, 64 * GiB, "AMD Ryzen 7 5700G with Radeon Graphics"),
        MockNode("pve3", 12, 32 * GiB, "Intel(R) Core(TM) i5-12500"),
    ]
    vm_names = [
        "homeassistant", "win11-desk", "opnsense", "truenas", "k3s-master", "k3s-worker1",
        "k3s-worker2", "pbs-test", "docker-host", "gitlab", "nextcloud", "debian-tpl", "ubuntu-lab",
    ]
    lxc_names = [
        "pihole", "adguard", "unifi", "nginx-proxy", "grafana", "influxdb", "mosquitto",
        "zigbee2mqtt", "jellyfin", "sonarr", "radarr", "prowlarr", "paperless", "vaultwarden",
        "uptime-kuma", "wireguard", "postgres", "redis", "minio", "dashboard",
    ]
    tag_pool = ["prod", "media", "infra", "smarthome", "lab", "k8s", "backup"]
    guests: list[MockGuest] = []
    vmid = 100
    for i, name in enumerate(vm_names):
        heavy = name in {"win11-desk", "gitlab", "k3s-worker1", "truenas"}
        guests.append(
            MockGuest(
                vmid=vmid, name=name, type="qemu", node=nodes[i % 3].name,
                cpus=rng.choice([2, 4, 4, 8]) if heavy else rng.choice([1, 2, 2, 4]),
                maxmem=rng.choice([8, 16]) * GiB if heavy else rng.choice([2, 4, 4, 8]) * GiB,
                maxdisk=rng.choice([32, 64, 128, 256]) * GiB,
                base_load=rng.uniform(0.25, 0.6) if heavy else rng.uniform(0.02, 0.2),
                io_rate=rng.uniform(2e6, 30e6) if heavy else rng.uniform(5e4, 2e6),
                net_rate=rng.uniform(1e6, 20e6) if heavy else rng.uniform(1e4, 1e6),
                status="stopped" if name in {"ubuntu-lab", "pbs-test"} else "running",
                tags=";".join(sorted(rng.sample(tag_pool, rng.randint(0, 2)))),
                pool="k8s" if name.startswith("k3s") else None,
                template=name == "debian-tpl",
                phase=rng.uniform(0, 2 * math.pi),
            )
        )
        vmid += 1
    vmid = 200
    for i, name in enumerate(lxc_names):
        heavy = name in {"jellyfin", "paperless", "postgres"}
        guests.append(
            MockGuest(
                vmid=vmid, name=name, type="lxc", node=nodes[(i + 1) % 3].name,
                cpus=rng.choice([2, 4]) if heavy else rng.choice([1, 1, 2]),
                maxmem=rng.choice([2, 4]) * GiB if heavy else rng.choice([0.5, 1, 1, 2]) * GiB,
                maxdisk=rng.choice([8, 16, 32]) * GiB,
                base_load=rng.uniform(0.15, 0.5) if heavy else rng.uniform(0.005, 0.08),
                io_rate=rng.uniform(1e6, 15e6) if heavy else rng.uniform(1e3, 3e5),
                net_rate=rng.uniform(5e5, 8e6) if heavy else rng.uniform(1e3, 2e5),
                status="stopped" if name in {"adguard", "minio"} else "running",
                tags=";".join(sorted(rng.sample(tag_pool, rng.randint(0, 2)))),
                phase=rng.uniform(0, 2 * math.pi),
            )
        )
        vmid += 1
    storages = [
        MockStorage("local", "dir", 100 * GiB, 0, content="iso,vztmpl,backup"),
        MockStorage("local-zfs", "zfspool", 900 * GiB, 0),
        MockStorage("nfs-nas", "nfs", 8000 * GiB, 5100 * GiB, shared=True, content="backup,iso"),
        MockStorage("pbs", "pbs", 4000 * GiB, 2650 * GiB, shared=True, content="backup"),
    ]
    return MockClusterSpec("homelab", True, nodes, guests, storages)


def _standalone_spec(rng: random.Random) -> MockClusterSpec:
    nodes = [MockNode("edge", 4, 16 * GiB, "Intel(R) N100")]
    guests = [
        MockGuest(300, "backup-pbs", "qemu", "edge", 2, 4 * GiB, 64 * GiB, 0.1, 5e6, 3e6,
                  tags="backup", phase=1.0),
        MockGuest(301, "edge-dns", "lxc", "edge", 1, 512 * 1024**2, 4 * GiB, 0.02, 2e3, 5e3,
                  tags="infra", phase=2.0),
        MockGuest(302, "monitoring", "lxc", "edge", 2, 2 * GiB, 16 * GiB, 0.08, 4e5, 2e5,
                  phase=3.0),
    ]
    storages = [
        MockStorage("local", "dir", 50 * GiB, 0, content="iso,vztmpl,backup"),
        MockStorage("local-lvm", "lvmthin", 400 * GiB, 0),
    ]
    return MockClusterSpec("standalone", False, nodes, guests, storages)


SPECS: dict[str, Callable[[random.Random], MockClusterSpec]] = {
    "homelab": _homelab_spec,
    "standalone": _standalone_spec,
}


class MockPveClient:
    """Drop-in replacement for :class:`PveClient` serving synthetic data."""

    def __init__(self, cluster_name: str, seed: int = 42, clock: Callable[[], float] = time.time):
        self.rng = random.Random(seed)
        spec_factory = SPECS.get(cluster_name, _standalone_spec)
        self.spec = spec_factory(self.rng)
        self.spec.name = cluster_name
        self.clock = clock
        self.started = clock() - 86400 * 12.3  # pretend the cluster booted ~12 days ago
        self.seed = seed
        self._fill_storage()

    # -- value generators ----------------------------------------------------------------------

    def _noise(self, *key: Any) -> float:
        """Deterministic pseudo-noise in [0, 1) for a key and the current 10 s bucket."""
        bucket = int(self.clock() // 10)
        return random.Random(zlib.crc32(repr((self.seed, bucket, *key)).encode())).random()

    def guest_cpu(self, g: MockGuest) -> float:
        if g.status != "running":
            return 0.0
        t = self.clock()
        daily = 0.5 + 0.5 * math.sin(2 * math.pi * t / 86400 + g.phase)
        value = g.base_load * (0.4 + 0.9 * daily) + 0.08 * g.base_load * self._noise(g.vmid)
        # Rare spike: one 10 s bucket out of ~40 per guest lights up.
        if self._noise(g.vmid, "spike") > 0.975:
            value += 0.4
        return max(0.0, min(1.0, value))

    def guest_mem(self, g: MockGuest) -> int:
        if g.status != "running":
            return 0
        frac = 0.35 + 0.4 * ((g.vmid * 37) % 100) / 100 + 0.05 * self._noise(g.vmid, "mem")
        return int(g.maxmem * min(frac, 0.97))

    def _counter(self, g: MockGuest, rate: float, salt: str) -> int:
        """Cumulative counter since boot, with mild time variation."""
        if g.status != "running":
            return 0
        # Integral of rate * (1 + 0.3 * sin(t/600 + p)) -> the rate itself varies by +-30 %.
        t = self.clock()
        p = g.phase + len(salt)
        elapsed = t - self.started
        drift = math.cos(t / 600 + p) - math.cos(self.started / 600 + p)
        return int(rate * (elapsed - 0.3 * 600 * drift))

    def _fill_storage(self) -> None:
        for s in self.spec.storages:
            if s.used:
                continue
            if s.type in ("zfspool", "lvmthin"):
                s.used = sum(int(g.maxdisk * 0.55) for g in self.spec.guests)
                s.used = min(s.used, int(s.total * 0.72))
            else:
                s.used = int(s.total * 0.41)

    def _node_guests(self, node: str) -> list[MockGuest]:
        return [g for g in self.spec.guests if g.node == node]

    def node_cpu(self, n: MockNode) -> float:
        if not n.online:
            return 0.0
        used = sum(self.guest_cpu(g) * g.cpus for g in self._node_guests(n.name))
        return min(1.0, 0.02 + used / n.cpus)

    def node_mem(self, n: MockNode) -> int:
        if not n.online:
            return 0
        used = sum(self.guest_mem(g) for g in self._node_guests(n.name))
        return min(int(n.mem * 0.98), int(used + 3.5 * GiB))  # + host / ZFS ARC overhead

    # -- API -------------------------------------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        path = "/" + path.strip("/")
        if path == "/cluster/resources":
            return self._resources((params or {}).get("type"))
        if path == "/cluster/status":
            return self._cluster_status()
        if path == "/version":
            return {"version": "9.0.10", "release": "9.0", "repoid": "deadbeef"}
        m = re.fullmatch(r"/nodes/([^/]+)/status", path)
        if m:
            return self._node_status(m.group(1))
        raise PveError(501, "Not implemented in mock", path)

    async def aclose(self) -> None:
        return None

    def _resources(self, rtype: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        uptime = int(self.clock() - self.started)
        if rtype in (None, "node"):
            for n in self.spec.nodes:
                out.append({
                    "id": f"node/{n.name}", "type": "node", "node": n.name,
                    "status": "online" if n.online else "offline",
                    "cpu": self.node_cpu(n), "maxcpu": n.cpus,
                    "mem": self.node_mem(n), "maxmem": n.mem,
                    "disk": 12 * GiB, "maxdisk": 100 * GiB,
                    "uptime": uptime if n.online else 0, "level": "",
                })
        if rtype in (None, "vm"):
            for g in self.spec.guests:
                running = g.status == "running"
                item: dict[str, Any] = {
                    "id": f"{g.type}/{g.vmid}", "type": g.type, "vmid": g.vmid, "name": g.name,
                    "node": g.node, "status": g.status, "template": int(g.template),
                    "cpu": self.guest_cpu(g), "maxcpu": g.cpus,
                    "mem": self.guest_mem(g), "maxmem": g.maxmem,
                    "disk": int(g.maxdisk * 0.4) if g.type == "lxc" and running else 0,
                    "maxdisk": g.maxdisk,
                    "diskread": self._counter(g, g.io_rate * 0.6, "r"),
                    "diskwrite": self._counter(g, g.io_rate * 0.4, "w"),
                    "netin": self._counter(g, g.net_rate * 0.55, "i"),
                    "netout": self._counter(g, g.net_rate * 0.45, "o"),
                    "uptime": int(uptime * 0.8) if running else 0,
                }
                if g.tags:
                    item["tags"] = g.tags
                if g.pool:
                    item["pool"] = g.pool
                out.append(item)
        if rtype in (None, "storage"):
            for n in self.spec.nodes:
                for s in self.spec.storages:
                    out.append({
                        "id": f"storage/{n.name}/{s.storage}", "type": "storage",
                        "storage": s.storage, "node": n.name, "plugintype": s.type,
                        "status": "available" if n.online else "unknown",
                        "disk": s.used, "maxdisk": s.total, "shared": int(s.shared),
                        "content": s.content,
                    })
        return out

    def _cluster_status(self) -> list[dict[str, Any]]:
        nodes = self.spec.nodes
        online = sum(n.online for n in nodes)
        out: list[dict[str, Any]] = []
        if self.spec.is_cluster:
            out.append({
                "id": "cluster", "type": "cluster", "name": self.spec.name,
                "nodes": len(nodes), "quorate": int(online * 2 > len(nodes)), "version": 7,
            })
        for i, n in enumerate(nodes, start=1):
            out.append({
                "id": f"node/{n.name}", "type": "node", "name": n.name, "nodeid": i,
                "ip": f"192.168.10.{10 + i}", "online": int(n.online), "local": int(i == 1),
                "level": "",
            })
        return out

    def _node_status(self, node: str) -> dict[str, Any]:
        n = next((x for x in self.spec.nodes if x.name == node), None)
        if n is None:
            raise PveError(500, f"hostname lookup '{node}' failed", f"/nodes/{node}/status")
        if not n.online:
            raise PveError(595, "No route to host", f"/nodes/{node}/status")
        cpu = self.node_cpu(n)
        load = cpu * n.cpus
        return {
            "cpu": cpu,
            "wait": 0.002 + 0.03 * self._noise(node, "iowait"),
            "loadavg": [f"{load:.2f}", f"{load * 0.9:.2f}", f"{load * 0.8:.2f}"],
            "uptime": int(self.clock() - self.started),
            "kversion": "Linux 6.14.11-4-pve #1 SMP PREEMPT_DYNAMIC PMX 6.14.11-4",
            "pveversion": "pve-manager/9.0.10/deadbeef",
            "current-kernel": {"release": "6.14.11-4-pve", "machine": "x86_64"},
            "cpuinfo": {
                "model": n.cpu_model, "sockets": 1, "cores": n.cpus // 2, "cpus": n.cpus,
                "mhz": "3800.000", "hvm": "1",
            },
            "memory": {"total": n.mem, "used": self.node_mem(n),
                       "free": n.mem - self.node_mem(n)},
            "swap": {"total": 8 * GiB, "used": int(0.05 * GiB * (1 + self._noise(node))),
                     "free": 7 * GiB},
            "rootfs": {"total": 100 * GiB, "used": 12 * GiB, "avail": 88 * GiB},
            "boot-info": {"mode": "efi", "secureboot": 0},
        }
