import asyncio

import pytest

from app.config import mock_clusters
from app.inventory import parse
from app.inventory.export import rows, to_csv
from app.inventory.models import GuestInventory
from app.main import create_app
from app.pve import MockPveClient
from tests.conftest import logged_in

GiB = 1024**3


def test_parse_size():
    assert parse.parse_size("32G") == 32 * GiB
    assert parse.parse_size("512M") == 512 * 1024**2
    assert parse.parse_size("1.5T") == int(1.5 * 1024**4)
    assert parse.parse_size("4096") == 4096
    assert parse.parse_size("bogus") is None and parse.parse_size(None) is None


def test_split_options():
    assert parse.split_options("local-zfs:vm-1-disk-0,iothread=1,size=8G,ro") == (
        "local-zfs:vm-1-disk-0", {"iothread": "1", "size": "8G", "ro": "1"})
    assert parse.split_options("bridge=vmbr0,tag=5") == (None, {"bridge": "vmbr0", "tag": "5"})


def test_disks_wearout_is_inverted_to_used():
    disks = parse.parse_disks([{"devpath": "/dev/sda", "wearout": 97, "size": 10},
                               {"devpath": "/dev/sdb", "wearout": "N/A"}])
    assert disks[0].wearout_used == 3 and disks[1].wearout_used is None


def test_network_bond_bridge_vlan():
    ifaces = parse.parse_network([
        {"iface": "vmbr0", "type": "bridge", "bridge_ports": "bond0", "bridge_vlan_aware": 1,
         "address": "10.0.0.2", "netmask": "24", "active": 1},
        {"iface": "bond0", "type": "bond", "slaves": "eno1 eno2", "bond_mode": "802.3ad"},
        {"iface": "vmbr0.30", "type": "vlan", "vlan-id": "30", "vlan-raw-device": "vmbr0"},
    ])
    assert [i.iface for i in ifaces] == ["bond0", "vmbr0", "vmbr0.30"]  # eth, bond, bridge, vlan
    assert ifaces[0].slaves == ["eno1", "eno2"]
    assert ifaces[1].cidr == "10.0.0.2/24" and ifaces[1].bridge_vlan_aware
    assert ifaces[2].vlan_id == 30


def test_pci_class_names_and_iommu():
    pci = parse.parse_pci([{"id": "0000:01:00.0", "class": "0x030000", "iommugroup": 13},
                           {"id": "0000:00:00.0", "class": "0x060000", "iommugroup": -1}])
    assert pci[1].class_name == "Grafik" and pci[1].iommu_group == 13
    assert pci[0].iommu_group is None  # -1 = IOMMU off


def test_expand_pci_ids():
    known = ["0000:01:00.0", "0000:01:00.1", "0000:02:00.0"]
    assert parse.expand_pci_ids("01:00", known) == ["0000:01:00.0", "0000:01:00.1"]
    assert parse.expand_pci_ids("0000:02:00.0", known) == ["0000:02:00.0"]
    assert parse.expand_pci_ids("0000:01:00.0;0000:01:00.1", []) == known[:2]
    assert parse.expand_pci_ids("03:00", []) == ["0000:03:00.0"]


def _inv(gtype="qemu"):
    return GuestInventory(cluster="c", vmid=100, name="vm", type=gtype, node="n", status="running")


def test_qemu_config():
    inv = _inv()
    parse.apply_guest_config(inv, {
        "cores": 2, "sockets": 2, "memory": "4096", "balloon": "1024", "cpu": "cputype=host,flags=+aes",
        "bios": "ovmf", "machine": "q35", "agent": "enabled=1,fstrim_cloned_disks=1",
        "scsi0": "local-zfs:vm-100-disk-0,iothread=1,size=32G",
        "ide2": "local:iso/x.iso,media=cdrom,size=600M", "efidisk0": "local-zfs:vm-100-disk-1,size=1M",
        "unused0": "local-zfs:vm-100-disk-9",
        "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,tag=20,firewall=1,rate=12.5",
        "net10": "e1000=BC:24:11:00:00:02,bridge=vmbr1",
        "net2": "virtio=BC:24:11:00:00:03,bridge=vmbr1",
        "hostpci0": "01:00,pcie=1,x-vga=1", "hostpci1": "mapping=gpu,pcie=1",
        "usb0": "host=10c4:ea60", "tags": "prod;db", "description": " Notiz \n", "onboot": 1,
    }, known_pci=["0000:01:00.0", "0000:01:00.1"], pci_mappings={"gpu": ["0000:05:00.0"]})
    assert (inv.cores, inv.sockets, inv.vcpus, inv.cpu_type) == (2, 2, 4, "host")
    assert inv.memory == 4 * GiB and inv.balloon_min == GiB
    assert inv.bios == "ovmf" and inv.machine == "q35" and inv.agent_enabled is True
    disks = {d.key: d for d in inv.disks}
    assert disks["scsi0"].storage == "local-zfs" and disks["scsi0"].size == 32 * GiB
    assert disks["scsi0"].options == {"iothread": "1"}
    assert disks["ide2"].media == "cdrom" and "unused0" in disks
    assert [n.key for n in inv.nics] == ["net0", "net2", "net10"]  # numeric order
    assert inv.nics[0].model == "virtio" and inv.nics[0].mac == "BC:24:11:00:00:01"
    assert inv.nics[0].vlan == 20 and inv.nics[0].firewall and inv.nics[0].rate_mbps == 100.0
    assert inv.hostpci[0].devices == ["0000:01:00.0", "0000:01:00.1"]
    assert inv.hostpci[1].mapping == "gpu" and inv.hostpci[1].devices == ["0000:05:00.0"]
    assert inv.usb == ["usb0: host=10c4:ea60"] and inv.tags == ["prod", "db"]
    assert inv.notes == "Notiz" and inv.onboot


