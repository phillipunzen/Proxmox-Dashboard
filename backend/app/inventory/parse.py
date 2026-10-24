"""Parsers for raw PVE responses and guest config strings. Pure functions, unit-tested."""

from __future__ import annotations

import re
from typing import Any

from app.inventory.models import (
    Disk,
    GuestDisk,
    GuestInventory,
    GuestNic,
    HostPci,
    NetIface,
    PciDevice,
)

_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$", re.I)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
_QEMU_DISK = re.compile(r"^(scsi|virtio|sata|ide)\d+$|^(efidisk|tpmstate)0$|^unused\d+$")
_LXC_DISK = re.compile(r"^rootfs$|^mp\d+$|^unused\d+$")
_PCI_SHORT = re.compile(r"^(?:([0-9a-f]{4}):)?([0-9a-f]{2}:[0-9a-f]{2})(?:\.([0-7]))?$", re.I)

# First byte of the PCI class code -> readable name.
PCI_CLASSES = {
    "00": "Unklassifiziert", "01": "Massenspeicher", "02": "Netzwerk", "03": "Grafik",
    "04": "Multimedia", "05": "Speicher-Controller", "06": "Bridge", "07": "Kommunikation",
    "08": "System", "09": "Eingabe", "0c": "Serieller Bus (USB/SMBus)", "0d": "Wireless",
    "10": "Verschlüsselung", "11": "Signalverarbeitung", "12": "Beschleuniger",
}


def parse_size(value: str | None) -> int | None:
    """``32G`` -> bytes. PVE uses binary units."""
    if not value:
        return None
    m = _SIZE.match(value.strip())
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def split_options(value: str) -> tuple[str | None, dict[str, str]]:
    """``vol,key=val,flag`` -> (``vol``, {...}). The first item is positional if it has no '='."""
    head: str | None = None
    opts: dict[str, str] = {}
    for i, part in enumerate(p for p in str(value).split(",") if p != ""):
        if "=" in part:
            k, v = part.split("=", 1)
            opts[k.strip()] = v.strip()
        elif i == 0:
            head = part.strip()
        else:
            opts[part.strip()] = "1"
    return head, opts


def _bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- node -----------------------------------------------------------------------------------


def parse_disks(raw: list[dict[str, Any]]) -> list[Disk]:
    disks = []
    for d in raw or []:
        wear = d.get("wearout")
        wear_i = _int(wear)
        disks.append(Disk(
            devpath=d.get("devpath", "?"),
            model=d.get("model") or None,
            vendor=(d.get("vendor") or "").strip() or None,
            serial=d.get("serial") or None,
            wwn=d.get("wwn") or None,
            size=_int(d.get("size")) or 0,
            type=d.get("type") or "unknown",
            rpm=_int(d.get("rpm")) or None,
            health=d.get("health") or None,
            wearout_used=100 - wear_i if wear_i is not None else None,
            used_by=d.get("used") or None,
        ))
    return sorted(disks, key=lambda x: x.devpath)


def parse_network(raw: list[dict[str, Any]]) -> list[NetIface]:
    out = []
    for n in raw or []:
        out.append(NetIface(
            iface=n.get("iface", "?"),
            type=n.get("type", "unknown"),
            active=_bool(n.get("active")),
            autostart=_bool(n.get("autostart")),
            method=n.get("method"),
            cidr=n.get("cidr") or (f"{n['address']}/{n['netmask']}"
                                   if n.get("address") and n.get("netmask") else n.get("address")),
            gateway=n.get("gateway"),
            cidr6=n.get("cidr6"),
            bridge_ports=str(n.get("bridge_ports") or "").split(),
            bridge_vlan_aware=_bool(n.get("bridge_vlan_aware")),
            slaves=str(n.get("slaves") or "").split(),
            bond_mode=n.get("bond_mode"),
            vlan_id=_int(n.get("vlan-id")),
            vlan_raw_device=n.get("vlan-raw-device"),
            mtu=_int(n.get("mtu")),
            comments=(n.get("comments") or "").strip() or None,
        ))
    order = {"eth": 0, "bond": 1, "bridge": 2, "OVSBridge": 2, "vlan": 3}
    return sorted(out, key=lambda i: (order.get(i.type, 9), i.iface))


