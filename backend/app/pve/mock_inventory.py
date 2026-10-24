"""Synthetic inventory endpoints for the mock cluster (disks, NICs, PCI, guest configs)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.pve.client import PveError

if TYPE_CHECKING:
    from app.pve.mock import MockGuest, MockPveClient

GiB = 1024**3
TB = 1000**4

# Per-node hardware. Names match the mock specs in app.pve.mock.
DISKS: dict[str, list[dict[str, Any]]] = {
    "pve1": [
        {"devpath": "/dev/nvme0n1", "model": "Samsung SSD 990 PRO 2TB", "serial": "S7KHNJ0W100001",
         "size": 2 * TB, "type": "nvme", "health": "PASSED", "wearout": 97, "used": "ZFS"},
        {"devpath": "/dev/nvme1n1", "model": "Samsung SSD 990 PRO 2TB", "serial": "S7KHNJ0W100002",
         "size": 2 * TB, "type": "nvme", "health": "PASSED", "wearout": 96, "used": "ZFS"},
        {"devpath": "/dev/sda", "model": "WDC WD120EFBX-68B0EN0", "serial": "5QG1ABCD",
         "vendor": "ATA", "size": 12 * TB, "type": "hdd", "rpm": 7200, "health": "PASSED",
         "wearout": "N/A", "used": "passthrough (HBA)"},
    ],
    "pve2": [
        {"devpath": "/dev/nvme0n1", "model": "WD_BLACK SN850X 2000GB", "serial": "23170A800001",
         "size": 2 * TB, "type": "nvme", "health": "PASSED", "wearout": 91, "used": "ZFS"},
        {"devpath": "/dev/sda", "model": "Crucial_CT500MX500SSD1", "serial": "2111E5A00001",
         "vendor": "ATA", "size": 500 * 1000**3, "type": "ssd", "health": "PASSED",
         "wearout": 68, "used": "ZFS"},
    ],
    "pve3": [
        {"devpath": "/dev/nvme0n1", "model": "KINGSTON SNV2S1000G", "serial": "50026B7686000001",
         "size": TB, "type": "nvme", "health": "PASSED", "wearout": 88, "used": "ZFS"},
        {"devpath": "/dev/sda", "model": "Samsung SSD 870 EVO 500GB", "serial": "S62ANZ0R000001",
         "vendor": "ATA", "size": 500 * 1000**3, "type": "ssd", "health": "FAILED",
         "wearout": 41, "used": "ZFS"},
    ],
    "edge": [
        {"devpath": "/dev/nvme0n1", "model": "Lexar SSD NM620 512GB", "serial": "NLC1234500001",
         "size": 512 * 1000**3, "type": "nvme", "health": "PASSED", "wearout": 99, "used": "LVM"},
    ],
}

PCI_COMMON = [
    {"id": "0000:00:00.0", "class": "0x060000", "vendor_name": "Advanced Micro Devices, Inc. [AMD]",
     "device_name": "Renoir/Cezanne Root Complex", "iommugroup": 0},
    {"id": "0000:00:14.0", "class": "0x0c0330", "vendor_name": "Advanced Micro Devices, Inc. [AMD]",
     "device_name": "FCH USB XHCI Controller", "iommugroup": 5},
    {"id": "0000:02:00.0", "class": "0x010802", "vendor_name": "Samsung Electronics Co Ltd",
     "device_name": "NVMe SSD Controller PM9A1/PM9A3/980PRO", "iommugroup": 7},
    {"id": "0000:05:00.0", "class": "0x020000", "vendor_name": "Intel Corporation",
     "device_name": "Ethernet Controller I226-V", "iommugroup": 9},
    {"id": "0000:06:00.0", "class": "0x020000", "vendor_name": "Intel Corporation",
     "device_name": "Ethernet Controller X710 for 10GbE SFP+", "iommugroup": 10},
]
PCI_EXTRA: dict[str, list[dict[str, Any]]] = {
    "pve1": [{"id": "0000:03:00.0", "class": "0x010700", "vendor_name": "Broadcom / LSI",
              "device_name": "SAS2308 PCI-Express Fusion-MPT SAS-2", "iommugroup": 12}],
    "pve2": [
        {"id": "0000:01:00.0", "class": "0x030000", "vendor_name": "NVIDIA Corporation",
         "device_name": "AD106 [GeForce RTX 4060 Ti]", "subsystem_vendor_name": "Gigabyte",
         "iommugroup": 13},
        {"id": "0000:01:00.1", "class": "0x040300", "vendor_name": "NVIDIA Corporation",
         "device_name": "AD106M High Definition Audio Controller", "iommugroup": 13},
    ],
    "pve3": [{"id": "0000:00:02.0", "class": "0x030000", "vendor_name": "Intel Corporation",
              "device_name": "AlderLake-S GT1 [UHD Graphics 770]", "iommugroup": 1}],
    "edge": [],
}

PCI_MAPPINGS = [
    {"id": "gpu-nvidia", "description": "RTX 4060 Ti",
     "map": ["node=pve2,path=0000:01:00.0;0000:01:00.1,id=10de:2803,iommugroup=13"]},
]

# Guest specifics: (os, agent, extra config)
QEMU_EXTRA: dict[str, dict[str, Any]] = {
    "homeassistant": {"ostype": "l26", "bios": "ovmf", "machine": "q35",
                      "efidisk0": "{s}:vm-{id}-disk-1,efitype=4m,size=1M", "os": "Home Assistant OS 16.2",
                      "usb0": "host=10c4:ea60"},
    "win11-desk": {"ostype": "win11", "bios": "ovmf", "machine": "pc-q35-9.2",
                   "efidisk0": "{s}:vm-{id}-disk-1,efitype=4m,pre-enrolled-keys=1,size=1M",
                   "tpmstate0": "{s}:vm-{id}-disk-2,size=4M,version=v2.0",
                   "hostpci0": "mapping=gpu-nvidia,pcie=1,x-vga=1", "os": "Microsoft Windows 11 Pro",
                   "ide2": "local:iso/virtio-win-0.1.271.iso,media=cdrom,size=709M"},
    "opnsense": {"ostype": "other", "os": "FreeBSD 14.3", "net1": "virtio={mac2},bridge=vmbr1"},
    "truenas": {"ostype": "l26", "agent": "0", "hostpci0": "0000:03:00.0,pcie=0"},
    "debian-tpl": {"template": 1},
}
LXC_EXTRA: dict[str, dict[str, Any]] = {
    "jellyfin": {"dev0": "/dev/dri/renderD128,gid=104", "mp0": "/mnt/media,mp=/media"},
    "paperless": {"mp0": "{s}:subvol-{id}-disk-1,mp=/data,size=50G"},
    "pihole": {"ostype": "alpine"},
}


def _mac(vmid: int, idx: int = 0) -> str:
    return f"BC:24:11:{(vmid >> 8) & 0xFF:02X}:{vmid & 0xFF:02X}:{idx:02X}"


def _ip(g: MockGuest) -> str:
    return f"192.168.10.{g.vmid - 100 + 100 if g.vmid < 200 else g.vmid - 200 + 150}"


def _local_storage(mock: MockPveClient) -> str:
    names = [s.storage for s in mock.spec.storages]
    return "local-zfs" if "local-zfs" in names else "local-lvm"


def qemu_config(mock: MockPveClient, g: MockGuest) -> dict[str, Any]:
    extra = dict(QEMU_EXTRA.get(g.name, {}))
    s = _local_storage(mock)
    vlan = 20 if "k8s" in g.tags else None
    cfg: dict[str, Any] = {
        "name": g.name, "cores": max(1, g.cpus // (2 if g.cpus >= 4 else 1)),
        "sockets": 2 if g.cpus >= 4 else 1, "memory": str(g.maxmem // 1024**2),
        "cpu": "host" if g.cpus >= 4 else "x86-64-v2-AES", "ostype": "l26",
        "scsihw": "virtio-scsi-single", "boot": "order=scsi0;net0",
        "scsi0": f"{s}:vm-{g.vmid}-disk-0,iothread=1,discard=on,size={g.maxdisk // GiB}G",
        "net0": f"virtio={_mac(g.vmid)},bridge=vmbr0,firewall=1" + (f",tag={vlan}" if vlan else ""),
        "agent": "1,fstrim_cloned_disks=1", "onboot": 1 if g.status == "running" else 0,
        "description": f"# {g.name}\nVerwaltet per Ansible.\n" if g.vmid % 3 == 0 else "",
        "balloon": str(g.maxmem // 1024**2 // 2) if g.maxmem >= 8 * GiB else "0",
        "digest": "0" * 40,
    }
    if g.tags:
        cfg["tags"] = g.tags
    for k, v in extra.items():
        if k == "os":
            continue
        cfg[k] = v.format(s=s, id=g.vmid, mac2=_mac(g.vmid, 1)) if isinstance(v, str) else v
    return cfg


def lxc_config(mock: MockPveClient, g: MockGuest) -> dict[str, Any]:
    s = _local_storage(mock)
    cfg: dict[str, Any] = {
        "hostname": g.name, "cores": g.cpus, "memory": str(int(g.maxmem // 1024**2)),
        "swap": "512", "arch": "amd64", "ostype": "debian", "unprivileged": 1,
        "features": "nesting=1", "onboot": 1 if g.status == "running" else 0,
        "rootfs": f"{s}:subvol-{g.vmid}-disk-0,size={g.maxdisk // GiB}G",
        "net0": f"name=eth0,bridge=vmbr0,hwaddr={_mac(g.vmid)},ip=dhcp,type=veth,firewall=1",
        "digest": "0" * 40,
    }
    if g.tags:
        cfg["tags"] = g.tags
    for k, v in LXC_EXTRA.get(g.name, {}).items():
        cfg[k] = v.format(s=s, id=g.vmid) if isinstance(v, str) else v
    return cfg


def network(node: str) -> list[dict[str, Any]]:
    idx = {"pve1": 11, "pve2": 12, "pve3": 13, "edge": 30}.get(node, 99)
    ifaces = [
        {"iface": "enp5s0", "type": "eth", "active": 1, "autostart": 1, "method": "manual"},
        {"iface": "enp6s0f0", "type": "eth", "active": 1, "autostart": 1, "method": "manual",
         "mtu": 9000},
        {"iface": "enp6s0f1", "type": "eth", "active": 1, "autostart": 1, "method": "manual",
         "mtu": 9000},
        {"iface": "bond0", "type": "bond", "active": 1, "autostart": 1, "method": "manual",
         "slaves": "enp6s0f0 enp6s0f1", "bond_mode": "802.3ad", "mtu": 9000},
        {"iface": "vmbr0", "type": "bridge", "active": 1, "autostart": 1, "method": "static",
         "cidr": f"192.168.10.{idx}/24", "address": f"192.168.10.{idx}", "netmask": "24",
         "gateway": "192.168.10.1", "bridge_ports": "enp5s0", "bridge_vlan_aware": 1,
         "comments": "LAN / VM-Netz"},
        {"iface": "vmbr1", "type": "bridge", "active": 1, "autostart": 1, "method": "manual",
         "bridge_ports": "bond0", "bridge_vlan_aware": 1, "mtu": 9000, "comments": "Trunk"},
        {"iface": "vmbr0.50", "type": "vlan", "active": 1, "autostart": 1, "method": "static",
         "cidr": f"10.50.0.{idx}/24", "address": f"10.50.0.{idx}", "netmask": "24",
         "vlan-id": 50, "vlan-raw-device": "vmbr0", "comments": "Corosync / Migration"},
    ]
    if node == "edge":
        ifaces = [i for i in ifaces if i["iface"] in ("enp5s0", "vmbr0")]
    return ifaces


def smart(disk: dict[str, Any]) -> dict[str, Any]:
    if disk["type"] == "nvme":
        text = (f"SMART/Health Information (NVMe Log 0x02)\nCritical Warning: 0x00\n"
                f"Temperature: 41 Celsius\nAvailable Spare: 100%\n"
                f"Percentage Used: {100 - int(disk.get('wearout', 100))}%\n"
                f"Data Units Written: 48,123,456 [24.6 TB]\nPower On Hours: 9,876\n"
                f"Unsafe Shutdowns: 12\nMedia and Data Integrity Errors: 0\n")
        return {"health": disk["health"], "type": "text", "text": text}
    failed = disk["health"] == "FAILED"
    return {"health": disk["health"], "type": "ata", "attributes": [
        {"id": "  5", "name": "Reallocated_Sector_Ct", "value": 1 if failed else 100,
         "worst": 1 if failed else 100, "threshold": 10, "raw": "2944" if failed else "0",
         "fail": "FAILING_NOW" if failed else "-", "flags": "PO--CK"},
        {"id": "  9", "name": "Power_On_Hours", "value": 92, "worst": 92, "threshold": 0,
         "raw": "31233", "fail": "-", "flags": "-O--CK"},
        {"id": "194", "name": "Temperature_Celsius", "value": 64, "worst": 50, "threshold": 0,
         "raw": "36", "fail": "-", "flags": "-O---K"},
        {"id": "199", "name": "UDMA_CRC_Error_Count", "value": 100, "worst": 100, "threshold": 0,
         "raw": "0", "fail": "-", "flags": "-O--CK"},
    ]}


def handle(mock: MockPveClient, path: str, params: dict[str, Any]) -> Any:
    """Return data for inventory paths, or raise ``LookupError`` if the path is not ours."""
    if path == "/cluster/mapping/pci":
        return PCI_MAPPINGS if mock.spec.name == "homelab" else []
    m = re.fullmatch(r"/nodes/([^/]+)/(disks/list|disks/smart|network|hardware/pci)", path)
    if m:
        node, what = m.groups()
        if node not in {n.name for n in mock.spec.nodes}:
            raise PveError(500, f"hostname lookup '{node}' failed", path)
        if what == "disks/list":
            return DISKS.get(node, [])
        if what == "disks/smart":
            disk = next((d for d in DISKS.get(node, []) if d["devpath"] == params.get("disk")), None)
            if disk is None:
                raise PveError(400, "Parameter verification failed: disk", path)
            return smart(disk)
        if what == "network":
            return network(node)
        return PCI_COMMON + PCI_EXTRA.get(node, [])

    m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/(config|agent/[\w-]+|interfaces)", path)
    if not m:
        raise LookupError(path)
    node, gtype, vmid, what = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    g = next((x for x in mock.spec.guests if x.vmid == vmid and x.type == gtype), None)
    if g is None or g.node != node:
        raise PveError(500, f"Configuration file 'nodes/{node}/{gtype}-server/{vmid}.conf' "
                            "does not exist", path)
    if what == "config":
        return qemu_config(mock, g) if gtype == "qemu" else lxc_config(mock, g)
    if g.status != "running":
        raise PveError(500, f"VM {vmid} is not running", path)
    if what == "interfaces":
        return [{"name": "lo", "hwaddr": "00:00:00:00:00:00", "inet": "127.0.0.1/8"},
                {"name": "eth0", "hwaddr": _mac(vmid), "inet": f"{_ip(g)}/24",
                 "inet6": f"fd00::{vmid:x}/64"}]
    cfg = qemu_config(mock, g)
    if not str(cfg.get("agent", "0")).startswith("1"):
        raise PveError(500, "No QEMU guest agent configured", path)
    if what == "agent/get-osinfo":
        os_name = QEMU_EXTRA.get(g.name, {}).get("os", "Debian GNU/Linux 13 (trixie)")
        os_id = ("mswindows" if "Windows" in os_name else "freebsd" if "FreeBSD" in os_name
                 else "haos" if "Home Assistant" in os_name else "debian")
        return {"result": {"pretty-name": os_name, "name": os_name.split()[0], "id": os_id,
                           "kernel-release": "6.12.48+deb13-amd64"}}
    if what == "agent/network-get-interfaces":
        return {"result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "prefix": 8}]},
            {"name": "ens18", "hardware-address": _mac(vmid).lower(), "ip-addresses": [
                {"ip-address": _ip(g), "ip-address-type": "ipv4", "prefix": 24},
                {"ip-address": f"fe80::be24:11ff:fe00:{vmid:x}", "ip-address-type": "ipv6",
                 "prefix": 64}]},
        ]}
    raise PveError(501, "Not implemented in mock", path)
