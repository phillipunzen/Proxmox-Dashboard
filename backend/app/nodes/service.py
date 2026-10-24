"""Node management: updates, upgrade via SSH, maintenance mode, reboot, sensors.

Proxmox API tokens used:
* read token     – status, task status
* operator token – guest migration/shutdown during maintenance
* nodeadmin token (Sys.Modify, Sys.PowerMgmt) – list/refresh apt updates, changelog, reboot
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app import audit
from app.collector.models import ClusterSnapshot, GuestView
from app.collector.state import SnapshotStore
from app.config import ClusterConfig, Settings
from app.db.engine import Database
from app.db.models import NodeState, utcnow
from app.jobs import JobContext, JobFailed, JobManager
from app.pve import PveApi, PveError, PveUnavailable
from app.ssh import HostKeyUnknown, SshError, SshRunner, SshTarget

log = logging.getLogger(__name__)


class NodeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Package(BaseModel):
    package: str
    old_version: str | None
    new_version: str
    title: str | None
    origin: str | None
    section: str | None
    priority: str | None


class UpdateInfo(BaseModel):
    cluster: str
    node: str
    packages: list[Package] = []
    checked_at: datetime | None = None
    error: str | None = None
    reboot_required: bool | None = None


class NodeStatusView(BaseModel):
    cluster: str
    node: str
    online: bool
    maintenance: bool
    maintenance_since: datetime | None
    maintenance_by: str | None
    evacuated: list[dict[str, Any]]
    updates: int | None
    updates_checked_at: datetime | None
    updates_error: str | None
    reboot_required: bool | None
    temperature: float | None
    ssh_host: str | None
    ssh_trusted: bool
    running_job: int | None
    can_manage: bool
    manage_reason: str | None


@dataclass
class NodeServiceState:
    updates: dict[tuple[str, str], UpdateInfo] = field(default_factory=dict)
    temps: dict[tuple[str, str], float] = field(default_factory=dict)
    dmi: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)


def max_temperature(sensors: dict[str, Any]) -> float | None:
    """Highest ``temp*_input`` value from ``sensors -j`` output."""
    best: float | None = None

    def walk(obj: Any) -> None:
        nonlocal best
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, int | float) and k.startswith("temp") and k.endswith("_input"):
                    if 0 < v < 150:  # ignore bogus sensors
                        best = v if best is None else max(best, float(v))
                else:
                    walk(v)

    walk(sensors)
    return best


def quorum_after_reboot(snap: ClusterSnapshot, node: str) -> tuple[bool, str]:
    """Would the cluster keep quorum if ``node`` goes down? (1 vote per node assumed.)"""
    if not snap.is_cluster:
        return True, "Standalone-Host"
    total = len(snap.nodes)
    online = sum(1 for n in snap.nodes if n.status == "online" and n.node != node)
    needed = total // 2 + 1
    if online >= needed:
        return True, f"{online} von {total} Nodes bleiben online (benötigt: {needed})"
    return False, f"Nur {online} von {total} Nodes blieben online – Quorum benötigt {needed}"


class NodeService:
    def __init__(self, settings: Settings, clusters: list[ClusterConfig],
                 read: dict[str, PveApi], operator: dict[str, PveApi],
                 nodeadmin: dict[str, PveApi], store: SnapshotStore, db: Database,
                 jobs: JobManager, ssh: SshRunner | None, poll_sleep: float = 2.0):
        self.settings = settings
        self.clusters = {c.name: c for c in clusters}
        self.read = read
        self.operator = operator
        self.nodeadmin = nodeadmin
        self.store = store
        self.db = db
        self.jobs = jobs
        self.ssh = ssh
        self.state = NodeServiceState()
        self.poll_sleep = poll_sleep
        self._tasks: list[asyncio.Task[None]] = []

    # --- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._update_loop(), name="update-check"))
        if self.ssh and self.settings.sensors_interval:
            self._tasks.append(asyncio.create_task(self._sensor_loop(), name="sensors"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _update_loop(self) -> None:
        await asyncio.sleep(5)  # let the first snapshot arrive
        while True:
            await self.check_all_updates()
            await asyncio.sleep(self.settings.update_check_interval)

    async def _sensor_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            await self.read_all_sensors()
            await asyncio.sleep(self.settings.sensors_interval)

    # --- helpers --------------------------------------------------------------------------------

    def _snap(self, cluster: str) -> ClusterSnapshot:
        snap = self.store.get(cluster)
        if snap is None or cluster not in self.clusters:
            raise NodeError(404, f"Unbekannter Cluster '{cluster}'")
        return snap

    def _node(self, cluster: str, node: str) -> Any:
        n = next((x for x in self._snap(cluster).nodes if x.node == node), None)
        if n is None:
            raise NodeError(404, f"Unbekannter Node '{node}'")
        return n

    def manage_reason(self, cluster: str) -> str | None:
        """Why node actions are not possible (None = possible)."""
        if self.settings.read_only:
            return "READ_ONLY=true"
        if cluster not in self.nodeadmin:
            return "Kein NodeAdmin-Token (PVE_n_NODEADMIN_TOKEN_ID/_SECRET)"
        return None

    def _require_manage(self, cluster: str) -> None:
        reason = self.manage_reason(cluster)
        if reason:
            raise NodeError(409, f"Node-Aktionen deaktiviert: {reason}")

    async def get_state(self, cluster: str, node: str) -> NodeState:
        async with self.db.session() as s:
            st = await s.get(NodeState, (cluster, node))
            if st is None:
                st = NodeState(cluster=cluster, node=node, maintenance=False, evacuated=[])
                s.add(st)
                await s.commit()
            return st

    async def _save_state(self, cluster: str, node: str, **values: Any) -> None:
        async with self.db.session() as s:
            st = await s.get(NodeState, (cluster, node))
            if st is None:
                st = NodeState(cluster=cluster, node=node, evacuated=[])
                s.add(st)
            for k, v in values.items():
                setattr(st, k, v)
            await s.commit()

    async def maintenance_set(self) -> set[tuple[str, str]]:
        async with self.db.session() as s:
            rows = await s.scalars(select(NodeState).where(NodeState.maintenance.is_(True)))
            return {(r.cluster, r.node) for r in rows}

    def pending_counts(self) -> dict[tuple[str, str], int]:
        return {k: len(v.packages) for k, v in self.state.updates.items() if v.error is None}

    async def ssh_target(self, cluster: str, node: str) -> SshTarget:
        cfg = self.clusters[cluster]
        n = self._node(cluster, node)
        host = cfg.ssh_hosts.get(node) or n.ip
        if not host:
            raise NodeError(409, f"Keine IP für {node} bekannt – PVE_n_SSH_HOSTS setzen")
        st = await self.get_state(cluster, node)
        return SshTarget(cluster, node, host, self.settings.ssh_port, st.ssh_host_key)

    async def _ssh(self, cluster: str, node: str, verb: str, ctx: JobContext | None = None,
                   timeout: float = 3600) -> str:
        if self.ssh is None:
            raise NodeError(409, "SSH nicht verfügbar")
        target = await self.ssh_target(cluster, node)
        try:
            code, out = await self.ssh.run(target, verb, ctx.log if ctx else None, timeout)
        except HostKeyUnknown as exc:
            raise NodeError(409, f"{exc} – unter Admin → SSH bestätigen") from exc
        except SshError as exc:
            raise NodeError(502, str(exc)) from exc
        if code != 0:
            raise NodeError(502, f"'{verb}' auf {node} endete mit Exit-Code {code}")
        return out

    async def _wait_task(self, cluster: str, upid: str, ctx: JobContext,
                         timeout: float = 1800) -> str:
        """Poll a PVE task until it stops. Returns the exit status."""
        node = upid.split(":")[1]
        client = self.read[cluster]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                st = await client.get(f"/nodes/{node}/tasks/{upid}/status")
            except (PveError, PveUnavailable) as exc:
                ctx.log(f"   Task-Status nicht abrufbar ({exc}), versuche erneut …")
            else:
                if st.get("status") == "stopped":
                    return str(st.get("exitstatus") or "unknown")
            await asyncio.sleep(self.poll_sleep)
        return "timeout"

    # --- status ---------------------------------------------------------------------------------

    async def statuses(self, cluster: str | None = None) -> list[NodeStatusView]:
        out = []
        for snap in self.store.all():
            if cluster and snap.name != cluster:
                continue
            for n in snap.nodes:
                st = await self.get_state(snap.name, n.node)
                upd = self.state.updates.get((snap.name, n.node))
                host = self.clusters[snap.name].ssh_hosts.get(n.node) or n.ip \
                    if snap.name in self.clusters else None
                out.append(NodeStatusView(
                    cluster=snap.name, node=n.node, online=n.status == "online",
                    maintenance=st.maintenance, maintenance_since=st.maintenance_since,
                    maintenance_by=st.maintenance_by, evacuated=st.evacuated or [],
                    updates=len(upd.packages) if upd and not upd.error else None,
                    updates_checked_at=upd.checked_at if upd else None,
                    updates_error=upd.error if upd else None,
                    reboot_required=upd.reboot_required if upd else None,
                    temperature=self.state.temps.get((snap.name, n.node)),
                    ssh_host=host, ssh_trusted=bool(st.ssh_host_key),
                    running_job=self.jobs.running_for(snap.name, n.node),
                    can_manage=self.manage_reason(snap.name) is None,
                    manage_reason=self.manage_reason(snap.name)))
        return out

    # --- updates --------------------------------------------------------------------------------

    async def check_updates(self, cluster: str, node: str) -> UpdateInfo:
        info = self.state.updates.get((cluster, node)) or UpdateInfo(cluster=cluster, node=node)
        client = self.nodeadmin.get(cluster)
        if client is None:
            info.error = "Kein NodeAdmin-Token – die Update-Liste braucht Sys.Modify"
        else:
            try:
                raw = await client.get(f"/nodes/{node}/apt/update")
                info.packages = sorted(
                    (Package(package=p.get("Package", "?"), old_version=p.get("OldVersion"),
                             new_version=p.get("Version", "?"), title=p.get("Title"),
                             origin=p.get("Origin"), section=p.get("Section"),
                             priority=p.get("Priority")) for p in raw or []),
                    key=lambda p: p.package)
                info.error = None
            except (PveError, PveUnavailable) as exc:
                info.error = str(exc)
        info.checked_at = datetime.now(UTC)
        if self.ssh is not None:
            try:
                st = await self.get_state(cluster, node)
                if st.ssh_host_key:
                    out = await self._ssh(cluster, node, "reboot-required", timeout=30)
                    info.reboot_required = out.strip().endswith("yes")
            except NodeError as exc:
                log.debug("reboot-required check on %s failed: %s", node, exc.message)
        self.state.updates[(cluster, node)] = info
        return info

    async def check_all_updates(self) -> None:
        for snap in self.store.all():
            for n in snap.nodes:
                if n.status == "online":
                    await self.check_updates(snap.name, n.node)

    async def refresh_package_lists(self, actor: audit.Actor, cluster: str, node: str) -> int:
        """``apt-get update`` via the API, then reload the list (as a job)."""
        self._require_manage(cluster)
        self._node(cluster, node)
        client = self.nodeadmin[cluster]

        async def run(ctx: JobContext) -> str:
            ctx.log(f"Paketlisten auf {node} aktualisieren (apt-get update über die API) …")
            try:
                upid = await client.post(f"/nodes/{node}/apt/update")
            except (PveError, PveUnavailable) as exc:
                raise JobFailed(f"Proxmox: {exc}") from exc
            status = await self._wait_task(cluster, upid, ctx, timeout=600)
            ctx.log(f"Task beendet: {status}")
            info = await self.check_updates(cluster, node)
            if status != "OK":
                raise JobFailed(f"apt-get update: {status}")
            return f"{len(info.packages)} Updates verfügbar"

        job = await self.jobs.start("apt-refresh", cluster, node, actor, run)
        return job.id

    async def changelog(self, cluster: str, node: str, package: str, version: str | None) -> str:
        client = self.nodeadmin.get(cluster)
        if client is None:
            raise NodeError(409, "Kein NodeAdmin-Token – Changelog braucht Sys.Modify")
        try:
            return await client.get(f"/nodes/{node}/apt/changelog",
                                    {"name": package, **({"version": version} if version else {})})
        except (PveError, PveUnavailable) as exc:
            raise NodeError(502, f"Proxmox: {exc}") from exc

    async def upgrade(self, actor: audit.Actor, cluster: str, node: str, confirm: str) -> int:
        """``apt-get dist-upgrade`` via SSH with live output."""
        self._require_manage(cluster)
        n = self._node(cluster, node)
        if confirm != node:
            raise NodeError(422, f"Zur Bestätigung den Node-Namen '{node}' eingeben")
        if n.status != "online":
            raise NodeError(409, f"{node} ist nicht online")
        target = await self.ssh_target(cluster, node)
        if not target.host_key:
            raise NodeError(409, f"SSH-Host-Key von {node} ist noch nicht bestätigt (Admin → SSH)")
        before = self.state.updates.get((cluster, node))
        count = len(before.packages) if before else None

        async def run(ctx: JobContext) -> str:
            ctx.log(f"Upgrade auf {node} ({target.host}) – {count if count is not None else '?'} "
                    "Paket(e)")
            ctx.log("$ apt-get update && apt-get dist-upgrade")
            try:
                await self._ssh(cluster, node, "upgrade", ctx, timeout=3600)
            except NodeError as exc:
                raise JobFailed(exc.message) from exc
            info = await self.check_updates(cluster, node)
            summary = f"Upgrade abgeschlossen, {len(info.packages)} Update(s) übrig"
            if info.reboot_required:
                summary += " – Neustart erforderlich (neuer Kernel)"
                ctx.log("Neustart erforderlich: ein neuerer Kernel ist installiert.")
            return summary

        job = await self.jobs.start("upgrade", cluster, node, actor, run,
                                    params={"packages": count})
        return job.id

    # --- sensors / DMI --------------------------------------------------------------------------

    async def read_all_sensors(self) -> None:
        for snap in self.store.all():
            for n in snap.nodes:
                if n.status != "online":
                    continue
                st = await self.get_state(snap.name, n.node)
                if not st.ssh_host_key:
                    continue
                try:
                    out = await self._ssh(snap.name, n.node, "sensors", timeout=20)
                    t = max_temperature(json.loads(out or "{}"))
                    if t is not None:
                        self.state.temps[(snap.name, n.node)] = t
                except (NodeError, ValueError) as exc:
                    log.debug("sensors on %s failed: %s", n.node, exc)

    async def mainboard(self, cluster: str, node: str) -> str | None:
        """'Vendor Board (BIOS x)' via SSH, cached; None if SSH is not set up."""
        if (cluster, node) not in self.state.dmi:
            st = await self.get_state(cluster, node)
            if not st.ssh_host_key or self.ssh is None:
                return None
            try:
                self.state.dmi[(cluster, node)] = json.loads(
                    await self._ssh(cluster, node, "dmi", timeout=20))
            except (NodeError, ValueError):
                return None
        d = self.state.dmi[(cluster, node)]
        name = " ".join(x for x in (d.get("board_vendor"), d.get("board_name")) if x)
        return f"{name} (BIOS {d['bios_version']})" if d.get("bios_version") else name or None

    # --- SSH host keys --------------------------------------------------------------------------

    async def scan_host_key(self, cluster: str, node: str) -> dict[str, str]:
        if self.ssh is None:
            raise NodeError(409, "SSH nicht verfügbar")
        target = await self.ssh_target(cluster, node)
        try:
            key, fp = await self.ssh.scan_host_key(target.host, target.port)
        except SshError as exc:
            raise NodeError(502, str(exc)) from exc
        return {"host": target.host, "key": key, "fingerprint": fp}

    async def trust_host_key(self, cluster: str, node: str, key: str | None) -> None:
        self._node(cluster, node)
        await self._save_state(cluster, node, ssh_host_key=key)
        self.state.dmi.pop((cluster, node), None)

    # --- maintenance ----------------------------------------------------------------------------

    def _pick_target(self, snap: ClusterSnapshot, node: str, guest: GuestView,
                     allowed: list[str], planned: dict[str, int],
                     maintenance: set[tuple[str, str]]) -> str | None:
        """Node with the most free RAM (minus what we already planned to move there)."""
        best, best_free = None, -1
        for n in snap.nodes:
            if n.node == node or n.status != "online" or n.node not in allowed:
                continue
            if (snap.name, n.node) in maintenance:
                continue
            free = n.maxmem - n.mem - planned.get(n.node, 0)
            if free > best_free:
                best, best_free = n.node, free
        if best:
            planned[best] = planned.get(best, 0) + guest.maxmem
        return best

    async def _migratable(self, cluster: str, g: GuestView) -> tuple[list[str], str | None]:
        """Allowed target nodes, or ([], reason)."""
        snap = self._snap(cluster)
        others = [n.node for n in snap.nodes if n.status == "online" and n.node != g.node]
        if g.type == "lxc":
            return others, None
        try:
            pre = await self.read[cluster].get(f"/nodes/{g.node}/qemu/{g.vmid}/migrate")
        except (PveError, PveUnavailable) as exc:
            return [], f"Migrations-Prüfung fehlgeschlagen: {exc}"
        allowed = [n for n in pre.get("allowed_nodes", others) if n != g.node]
        if not allowed:
            res = ", ".join(pre.get("local_resources") or []) or "keine erlaubten Ziele"
            return [], f"nicht migrierbar ({res})"
        return allowed, None

    async def maintenance_plan(self, cluster: str, node: str) -> list[dict[str, Any]]:
        """What would happen to each running guest (for the confirmation dialog)."""
        snap = self._snap(cluster)
        self._node(cluster, node)
        maint = await self.maintenance_set()
        planned: dict[str, int] = {}
        plan = []
        for g in sorted((g for g in snap.guests if g.node == node and g.status == "running"),
                        key=lambda g: g.vmid):
            allowed, reason = await self._migratable(cluster, g)
            target = self._pick_target(snap, node, g, allowed, planned, maint) if allowed else None
            plan.append({"vmid": g.vmid, "name": g.name, "type": g.type, "target": target,
                         "reason": reason if not target else None,
                         "restart": g.type == "lxc"})
        return plan

    async def enter_maintenance(self, actor: audit.Actor, cluster: str, node: str,
                                confirm: str, shutdown_unmigratable: bool) -> int:
        self._require_manage(cluster)
        self._node(cluster, node)
        if confirm != node:
            raise NodeError(422, f"Zur Bestätigung den Node-Namen '{node}' eingeben")
        if cluster not in self.operator:
            raise NodeError(409, "Kein Operator-Token – Gäste können nicht migriert werden")
        op = self.operator[cluster]

        async def run(ctx: JobContext) -> str:
            await self._save_state(cluster, node, maintenance=True, maintenance_since=utcnow(),
                                   maintenance_by=actor.name)
            ctx.log(f"Wartungsmodus für {node} aktiviert.")
            plan = await self.maintenance_plan(cluster, node)
            if not plan:
                ctx.log("Keine laufenden Gäste – nichts zu verschieben.")
            moved, stopped, failed = [], [], []
            for p in plan:
                label = f"{p['vmid']} {p['name']}"
                gtype = p["type"]
                if p["target"]:
                    ctx.log(f"→ {label}: migriere nach {p['target']}"
                            f"{' (Neustart-Migration)' if p['restart'] else ' (live)'} …")
                    data: dict[str, Any] = ({"target": p["target"], "online": True,
                                             "with-local-disks": True} if gtype == "qemu"
                                            else {"target": p["target"], "restart": True})
                    try:
                        upid = await op.post(f"/nodes/{node}/{gtype}/{p['vmid']}/migrate", data)
                        status = await self._wait_task(cluster, upid, ctx)
                    except (PveError, PveUnavailable) as exc:
                        status = str(exc)
                    if status == "OK":
                        ctx.log(f"  ✓ {label} läuft jetzt auf {p['target']}")
                        moved.append({"vmid": p["vmid"], "name": p["name"], "type": gtype,
                                      "target": p["target"]})
                        continue
                    ctx.log(f"  ✗ Migration fehlgeschlagen: {status}")
                    failed.append(label)
                    continue
                ctx.log(f"→ {label}: {p['reason']}")
                if shutdown_unmigratable:
                    ctx.log(f"  fahre {label} sauber herunter …")
                    try:
                        upid = await op.post(f"/nodes/{node}/{gtype}/{p['vmid']}/status/shutdown",
                                             {"timeout": 180})
                        status = await self._wait_task(cluster, upid, ctx, timeout=300)
                    except (PveError, PveUnavailable) as exc:
                        status = str(exc)
                    if status == "OK":
                        stopped.append({"vmid": p["vmid"], "name": p["name"], "type": gtype,
                                        "target": None})
                        ctx.log(f"  ✓ {label} heruntergefahren")
                    else:
                        failed.append(label)
                        ctx.log(f"  ✗ Herunterfahren fehlgeschlagen: {status}")
                else:
                    ctx.log("  bleibt laufen (nicht migrierbar)")
                    failed.append(f"{label} (läuft weiter)")
            await self._save_state(cluster, node, evacuated=moved + stopped)
            summary = f"{len(moved)} migriert, {len(stopped)} heruntergefahren"
            if failed:
                summary += f", offen: {', '.join(failed)}"
            return summary

        job = await self.jobs.start("maintenance-on", cluster, node, actor, run,
                                    params={"shutdown_unmigratable": shutdown_unmigratable})
        return job.id

    async def exit_maintenance(self, actor: audit.Actor, cluster: str, node: str,
                               return_guests: bool) -> int:
        self._require_manage(cluster)
        self._node(cluster, node)
        op = self.operator.get(cluster)

        async def run(ctx: JobContext) -> str:
            st = await self.get_state(cluster, node)
            evacuated = list(st.evacuated or [])
            back = 0
            if return_guests and evacuated:
                if op is None:
                    raise JobFailed("Kein Operator-Token – Gäste können nicht zurückgeholt werden")
                snap = self._snap(cluster)
                for e in evacuated:
                    g = next((x for x in snap.guests if x.vmid == e["vmid"]), None)
                    if g is None:
                        ctx.log(f"→ {e['vmid']} {e['name']}: existiert nicht mehr")
                        continue
                    if e.get("target") is None:  # was shut down -> start again
                        if g.node == node and g.status == "stopped":
                            ctx.log(f"→ {g.vmid} {g.name}: starte …")
                            upid = await op.post(f"/nodes/{node}/{g.type}/{g.vmid}/status/start")
                            ctx.log(f"  {await self._wait_task(cluster, upid, ctx)}")
                            back += 1
                        continue
                    if g.node == node:
                        continue
                    ctx.log(f"→ {g.vmid} {g.name}: zurück von {g.node} nach {node} …")
                    data: dict[str, Any] = ({"target": node, "online": g.status == "running",
                                             "with-local-disks": True} if g.type == "qemu"
                                            else {"target": node, "restart": g.status == "running"})
                    try:
                        upid = await op.post(f"/nodes/{g.node}/{g.type}/{g.vmid}/migrate", data)
                        status = await self._wait_task(cluster, upid, ctx)
                    except (PveError, PveUnavailable) as exc:
                        status = str(exc)
                    ctx.log(f"  {'✓' if status == 'OK' else '✗'} {status}")
                    back += status == "OK"
            await self._save_state(cluster, node, maintenance=False, maintenance_since=None,
                                   maintenance_by=None, evacuated=[])
            ctx.log(f"Wartungsmodus für {node} beendet.")
            return f"Wartung beendet, {back} Gast/Gäste zurückgeholt" if return_guests \
                else "Wartung beendet"

        job = await self.jobs.start("maintenance-off", cluster, node, actor, run,
                                    params={"return_guests": return_guests})
        return job.id

    # --- reboot ---------------------------------------------------------------------------------

    async def reboot_precheck(self, cluster: str, node: str) -> dict[str, Any]:
        snap = self._snap(cluster)
        n = self._node(cluster, node)
        st = await self.get_state(cluster, node)
        running = [{"vmid": g.vmid, "name": g.name} for g in snap.guests
                   if g.node == node and g.status == "running"]
        quorum_ok, quorum_msg = quorum_after_reboot(snap, node)
        blockers, warnings = [], []
        if n.status != "online":
            blockers.append(f"{node} ist nicht online")
        if not quorum_ok:
            blockers.append(quorum_msg)
        if running:
            warnings.append(f"{len(running)} Gast/Gäste laufen noch und werden mit dem Node "
                            "heruntergefahren – vorher Wartungsmodus aktivieren")
        if not st.maintenance:
            warnings.append("Node ist nicht im Wartungsmodus")
        others_in_maint = [x for c, x in await self.maintenance_set()
                           if c == cluster and x != node]
        if others_in_maint:
            warnings.append(f"Weitere Nodes in Wartung: {', '.join(others_in_maint)}")
        return {"ok": not blockers, "blockers": blockers, "warnings": warnings,
                "quorum": quorum_msg, "running_guests": running, "maintenance": st.maintenance,
                "manage_reason": self.manage_reason(cluster)}

    async def reboot(self, actor: audit.Actor, cluster: str, node: str, confirm: str,
                     force: bool) -> int:
        self._require_manage(cluster)
        if confirm != node:
            raise NodeError(422, f"Zur Bestätigung den Node-Namen '{node}' eingeben")
        pre = await self.reboot_precheck(cluster, node)
        if not pre["ok"]:
            raise NodeError(409, "; ".join(pre["blockers"]))
        if pre["running_guests"] and not force:
            raise NodeError(409, "Es laufen noch Gäste – Wartungsmodus aktivieren oder "
                                 "'force' setzen")
        client = self.nodeadmin[cluster]
        timeout = self.settings.reboot_timeout

        async def run(ctx: JobContext) -> str:
            ctx.log(f"Vorab-Prüfung: {pre['quorum']}")
            for w in pre["warnings"]:
                ctx.log(f"Hinweis: {w}")
            ctx.log(f"Sende Reboot an {node} …")
            try:
                await client.post(f"/nodes/{node}/status", {"command": "reboot"})
            except (PveError, PveUnavailable) as exc:
                raise JobFailed(f"Proxmox: {exc}") from exc
            started = time.monotonic()
            went_down = False
            while time.monotonic() - started < timeout:
                await asyncio.sleep(self.poll_sleep)
                n = next((x for x in self._snap(cluster).nodes if x.node == node), None)
                online = n is not None and n.status == "online"
                if not went_down and not online:
                    went_down = True
                    ctx.log(f"{node} ist offline ({time.monotonic() - started:.0f} s) – warte …")
                elif went_down and online and n is not None and n.uptime < timeout:
                    ctx.log(f"{node} ist wieder online nach {time.monotonic() - started:.0f} s.")
                    self.state.dmi.pop((cluster, node), None)
                    info = await self.check_updates(cluster, node)
                    if info.reboot_required:
                        ctx.log("Hinweis: reboot-required meldet weiterhin 'yes'.")
                    return f"Neustart erfolgreich ({time.monotonic() - started:.0f} s)"
            raise JobFailed(f"{node} ist nach {timeout:.0f} s nicht zurück"
                            if went_down else f"{node} ging nicht offline – Reboot ausgeführt?")

        job = await self.jobs.start("reboot", cluster, node, actor, run,
                                    params={"force": force})
        return job.id
