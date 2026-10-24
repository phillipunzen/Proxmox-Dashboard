from app.collector.builder import (
    CounterSample,
    build_snapshot,
    compute_rates,
    console_url,
)
from app.config import ClusterConfig

GiB = 1024**3
CFG = ClusterConfig(name="c1", urls=["https://pve1:8006"])


def test_rates_need_previous_sample():
    assert compute_rates(None, CounterSample(10, (1, 2, 3, 4))) == (None,) * 4


def test_rates_per_second_and_counter_reset():
    prev = CounterSample(100, (1000, 1000, 1000, 1000))
    cur = CounterSample(110, (2000, 500, 1000, 11000))
    assert compute_rates(prev, cur) == (100.0, None, 0.0, 1000.0)


def test_rates_ignore_non_advancing_clock():
    s = CounterSample(100, (1, 1, 1, 1))
    assert compute_rates(s, s) == (None,) * 4


def test_console_urls():
    assert "console=kvm" in console_url("https://p:8006", "qemu", 100, "vm", "n1")
    assert "novnc=1" in console_url("https://p:8006", "qemu", 100, "vm", "n1")
    lxc = console_url("https://p:8006", "lxc", 200, "ct", "n1")
    assert "console=lxc" in lxc and "xtermjs=1" in lxc and "vmid=200" in lxc


def _resources():
    return [
        {"type": "node", "node": "n1", "status": "online", "cpu": 0.5, "maxcpu": 8,
         "mem": 8 * GiB, "maxmem": 32 * GiB, "uptime": 100},
        {"type": "node", "node": "n2", "status": "offline", "cpu": 0, "maxcpu": 8,
         "mem": 0, "maxmem": 32 * GiB},
        {"type": "qemu", "vmid": 100, "name": "web", "node": "n1", "status": "running",
         "cpu": 0.5, "maxcpu": 4, "mem": 2 * GiB, "maxmem": 4 * GiB, "tags": "prod;web",
         "diskread": 0, "diskwrite": 0, "netin": 0, "netout": 0},
        {"type": "lxc", "vmid": 200, "name": "dns", "node": "n1", "status": "stopped",
         "maxcpu": 1, "maxmem": GiB},
        {"type": "qemu", "vmid": 900, "name": "tpl", "node": "n1", "status": "stopped",
         "template": 1, "maxcpu": 2, "maxmem": GiB},
        {"type": "storage", "storage": "nfs", "node": "n1", "plugintype": "nfs",
         "status": "available", "shared": 1, "disk": 96, "maxdisk": 100},
        {"type": "storage", "storage": "nfs", "node": "n2", "plugintype": "nfs",
         "status": "available", "shared": 1, "disk": 96, "maxdisk": 100},
        {"type": "storage", "storage": "local", "node": "n1", "plugintype": "dir",
         "status": "available", "shared": 0, "disk": 10, "maxdisk": 100},
    ]


STATUS = [
    {"type": "cluster", "name": "c1", "quorate": 0, "nodes": 2},
    {"type": "node", "name": "n1", "ip": "10.0.0.1", "online": 1},
    {"type": "node", "name": "n2", "ip": "10.0.0.2", "online": 0},
]

NODE_STATUS = {"n1": {"wait": 0.12, "loadavg": ["1.5", "1.0", "0.5"],
                      "pveversion": "pve-manager/9.0.10/abc",
                      "current-kernel": {"release": "6.14.11-4-pve"},
                      "swap": {"used": 1, "total": 2},
                      "cpuinfo": {"model": "Ryzen", "sockets": 1, "cores": 4}}}


def test_build_snapshot_core_fields():
    snap, counters = build_snapshot(CFG, _resources(), STATUS, NODE_STATUS, {}, now=1000.0)
    assert snap.is_cluster and snap.quorate is False
    n1 = next(n for n in snap.nodes if n.node == "n1")
    assert n1.ip == "10.0.0.1" and n1.pve_version == "9.0.10" and n1.kernel == "6.14.11-4-pve"
    assert n1.loadavg == [1.5, 1.0, 0.5] and n1.cpu_model == "Ryzen"
    assert n1.guests_running == 1 and n1.guests_total == 2  # template excluded
    assert n1.vcpus_allocated == 4

    web = next(g for g in snap.guests if g.vmid == 100)
    assert web.tags == ["prod", "web"]
    # 50 % of 4 vCPUs on an 8-thread host = 25 % of the host
    assert web.cpu_host == 0.25
    assert web.diskread_bps is None  # first sample
    assert set(counters) == {100}  # only running guests keep counters


def test_totals_count_shared_storage_once_and_skip_offline_nodes():
    snap, _ = build_snapshot(CFG, _resources(), STATUS, NODE_STATUS, {}, now=1000.0)
    t = snap.totals
    assert (t.nodes_online, t.nodes_total) == (1, 2)
    assert t.cpus == 8 and t.cpu == 0.5
    assert t.storage_used == 96 + 10 and t.storage_total == 200
    assert (t.vms_running, t.vms_stopped, t.lxc_running, t.lxc_stopped, t.templates) == (
        1, 0, 0, 1, 1)
    assert t.iowait == 0.12 and t.load1 == 1.5


def test_warnings():
    snap, _ = build_snapshot(CFG, _resources(), STATUS, NODE_STATUS, {}, now=1000.0)
    msgs = [(w.severity, w.target) for w in snap.warnings]
    assert ("critical", "cluster") in msgs  # no quorum
    assert ("critical", "node/n2") in msgs  # offline
    assert ("warning", "node/n1") in msgs  # iowait 12 %
    assert ("critical", "storage/n1/nfs") in msgs  # 96 % full
    assert sum(1 for w in snap.warnings if "nfs" in w.target) == 1  # shared reported once


def test_rates_on_second_poll():
    res = _resources()
    _, counters = build_snapshot(CFG, res, STATUS, {}, {}, now=1000.0)
    res[2].update(diskread=10_000, netout=5_000)
    snap, _ = build_snapshot(CFG, res, STATUS, {}, counters, now=1010.0)
    web = next(g for g in snap.guests if g.vmid == 100)
    assert web.diskread_bps == 1000.0 and web.netout_bps == 500.0
    assert snap.totals.diskread_bps == 1000.0


def test_standalone_host_has_no_quorum_info():
    res = [r for r in _resources() if r.get("node") != "n2"]
    snap, _ = build_snapshot(CFG, res, [STATUS[1]], {}, {}, now=1.0)
    assert snap.is_cluster is False and snap.quorate is None
