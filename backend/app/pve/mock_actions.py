"""Write operations and tasks for the mock cluster.

Every action creates a task (UPID) that finishes after a type-specific delay; the effect
(status change, migration, snapshot) is applied when the task completes - like on a real
cluster, where e.g. a migration takes a while.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.pve.client import PveError

if TYPE_CHECKING:
    from app.pve.mock import MockGuest, MockPveClient

MOCK_USER = "dashboard@pve!operator"
DURATION = {"qmstart": 3, "vzstart": 2, "qmshutdown": 6, "vzshutdown": 4, "qmstop": 1,
            "vzstop": 1, "qmreboot": 8, "vzreboot": 5, "qmsuspend": 2, "qmresume": 2,
            "vzsuspend": 2, "vzresume": 2, "qmsnapshot": 3, "vzsnapshot": 3,
            "qmrollback": 4, "vzrollback": 4, "qmdelsnapshot": 2, "vzdelsnapshot": 2,
            "vzdump": 15, "qmigrate": 12, "vzmigrate": 8}


@dataclass
class MockTask:
    upid: str
    node: str
    type: str
    id: str
    user: str
    starttime: float
    duration: float
    effect: Callable[[], str | None] | None = None  # returns an error text or None
    endtime: float | None = None
    status: str | None = None  # None while running; "OK" or error text
    log: list[str] = field(default_factory=list)


@dataclass
class MockSnapshot:
    name: str
    description: str
    snaptime: int
    vmstate: bool = False
    parent: str | None = None


APT_PACKAGES = [
    ("pve-manager", "9.0.10", "9.0.11", "Proxmox Virtual Environment Management Tools"),
    ("proxmox-kernel-6.14", "6.14.11-4", "6.14.11-5", "Latest Proxmox Kernel Image"),
    ("qemu-server", "9.0.21", "9.0.23", "Qemu Server Tools"),
    ("libpve-common-perl", "9.0.11", "9.0.12", "Proxmox VE base library"),
    ("zfsutils-linux", "2.3.4-pve1", "2.3.4-pve2", "command-line tools to manage OpenZFS"),
    ("openssl", "3.5.1-1", "3.5.1-1+deb13u1", "Secure Sockets Layer toolkit"),
    ("tzdata", "2025b-4", "2025b-5", "time zone and daylight-saving time data"),
]


class MockActions:
    def __init__(self, mock: MockPveClient):
        self.mock = mock
        self.tasks: list[MockTask] = []
        self.snapshots: dict[int, list[MockSnapshot]] = {}
        self._pid = 1000
        # Pending apt updates per node; edge is up to date.
        self.apt: dict[str, list[tuple[str, str, str, str]]] = {
            n.name: list(APT_PACKAGES[: 7 - i * 2]) for i, n in enumerate(mock.spec.nodes)
            if n.name != "edge"}
        self.reboot_required: dict[str, bool] = {}
        self._seed()

    # --- helpers --------------------------------------------------------------------------------

    def _guest(self, node: str, gtype: str, vmid: int, path: str) -> MockGuest:
        g = next((x for x in self.mock.spec.guests if x.vmid == vmid and x.type == gtype), None)
        if g is None or g.node != node:
            raise PveError(500, f"Configuration file 'nodes/{node}/{gtype}/{vmid}.conf' does not "
                                "exist", path)
        return g

    def _task(self, node: str, ttype: str, tid: str, effect: Callable[[], str | None] | None,
              start: float | None = None, user: str = MOCK_USER) -> MockTask:
        now = self.mock.clock() if start is None else start
        self._pid += 1
        upid = f"UPID:{node}:{self._pid:08X}:{int(now * 100) & 0xFFFFFFFF:08X}:" \
               f"{int(now):08X}:{ttype}:{tid}:{user}:"
        t = MockTask(upid, node, ttype, tid, user, now, DURATION.get(ttype, 3), effect,
                     log=[f"task started by {user}"])
        self.tasks.append(t)
        self.tasks = self.tasks[-300:]
        return t

    def advance(self) -> None:
        """Finish tasks whose time has come and apply their effects."""
        now = self.mock.clock()
        for t in self.tasks:
            if t.status is None and now >= t.starttime + t.duration:
                error = t.effect() if t.effect else None
                t.endtime = t.starttime + t.duration
                t.status = error or "OK"
                t.log.append(f"TASK ERROR: {error}" if error else "TASK OK")

    def _seed(self) -> None:
        """Some history for the task list and snapshots."""
        now = self.mock.clock()
        guests = [g for g in self.mock.spec.guests if not g.template]
        for i, g in enumerate(guests[:6]):
            t = self._task(g.node, "vzdump", str(g.vmid), None, start=now - 6 * 3600 + i * 300,
                           user="root@pam")
            t.log += [f"INFO: Starting Backup of VM {g.vmid} ({g.type})",
                      "INFO: creating Proxmox Backup Server archive", "INFO: Finished Backup"]
            if i == 4:
                t.effect = lambda: "job errors"
                t.log.insert(-1, "ERROR: Backup of VM failed - unable to connect to PBS")
        if guests:
            g = guests[0]
            self.snapshots[g.vmid] = [
                MockSnapshot("vor-update-2026-09", "Vor HA Core Update", int(now - 40 * 86400)),
                MockSnapshot("vor-update-2026-10", "Vor HA OS 16", int(now - 9 * 86400),
                             parent="vor-update-2026-09"),
            ]
        self.advance()

    # --- dispatch -------------------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any]) -> Any:
        self.advance()
        m = re.fullmatch(r"/nodes/([^/]+)/apt/(update|changelog)", path)
        if m:
            node = m.group(1)
            if m.group(2) == "changelog":
                name = params.get("name", "")
                return (f"{name} ({params.get('version', 'new')}) trixie; urgency=medium\n\n"
                        "  * fix various issues and improve performance\n"
                        "  * update translations\n\n -- Proxmox Support Team  Mon, 20 Oct 2026\n")
            return [{"Package": p, "OldVersion": old, "Version": new, "Title": title,
                     "Section": "admin", "Priority": "optional",
                     "Origin": "Proxmox" if p.startswith(("pve", "proxmox", "qemu", "libpve",
                                                           "zfs")) else "Debian"}
                    for p, old, new, title in self.apt.get(node, [])]
        if path == "/cluster/tasks":
            return [self._task_dict(t) for t in reversed(self.tasks)]
        m = re.fullmatch(r"/nodes/([^/]+)/tasks", path)
        if m:
            rows = [self._task_dict(t) for t in reversed(self.tasks) if t.node == m.group(1)]
            if params.get("vmid"):
                rows = [r for r in rows if r["id"] == str(params["vmid"])]
            return rows[: int(params.get("limit", 50))]
        m = re.fullmatch(r"/nodes/([^/]+)/tasks/([^/]+)/(status|log)", path)
        if m:
            t = self._find_task(m.group(2), path)
            if m.group(3) == "status":
                d = self._task_dict(t)
                d["status"] = "running" if t.status is None else "stopped"
                if t.status is not None:
                    d["exitstatus"] = t.status
                return d
            start = int(params.get("start", 0))
            limit = int(params.get("limit", 500))
            return [{"n": i + 1, "t": line} for i, line in enumerate(t.log)][start:start + limit]
        m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/snapshot", path)
        if m:
            node, gtype, vmid = m.group(1), m.group(2), int(m.group(3))
            self._guest(node, gtype, vmid, path)
            snaps = self.snapshots.get(vmid, [])
            out: list[dict[str, Any]] = [{"name": s.name, "description": s.description,
                                          "snaptime": s.snaptime, "vmstate": int(s.vmstate),
                                          **({"parent": s.parent} if s.parent else {})}
                                         for s in snaps]
            out.append({"name": "current", "description": "You are here!",
                        **({"parent": snaps[-1].name} if snaps else {})})
            return out
        m = re.fullmatch(r"/nodes/([^/]+)/qemu/(\d+)/migrate", path)
        if m:
            node = m.group(1)
            g = self._guest(node, "qemu", int(m.group(2)), path)
            others = [n.name for n in self.mock.spec.nodes if n.name != node and n.online]
            local = [] if g.name != "truenas" else [{"volid": "local-zfs:vm-103-disk-0"}]
            res: dict[str, Any] = {"running": int(g.status == "running"), "allowed_nodes": others,
                                   "not_allowed_nodes": {}, "local_disks": local,
                                   "local_resources": []}
            if g.name in ("truenas", "win11-desk"):  # PCI passthrough blocks live migration
                res["local_resources"] = ["hostpci0"]
                res["allowed_nodes"] = []
                res["not_allowed_nodes"] = {n: {"unavailable_resources": ["hostpci0"]}
                                            for n in others}
            return res
        raise LookupError(path)

    def post(self, path: str, data: dict[str, Any]) -> Any:
        self.advance()
        m = re.fullmatch(r"/nodes/([^/]+)/apt/update", path)
        if m:
            t = self._task(m.group(1), "aptupdate", "", None)
            t.log += ["starting apt-get update",
                      "Hit:1 http://deb.debian.org/debian trixie InRelease",
                      "Hit:2 http://download.proxmox.com/debian/pve trixie InRelease",
                      "Reading package lists...", "Building dependency tree..."]
            return t.upid
        m = re.fullmatch(r"/nodes/([^/]+)/status", path)
        if m:
            return self._reboot(m.group(1), str(data.get("command")), path)
        m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/status/(\w+)", path)
        if m:
            return self._power(m.group(1), m.group(2), int(m.group(3)), m.group(4), path)
        m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/snapshot", path)
        if m:
            return self._snapshot_create(m.group(1), m.group(2), int(m.group(3)), data, path)
        m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/snapshot/([^/]+)/rollback", path)
        if m:
            return self._snapshot_rollback(m.group(1), m.group(2), int(m.group(3)), m.group(4),
                                           path)
        m = re.fullmatch(r"/nodes/([^/]+)/vzdump", path)
        if m:
            vmid = int(data.get("vmid", 0))
            g = next((x for x in self.mock.spec.guests if x.vmid == vmid), None)
            if g is None or g.node != m.group(1):
                raise PveError(500, f"guest {vmid} not on node {m.group(1)}", path)
            t = self._task(g.node, "vzdump", str(vmid), None)
            t.log += [f"INFO: starting new backup job: vzdump {vmid} --storage "
                      f"{data.get('storage')} --mode {data.get('mode', 'snapshot')}",
                      f"INFO: Starting Backup of VM {vmid} ({g.type})", "INFO: Finished Backup"]
            return t.upid
        m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/migrate", path)
        if m:
            return self._migrate(m.group(1), m.group(2), int(m.group(3)), data, path)
        raise LookupError(path)

    def delete(self, path: str, params: dict[str, Any]) -> Any:
        self.advance()
        m = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/snapshot/([^/]+)", path)
        if m:
            node, gtype, vmid, name = m.group(1), m.group(2), int(m.group(3)), m.group(4)
            self._guest(node, gtype, vmid, path)
            snaps = self.snapshots.get(vmid, [])
            if not any(s.name == name for s in snaps):
                raise PveError(500, f"snapshot '{name}' does not exist", path)

            def effect() -> None:
                self.snapshots[vmid] = [s for s in snaps if s.name != name]

            prefix = "qm" if gtype == "qemu" else "vz"
            return self._task(node, f"{prefix}delsnapshot", str(vmid), effect).upid
        raise LookupError(path)

    # --- actions --------------------------------------------------------------------------------

    def _power(self, node: str, gtype: str, vmid: int, action: str, path: str) -> str:
        g = self._guest(node, gtype, vmid, path)
        if action not in ("start", "shutdown", "stop", "reboot", "suspend", "resume"):
            raise PveError(501, f"Method 'POST {path}' not implemented", path)
        prefix = "qm" if gtype == "qemu" else "vz"

        def effect() -> str | None:
            kind = "VM" if gtype == "qemu" else "CT"
            if action == "start":
                if g.status == "running":
                    return f"{kind} {vmid} already running"
                g.status = "running"
            elif action in ("shutdown", "stop"):
                if g.status == "stopped":
                    return f"{kind} {vmid} not running"
                g.status = "stopped"
            elif action == "reboot":
                if g.status != "running":
                    return f"{kind} {vmid} not running"
            elif action == "suspend":
                if g.status != "running":
                    return f"{kind} {vmid} not running"
                g.status = "paused"
            elif action == "resume":
                if g.status != "paused":
                    return f"{kind} {vmid} not paused"
                g.status = "running"
            return None

        return self._task(node, f"{prefix}{action}", str(vmid), effect).upid

    def _snapshot_create(self, node: str, gtype: str, vmid: int, data: dict[str, Any],
                         path: str) -> str:
        self._guest(node, gtype, vmid, path)
        name = str(data.get("snapname", ""))
        snaps = self.snapshots.setdefault(vmid, [])
        if any(s.name == name for s in snaps):
            raise PveError(500, f"snapshot name '{name}' already used", path)
        snap = MockSnapshot(name, str(data.get("description", "")), int(self.mock.clock()),
                            vmstate=str(data.get("vmstate", "0")) == "1",
                            parent=snaps[-1].name if snaps else None)

        def effect() -> None:
            snaps.append(snap)

        prefix = "qm" if gtype == "qemu" else "vz"
        return self._task(node, f"{prefix}snapshot", str(vmid), effect).upid

    def _snapshot_rollback(self, node: str, gtype: str, vmid: int, name: str, path: str) -> str:
        g = self._guest(node, gtype, vmid, path)
        if not any(s.name == name for s in self.snapshots.get(vmid, [])):
            raise PveError(500, f"snapshot '{name}' does not exist", path)

        def effect() -> None:
            g.status = "stopped" if gtype == "lxc" else g.status

        prefix = "qm" if gtype == "qemu" else "vz"
        return self._task(node, f"{prefix}rollback", str(vmid), effect).upid

    def _migrate(self, node: str, gtype: str, vmid: int, data: dict[str, Any], path: str) -> str:
        g = self._guest(node, gtype, vmid, path)
        target = data.get("target")
        if target not in {n.name for n in self.mock.spec.nodes if n.online} or target == node:
            raise PveError(400, f"Parameter verification failed: target '{target}'", path)
        if g.name in ("truenas", "win11-desk"):
            raise PveError(500, "can't migrate VM which uses local devices: hostpci0", path)

        def effect() -> None:
            g.node = str(target)

        t = self._task(node, "qmigrate" if gtype == "qemu" else "vzmigrate", str(vmid), effect)
        t.log += [f"starting migration of {gtype.upper()} {vmid} to node '{target}'",
                  "migration finished successfully"]
        return t.upid

    def _reboot(self, node: str, command: str, path: str) -> None:
        n = next((x for x in self.mock.spec.nodes if x.name == node), None)
        if n is None or not n.online:
            raise PveError(595, f"node '{node}' not reachable", path)
        if command not in ("reboot", "shutdown"):
            raise PveError(400, f"Parameter verification failed: command '{command}'", path)
        was_running = [g for g in self.mock.spec.guests
                       if g.node == node and g.status == "running"]
        for g in was_running:
            g.status = "stopped"
        n.online = False

        def boot() -> None:
            n.online = True
            self.mock.started = self.mock.clock()  # uptime restarts
            self.reboot_required[node] = False
            for g in was_running:
                g.status = "running"  # onboot=1 in the mock configs

        # Not a PVE task in reality (the call returns null); modelled as one for the timing.
        t = self._task(node, "srvreboot", "", boot if command == "reboot" else None)
        t.duration = 25
        return None

    # --- formatting -----------------------------------------------------------------------------

    def _find_task(self, upid: str, path: str) -> MockTask:
        t = next((x for x in self.tasks if x.upid == upid), None)
        if t is None:
            raise PveError(500, "no such task", path)
        return t

    @staticmethod
    def _task_dict(t: MockTask) -> dict[str, Any]:
        d: dict[str, Any] = {"upid": t.upid, "node": t.node, "type": t.type, "id": t.id,
                             "user": t.user, "starttime": int(t.starttime),
                             "pid": int(t.upid.split(":")[2], 16)}
        if t.status is not None:
            d["endtime"] = int(t.endtime or t.starttime)
            d["status"] = t.status
        return d
