"""Normalised views of cluster state, as served by the dashboard API.

These are *our* models, decoupled from the raw PVE responses, so the API stays stable
even if Proxmox changes field names between releases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

GuestType = Literal["qemu", "lxc"]
Severity = Literal["info", "warning", "critical"]


class ClusterWarning(BaseModel):
    severity: Severity
    target: str = Field(description="e.g. 'node/pve1', 'storage/pve1/local-zfs', 'cluster'")
    message: str


class NodeView(BaseModel):
    cluster: str
    node: str
    status: Literal["online", "offline", "unknown"]
    ip: str | None = None
    cpu: float = Field(0.0, description="CPU utilisation 0..1")
    maxcpu: int = 0
    mem: int = 0
    maxmem: int = 0
    swap_used: int | None = None
    swap_total: int | None = None
    rootfs_used: int | None = None
    rootfs_total: int | None = None
    iowait: float | None = Field(None, description="IO delay 0..1")
    loadavg: list[float] | None = None
    uptime: int = 0
    pve_version: str | None = None
    kernel: str | None = None
    cpu_model: str | None = None
    cpu_sockets: int | None = None
    cpu_cores: int | None = None
    guests_running: int = 0
    guests_total: int = 0
    vcpus_allocated: int = Field(0, description="Sum of vCPUs of running guests")
    mem_allocated: int = Field(0, description="Sum of configured RAM of running guests")


class GuestView(BaseModel):
    cluster: str
    vmid: int
    name: str
    type: GuestType
    node: str
    status: str
    template: bool = False
    cpu: float = Field(0.0, description="Utilisation relative to the guest's own vCPUs, 0..1")
    cpu_host: float = Field(
        0.0, description="Share of the host's total CPU capacity used by this guest, 0..1"
    )
    maxcpu: int = 0
    mem: int = 0
    maxmem: int = 0
    disk: int = 0
    maxdisk: int = 0
    diskread_bps: float | None = None
    diskwrite_bps: float | None = None
    netin_bps: float | None = None
    netout_bps: float | None = None
    uptime: int = 0
    tags: list[str] = []
    pool: str | None = None
    lock: str | None = None
    console_url: str | None = None


class StorageView(BaseModel):
    cluster: str
    node: str
    storage: str
    type: str
    status: str
    shared: bool = False
    used: int = 0
    total: int = 0
    content: list[str] = []


class ClusterTotals(BaseModel):
    nodes_online: int = 0
    nodes_total: int = 0
    cpu: float = Field(0.0, description="Capacity-weighted CPU utilisation of online nodes")
    cpus: int = 0
    mem: int = 0
    maxmem: int = 0
    swap_used: int = 0
    swap_total: int = 0
    storage_used: int = Field(0, description="Shared storages counted once")
    storage_total: int = 0
    netin_bps: float = 0.0
    netout_bps: float = 0.0
    diskread_bps: float = 0.0
    diskwrite_bps: float = 0.0
    iowait: float | None = Field(None, description="Average IO delay of online nodes")
    load1: float | None = Field(None, description="Sum of 1-min load average")
    vms_running: int = 0
    vms_stopped: int = 0
    lxc_running: int = 0
    lxc_stopped: int = 0
    templates: int = 0
    vcpus_allocated: int = 0
    mem_allocated: int = 0


class ClusterSnapshot(BaseModel):
    name: str
    is_cluster: bool = False
    reachable: bool = False
    quorate: bool | None = None
    api_url: str | None = None
    ui_url: str | None = None
    pve_version: str | None = None
    updated_at: datetime | None = None
    last_error: str | None = None
    totals: ClusterTotals = ClusterTotals()
    nodes: list[NodeView] = []
    guests: list[GuestView] = []
    storages: list[StorageView] = []
    warnings: list[ClusterWarning] = []
    # Ceph/HA are detected in later phases; None = not present / not checked.
    ha_enabled: bool | None = None
    ceph_health: str | None = None