def parse_pci(raw: list[dict[str, Any]]) -> list[PciDevice]:
    out = []
    for p in raw or []:
        cls = str(p.get("class") or "")
        code = cls[2:4].lower() if cls.startswith("0x") else None
        sub = " ".join(x for x in (p.get("subsystem_vendor_name"),
                                   p.get("subsystem_device_name")) if x)
        group = _int(p.get("iommugroup"))
        out.append(PciDevice(
            id=p.get("id", "?"), class_code=cls or None,
            class_name=PCI_CLASSES.get(code or "", "Sonstige") if code else None,
            vendor_name=p.get("vendor_name"), device_name=p.get("device_name"),
            subsystem=sub or None,
            iommu_group=group if group is not None and group >= 0 else None,
            mdev=_bool(p.get("mdev")),
        ))
    return sorted(out, key=lambda d: d.id)


# --- guests -----------------------------------------------------------------------------------


def expand_pci_ids(spec: str, known: list[str]) -> list[str]:
    """``01:00`` / ``0000:01:00`` / ``0000:01:00.0`` -> full ids (all functions if none given)."""
    out: list[str] = []
    for part in spec.split(";"):
        m = _PCI_SHORT.match(part.strip())
        if not m:
            continue
        domain = (m.group(1) or "0000").lower()
        base = f"{domain}:{m.group(2).lower()}"
        if m.group(3) is not None:
            out.append(f"{base}.{m.group(3)}")
        else:
            funcs = [k for k in known if k.lower().startswith(base + ".")]
            out.extend(funcs or [f"{base}.0"])
    return out


def _guest_disk(key: str, value: str, lxc: bool) -> GuestDisk:
    head, opts = split_options(value)
    volume = head or opts.pop("volume", "") or opts.pop("file", "")
    storage = volume.split(":", 1)[0] if ":" in volume and not volume.startswith("/") else None
    return GuestDisk(
        key=key, storage=storage, volume=volume,
        size=parse_size(opts.pop("size", None)),
        media=opts.pop("media", None),
        mountpoint="/" if key == "rootfs" else (opts.pop("mp", None) if lxc else None),
        options=opts,
    )


def _qemu_nic(key: str, value: str) -> GuestNic:
    _, opts = split_options(value)
    model = mac = None
    for m in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "i82551", "i82557b", "i82559er",
              "ne2k_pci", "ne2k_isa", "pcnet"):
        if m in opts:
            model, mac = m, opts.pop(m)
            break
    else:
        model = opts.pop("model", None)
        mac = opts.pop("macaddr", None)
    rate = opts.get("rate")
    return GuestNic(key=key, model=model, mac=mac, bridge=opts.get("bridge"),
                    vlan=_int(opts.get("tag")), firewall=_bool(opts.get("firewall")),
                    rate_mbps=float(rate) * 8 if rate else None)  # PVE rate is MB/s


def _lxc_nic(key: str, value: str) -> GuestNic:
    _, opts = split_options(value)
    ips = ", ".join(f"{k}={opts[k]}" for k in ("ip", "ip6") if opts.get(k))
    rate = opts.get("rate")
    return GuestNic(key=key, model=opts.get("type", "veth"), mac=opts.get("hwaddr"),
                    bridge=opts.get("bridge"), vlan=_int(opts.get("tag")),
                    firewall=_bool(opts.get("firewall")), name=opts.get("name"),
                    ip_config=ips or None, rate_mbps=float(rate) * 8 if rate else None)


