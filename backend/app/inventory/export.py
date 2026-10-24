"""Flatten the inventory into tables for CSV/JSON export."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from typing import Any, Literal

from app.inventory.models import ClusterInventory

ExportKind = Literal["nodes", "disks", "network", "pci", "guests", "guest_disks", "guest_nics"]

GiB = 1024**3


def _gib(v: int | None) -> float | None:
    return round(v / GiB, 2) if v is not None else None


def _nodes(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": n.cluster, "node": n.node, "online": n.online, "cpu_model": n.cpu_model,
        "sockets": n.cpu_sockets, "cores": n.cpu_cores, "threads": n.cpu_threads,
        "mem_gib": _gib(n.mem_total), "pve_version": n.pve_version, "kernel": n.kernel,
        "boot_mode": n.boot_mode, "secure_boot": n.secure_boot, "mainboard": n.mainboard,
        "disks": len(n.disks), "pci_devices": len(n.pci),
    } for n in inv.nodes]


def _disks(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": n.cluster, "node": n.node, "devpath": d.devpath, "type": d.type,
        "model": d.model, "serial": d.serial, "size_gb": round(d.size / 1000**3, 1),
        "health": d.health, "wearout_used_pct": d.wearout_used, "rpm": d.rpm, "used_by": d.used_by,
    } for n in inv.nodes for d in n.disks]


def _network(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": n.cluster, "node": n.node, "iface": i.iface, "type": i.type, "active": i.active,
        "cidr": i.cidr, "gateway": i.gateway, "bridge_ports": " ".join(i.bridge_ports),
        "vlan_aware": i.bridge_vlan_aware, "slaves": " ".join(i.slaves), "bond_mode": i.bond_mode,
        "vlan_id": i.vlan_id, "vlan_raw_device": i.vlan_raw_device, "mtu": i.mtu,
        "comments": i.comments,
    } for n in inv.nodes for i in n.network]


def _pci(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": n.cluster, "node": n.node, "id": p.id, "class": p.class_name,
        "vendor": p.vendor_name, "device": p.device_name, "iommu_group": p.iommu_group,
        "assigned_to": ", ".join(f"{a.vmid} {a.name} ({a.key})" for a in p.assigned_to),
    } for n in inv.nodes for p in n.pci]


def _guests(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": g.cluster, "vmid": g.vmid, "name": g.name, "type": g.type, "node": g.node,
        "status": g.status, "template": g.template, "vcpus": g.vcpus, "cores": g.cores,
        "sockets": g.sockets, "cpu_type": g.cpu_type, "mem_gib": _gib(g.memory),
        "balloon_min_gib": _gib(g.balloon_min), "disk_total_gib": _gib(
            sum(d.size or 0 for d in g.disks if d.media != "cdrom")),
        "bios": g.bios, "machine": g.machine, "ostype": g.ostype, "os_name": g.os_name,
        "agent": g.agent_enabled, "agent_running": g.agent_running,
        "ips": " ".join(g.ip_addresses),
        "bridges": " ".join(sorted({n.bridge for n in g.nics if n.bridge})),
        "passthrough": " ".join(d for p in g.hostpci for d in p.devices),
        "onboot": g.onboot, "tags": ";".join(g.tags), "notes": (g.notes or "").replace("\n", " "),
    } for g in inv.guests]


def _guest_disks(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": g.cluster, "vmid": g.vmid, "name": g.name, "key": d.key, "storage": d.storage,
        "volume": d.volume, "size_gib": _gib(d.size), "media": d.media, "mountpoint": d.mountpoint,
    } for g in inv.guests for d in g.disks]


def _guest_nics(inv: ClusterInventory) -> list[dict[str, Any]]:
    return [{
        "cluster": g.cluster, "vmid": g.vmid, "name": g.name, "key": n.key, "model": n.model,
        "mac": n.mac, "bridge": n.bridge, "vlan": n.vlan, "firewall": n.firewall,
        "rate_mbps": n.rate_mbps, "ip_config": n.ip_config,
    } for g in inv.guests for n in g.nics]


TABLES: dict[str, Callable[[ClusterInventory], list[dict[str, Any]]]] = {
    "nodes": _nodes, "disks": _disks, "network": _network, "pci": _pci,
    "guests": _guests, "guest_disks": _guest_disks, "guest_nics": _guest_nics,
}


def rows(kind: str, inventories: list[ClusterInventory]) -> list[dict[str, Any]]:
    return [row for inv in inventories for row in TABLES[kind](inv)]


def to_csv(data: list[dict[str, Any]]) -> str:
    """Semicolon-separated with BOM, so Excel (de-DE) opens it correctly."""
    buf = io.StringIO()
    buf.write("﻿")
    if not data:
        return buf.getvalue()
    writer = csv.DictWriter(buf, fieldnames=list(data[0].keys()), delimiter=";",
                            lineterminator="\n")
    writer.writeheader()
    for row in data:
        writer.writerow({k: _cell(v) for k, v in row.items()})
    return buf.getvalue()


def _cell(v: Any) -> Any:
    if isinstance(v, bool):
        return "ja" if v else "nein"
    if isinstance(v, float):
        return f"{v:.2f}".replace(".", ",")
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v  # neutralise spreadsheet formula injection from notes/names
    return "" if v is None else v