def test_qemu_defaults():
    inv = _inv()
    parse.apply_guest_config(inv, {}, [], {})
    assert inv.memory == 512 * 1024**2 and inv.cpu_type == "kvm64" and inv.bios == "seabios"
    assert inv.machine == "i440fx" and inv.agent_enabled is False and inv.balloon_min is None


def test_lxc_config():
    inv = _inv("lxc")
    parse.apply_guest_config(inv, {
        "cores": 2, "memory": "1024", "swap": "512", "unprivileged": 1, "arch": "amd64",
        "rootfs": "local-zfs:subvol-100-disk-0,size=8G",
        "mp0": "/mnt/media,mp=/media", "mp1": "tank:subvol-100-disk-1,mp=/data,size=100G",
        "net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,ip=dhcp,ip6=auto,type=veth",
        "dev0": "/dev/dri/renderD128,gid=104",
    }, [], {})
    assert inv.memory == GiB and inv.swap == 512 * 1024**2 and inv.unprivileged
    assert [d.key for d in inv.disks] == ["rootfs", "mp0", "mp1"]
    assert inv.disks[0].mountpoint == "/" and inv.disks[1].storage is None  # bind mount
    assert inv.disks[2].mountpoint == "/data" and inv.disks[2].size == 100 * GiB
    assert inv.nics[0].name == "eth0" and inv.nics[0].ip_config == "ip=dhcp, ip6=auto"
    assert inv.usb == ["dev0: /dev/dri/renderD128,gid=104"]


def test_mappings_and_ips():
    m = parse.parse_mappings([{"id": "gpu", "map": [
        "node=a,path=0000:01:00.0;0000:01:00.1,id=10de:1", "node=b,path=0000:02:00.0,id=10de:1"]}],
        node="b")
    assert m == {"gpu": ["0000:02:00.0"]}
    assert parse.agent_ips({"result": [
        {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
        {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.0.5"},
                                          {"ip-address": "fe80::1"}]}]}) == ["10.0.0.5"]
    assert parse.lxc_ips([{"name": "eth0", "inet": "10.0.0.6/24", "inet6": "fd00::6/64"}]) == [
        "10.0.0.6", "fd00::6"]


def test_csv_is_excel_friendly_and_safe():
    text = to_csv([{"name": "=cmd()", "ok": True, "mem": 1.5, "none": None}])
    assert text.startswith("﻿name;ok;mem;none\n")
    assert "'=cmd();ja;1,50;" in text


# --- collector + API against the mock cluster -------------------------------------------------


@pytest.fixture
async def api(settings, clock):
    clients = {c.name: MockPveClient(c.name, clock=clock) for c in mock_clusters()}
    app = create_app(settings, clients=clients)
    async with app.router.lifespan_context(app):
        await app.state.inventory_collector.refresh()  # wait for the first run
        async with logged_in(app) as client:
            yield client


async def test_inventory_api(api):
    inv = (await api.get("/api/v1/inventory/clusters/homelab")).json()
    assert len(inv["nodes"]) == 3 and len(inv["guests"]) == 33
    pve2 = (await api.get("/api/v1/inventory/clusters/homelab/nodes/pve2")).json()
    gpu = next(p for p in pve2["pci"] if p["id"] == "0000:01:00.0")
    assert gpu["assigned_to"][0]["name"] == "win11-desk"  # via PCI mapping
    win = (await api.get("/api/v1/inventory/clusters/homelab/guests/101")).json()
    assert win["agent_running"] and win["os_name"].startswith("Microsoft")
    assert win["bios"] == "ovmf" and any(d["key"] == "tpmstate0" for d in win["disks"])
    stopped = next(g for g in inv["guests"] if g["status"] == "stopped" and g["type"] == "qemu")
    assert stopped["agent_running"] is None and stopped["ip_addresses"] == []


async def test_smart_only_for_known_disks(api):
    ok = await api.get("/api/v1/inventory/clusters/homelab/nodes/pve3/smart",
                       params={"disk": "/dev/sda"})
    assert ok.status_code == 200 and ok.json()["health"] == "FAILED"
    bad = await api.get("/api/v1/inventory/clusters/homelab/nodes/pve3/smart",
                        params={"disk": "/dev/sdz"})
    assert bad.status_code == 404
    evil = await api.get("/api/v1/inventory/clusters/homelab/nodes/pve3/smart",
                         params={"disk": "/etc/passwd"})
    assert evil.status_code == 422


async def test_export_csv_and_json(api):
    r = await api.get("/api/v1/inventory/export", params={"kind": "guests", "cluster": "homelab"})
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("cluster;vmid;name") and len(lines) == 34
    j = await api.get("/api/v1/inventory/export", params={"kind": "pci", "format": "json"})
    assert any(row["assigned_to"] for row in j.json())


async def test_refresh_is_throttled(api):
    first = (await api.post("/api/v1/inventory/refresh")).json()
    assert first["started"] is False  # the fixture just ran it
    await asyncio.sleep(0)


def test_export_tables_exist():
    for kind in ("nodes", "disks", "network", "pci", "guests", "guest_disks", "guest_nics"):
        assert rows(kind, []) == []