def apply_guest_config(inv: GuestInventory, cfg: dict[str, Any], known_pci: list[str],
                       pci_mappings: dict[str, list[str]]) -> None:
    """Fill a :class:`GuestInventory` from ``/nodes/{n}/{qemu|lxc}/{vmid}/config``."""
    lxc = inv.type == "lxc"
    inv.cores = _int(cfg.get("cores")) or (None if lxc else 1)
    inv.sockets = None if lxc else (_int(cfg.get("sockets")) or 1)
    inv.vcpus = _int(cfg.get("vcpus")) or (
        (inv.cores or 0) * (inv.sockets or 1) if inv.cores else None)
    if cfg.get("cpulimit"):
        inv.cpu_limit = float(cfg["cpulimit"])
    mem = _int(cfg.get("memory"))
    inv.memory = mem * 1024**2 if mem is not None else (None if lxc else 512 * 1024**2)
    if "balloon" in cfg:
        inv.balloon_min = (_int(cfg["balloon"]) or 0) * 1024**2
    if lxc:
        swap = _int(cfg.get("swap"))
        inv.swap = swap * 1024**2 if swap is not None else None
        inv.arch = cfg.get("arch")
        inv.unprivileged = _bool(cfg.get("unprivileged"))
    else:
        cpu_head, cpu_opts = split_options(str(cfg.get("cpu", "kvm64")))
        inv.cpu_type = cpu_head or cpu_opts.get("cputype") or "kvm64"
        inv.bios = cfg.get("bios", "seabios")
        machine = str(cfg.get("machine") or "")
        inv.machine = machine or "i440fx"
        agent_head, agent_opts = split_options(str(cfg.get("agent", "0")))
        inv.agent_enabled = _bool(agent_head or agent_opts.get("enabled", "0"))
    inv.ostype = cfg.get("ostype")
    inv.onboot = _bool(cfg.get("onboot"))
    inv.protection = _bool(cfg.get("protection"))
    inv.template = _bool(cfg.get("template")) or inv.template
    inv.tags = [t for t in str(cfg.get("tags", "")).replace(",", ";").split(";") if t]
    inv.notes = (cfg.get("description") or "").strip() or None

    disk_re = _LXC_DISK if lxc else _QEMU_DISK
    disks, nics, pcis, usb = [], [], [], []
    for key in sorted(cfg, key=_natural_key):
        value = str(cfg[key])
        if disk_re.match(key):
            disks.append(_guest_disk(key, value, lxc))
        elif re.match(r"^net\d+$", key):
            nics.append(_lxc_nic(key, value) if lxc else _qemu_nic(key, value))
        elif re.match(r"^hostpci\d+$", key):
            head, opts = split_options(value)
            mapping = opts.pop("mapping", None)
            if head is None and "host" in opts:
                head = opts.pop("host")
            devices = (pci_mappings.get(mapping, []) if mapping
                       else expand_pci_ids(head or "", known_pci))
            pcis.append(HostPci(key=key, devices=devices, mapping=mapping, options=opts))
        elif re.match(r"^usb\d+$", key) or (lxc and re.match(r"^dev\d+$", key)):
            usb.append(f"{key}: {value}")  # USB (VM) / device passthrough (LXC)
    inv.disks, inv.nics, inv.hostpci, inv.usb = disks, nics, pcis, usb


def _natural_key(key: str) -> tuple[int, str, int]:
    """rootfs first, then by name with numeric suffixes in numeric order (net2 < net10)."""
    m = re.match(r"^([a-z]+)(\d*)$", key)
    if not m:
        return (1, key, -1)
    return (0 if key == "rootfs" else 1, m.group(1), int(m.group(2) or -1))


def parse_mappings(raw: list[dict[str, Any]], node: str) -> dict[str, list[str]]:
    """``/cluster/mapping/pci`` -> {mapping id: [pci ids on this node]}."""
    out: dict[str, list[str]] = {}
    for m in raw or []:
        ids: list[str] = []
        for entry in m.get("map") or []:
            _, opts = split_options(entry)
            if opts.get("node") == node and opts.get("path"):
                ids.extend(expand_pci_ids(opts["path"], []))
        out[m.get("id", "?")] = ids
    return out


def agent_ips(raw: dict[str, Any] | list[Any] | None) -> list[str]:
    """IPs from QGA ``network-get-interfaces`` (skips loopback and link-local)."""
    items = raw.get("result", []) if isinstance(raw, dict) else (raw or [])
    ips: list[str] = []
    for iface in items:
        if iface.get("name") == "lo":
            continue
        for a in iface.get("ip-addresses") or []:
            ip = a.get("ip-address", "")
            if ip and not ip.startswith(("127.", "fe80", "::1", "169.254.")):
                ips.append(ip)
    return ips


def lxc_ips(raw: list[dict[str, Any]] | None) -> list[str]:
    ips: list[str] = []
    for iface in raw or []:
        if iface.get("name") == "lo":
            continue
        for key in ("inet", "inet6"):
            for ip in str(iface.get(key) or "").split():
                addr = ip.split("/")[0]
                if addr and not addr.startswith(("127.", "fe80", "::1")):
                    ips.append(addr)
    return ips
