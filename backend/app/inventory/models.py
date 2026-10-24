"""Hardware and configuration inventory models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Disk(BaseModel):
    devpath: str
    model: str | None = None
    vendor: str | None = None
    serial: str | None = None
    wwn: str | None = None
    size: int = 0
    type: str = "unknown"  # ssd | hdd | nvme | usb | unknown
    rpm: int | None = None
    health: str | None = None  # PASSED / OK / FAILED / UNKNOWN
    # PVE reports remaining life in %; we expose the *used* percentage like the PVE GUI does.
    wearout_used: int | None = None
    used_by: str | None = None  # LVM, ZFS, partitions, mounted, ...


class NetIface(BaseModel):
    iface: str
    type: str  # eth, bridge, bond, vlan, OVSBridge, ...
    active: bool = False
    autostart: bool = False
    method: str | None = None
    cidr: str | None = None
    gateway: str | None = None
    cidr6: str | None = None
    bridge_ports: list[str] = []
    bridge_vlan_aware: bool = False
    slaves: list[str] = []
    bond_mode: str | None = None
    vlan_id: int | None = None
    vlan_raw_device: str | None = None
    mtu: int | None = None
    comments: str | None = None


class PassthroughRef(BaseModel):
    cluster: str
    vmid: int
    name: str
    key: str  # hostpci0, ...


class PciDevice(BaseModel):
    id: str  # 0000:01:00.0
    class_code: str | None = None
    class_name: str | None = None
    vendor_name: str | None = None
    device_name: str | None = None
    subsystem: str | None = None
    iommu_group: int | None = None
    mdev: bool = False
    assigned_to: list[PassthroughRef] = []


class NodeHardware(BaseModel):
    cluster: str
    node: str
    online: bool
    cpu_model: str | None = None
    cpu_sockets: int | None = None
    cpu_cores: int | None = None
    cpu_threads: int | None = None
    cpu_mhz: float | None = None
    mem_total: int | None = None
    kernel: str | None = None
    pve_version: str | None = None
    boot_mode: str | None = None
    secure_boot: bool | None = None
    # Not exposed by the PVE API; filled via SSH (dmidecode) in a later phase.
    mainboard: str | None = None
    disks: list[Disk] = []
    network: list[NetIface] = []
    pci: list[PciDevice] = []
    collected_at: datetime | None = None
    errors: list[str] = []


class GuestDisk(BaseModel):
    key: str  # scsi0, virtio1, rootfs, mp0, efidisk0, unused0 ...
    storage: str | None = None  # None for bind mounts / passthrough devices
    volume: str
    size: int | None = None
    media: str | None = None  # cdrom
    mountpoint: str | None = None  # LXC mount points
    options: dict[str, str] = {}


class GuestNic(BaseModel):
    key: str  # net0 ...
    model: str | None = None  # virtio, e1000, veth ...
    mac: str | None = None
    bridge: str | None = None
    vlan: int | None = None
    firewall: bool = False
    rate_mbps: float | None = None
    name: str | None = None  # LXC interface name (eth0)
    ip_config: str | None = None  # LXC ip=, ip6=


class HostPci(BaseModel):
    key: str
    devices: list[str] = []  # resolved PCI ids (0000:01:00.0)
    mapping: str | None = None
    options: dict[str, str] = {}


class GuestInventory(BaseModel):
    cluster: str
    vmid: int
    name: str
    type: str
    node: str
    status: str
    template: bool = False
    cores: int | None = None
    sockets: int | None = None
    vcpus: int | None = None
    cpu_type: str | None = None
    cpu_limit: float | None = None
    memory: int | None = None
    balloon_min: int | None = None  # None = not configured, 0 = disabled
    swap: int | None = None
    disks: list[GuestDisk] = []
    nics: list[GuestNic] = []
    hostpci: list[HostPci] = []
    usb: list[str] = []
    bios: str | None = None
    machine: str | None = None
    ostype: str | None = None
    arch: str | None = None
    unprivileged: bool | None = None
    agent_enabled: bool | None = None
    agent_running: bool | None = None
    os_name: str | None = None
    ip_addresses: list[str] = []
    onboot: bool = False
    protection: bool = False
    tags: list[str] = []
    notes: str | None = None
    collected_at: datetime | None = None
    error: str | None = None


class ClusterInventory(BaseModel):
    cluster: str
    nodes: list[NodeHardware] = []
    guests: list[GuestInventory] = []
    collected_at: datetime | None = None
    duration_s: float | None = None
