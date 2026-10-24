"""Control actions on guests, with safety checks, audit log and task tracking.

Every action
1. resolves the guest from the live snapshot (node, type, name),
2. checks the global switches (READ_ONLY, operator token) and - for destructive actions -
   that the caller typed the guest name,
3. writes an audit entry, executes (or simulates with DRY_RUN) and stores the task UPID,
4. leaves the final result to the :class:`TaskWatcher`, which follows the Proxmox task.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Literal

from pydantic import BaseModel

from app import audit
from app.collector.models import GuestView
from app.collector.state import SnapshotStore
from app.config import ClusterConfig, Settings
from app.db.engine import Database
from app.pve import PveApi, PveError, PveUnavailable

log = logging.getLogger(__name__)

PowerAction = Literal["start", "shutdown", "stop", "reboot", "suspend", "resume"]
DESTRUCTIVE_POWER = {"stop"}  # hard power-off: data loss possible
SNAPNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
BULK_CONCURRENCY = 3


class ActionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ActionResult(BaseModel):
    audit_id: int
    cluster: str
    node: str
    vmid: int | None = None
    action: str
    dry_run: bool
    upid: str | None = None
    message: str | None = None


@dataclass
class Target:
    cluster: ClusterConfig
    guest: GuestView

    @property
    def base(self) -> str:
        return f"/nodes/{self.guest.node}/{self.guest.type}/{self.guest.vmid}"


class ActionService:
    def __init__(self, settings: Settings, clusters: list[ClusterConfig],
                 clients: dict[str, PveApi], store: SnapshotStore, db: Database):
        self.settings = settings
        self.clusters = {c.name: c for c in clusters}
        self.clients = clients  # operator-token clients (mock: shared instance)
        self.store = store
        self.db = db

    # --- capability -----------------------------------------------------------------------------

    def availability(self, cluster: str) -> tuple[bool, str | None]:
        if self.settings.read_only:
            return False, "READ_ONLY=true - Steuerung global deaktiviert"
        cfg = self.clusters.get(cluster)
        if cfg is None:
            return False, "Unbekannter Cluster"
        if cluster not in self.clients:
            return False, "Kein Operator-Token konfiguriert (PVE_n_OPERATOR_TOKEN_ID/_SECRET)"
        return True, None

    def _require(self, cluster: str) -> None:
        ok, why = self.availability(cluster)
        if not ok:
            raise ActionError(409 if cluster in self.clusters else 404, why or "")

    def target(self, cluster: str, vmid: int) -> Target:
        cfg = self.clusters.get(cluster)
        snap = self.store.get(cluster)
        guest = next((g for g in snap.guests if g.vmid == vmid), None) if snap else None
        if cfg is None or guest is None:
            raise ActionError(404, f"Gast {vmid} in Cluster '{cluster}' nicht gefunden")
        if guest.template:
            raise ActionError(409, "Templates können nicht gesteuert werden")
        return Target(cfg, guest)

    @staticmethod
    def _confirm(t: Target, confirm: str | None) -> None:
        if (confirm or "").strip() != t.guest.name:
            raise ActionError(422, f"Zur Bestätigung den Gast-Namen '{t.guest.name}' eingeben")

    # --- execution ------------------------------------------------------------------------------

    async def _execute(self, actor: audit.Actor, t: Target | None, action: str, *,
                       cluster: str, node: str, call: tuple[str, str, dict[str, Any] | None],
                       params: dict[str, Any] | None = None) -> ActionResult:
        """Run one PVE write call (method, path, data) with audit trail."""
        self._require(cluster)
        method, path, data = call
        vmid = t.guest.vmid if t else None
        name = t.guest.name if t else None
        dry = self.settings.dry_run
        common = {"cluster": cluster, "node": node, "vmid": vmid, "target": name,
                  "params": params or {}, "dry_run": dry}
        if dry:
            entry = await audit.record(self.db, actor, action, "ok",
                                       message=f"Dry-Run: {method} {path}", **common)
            return ActionResult(audit_id=entry, cluster=cluster, node=node, vmid=vmid,
                                action=action, dry_run=True,
                                message=f"Dry-Run – nicht ausgeführt: {method} {path}")
        client = self.clients[cluster]
        try:
            if method == "POST":
                upid = await client.post(path, data)
            else:
                upid = await client.delete(path, data)
        except (PveError, PveUnavailable) as exc:
            msg = exc.message if isinstance(exc, PveError) else str(exc)
            entry = await audit.record(self.db, actor, action, "error", message=msg, **common)
            status = 403 if isinstance(exc, PveError) and exc.status == 403 else 502
            raise ActionError(status, f"Proxmox: {msg}") from exc
        upid = upid if isinstance(upid, str) and upid.startswith("UPID:") else None
        entry = await audit.record(self.db, actor, action, "submitted" if upid else "ok",
                                   upid=upid, **common)
        return ActionResult(audit_id=entry, cluster=cluster, node=node, vmid=vmid, action=action,
                            dry_run=False, upid=upid)

    # --- guest actions --------------------------------------------------------------------------

    async def power(self, actor: audit.Actor, cluster: str, vmid: int, action: PowerAction,
                    confirm: str | None = None, timeout: int | None = None) -> ActionResult:
        t = self.target(cluster, vmid)
        if action in DESTRUCTIVE_POWER:
            self._confirm(t, confirm)
        data: dict[str, Any] = {}
        if action == "shutdown" and timeout:
            data["timeout"] = timeout
        return await self._execute(actor, t, f"guest.{action}", cluster=cluster,
                                   node=t.guest.node,
                                   call=("POST", f"{t.base}/status/{action}", data or None),
                                   params=data)

    async def list_snapshots(self, cluster: str, vmid: int,
                             read_client: PveApi) -> list[dict[str, Any]]:
        t = self.target(cluster, vmid)
        try:
            snaps = await read_client.get(f"{t.base}/snapshot")
        except PveError as exc:
            raise ActionError(502, f"Proxmox: {exc.message}") from exc
        return sorted((s for s in snaps if s.get("name") != "current"),
                      key=lambda s: s.get("snaptime", 0), reverse=True)

    async def snapshot_create(self, actor: audit.Actor, cluster: str, vmid: int, name: str,
                              description: str = "", vmstate: bool = False) -> ActionResult:
        t = self.target(cluster, vmid)
        if not SNAPNAME.match(name):
            raise ActionError(422, "Snapshot-Name: 2–40 Zeichen, beginnt mit einem Buchstaben, "
                                   "nur A–Z, 0–9, _ und -")
        data: dict[str, Any] = {"snapname": name, "description": description[:500] or None}
        if t.guest.type == "qemu" and vmstate and t.guest.status == "running":
            data["vmstate"] = True
        return await self._execute(actor, t, "guest.snapshot.create", cluster=cluster,
                                   node=t.guest.node, call=("POST", f"{t.base}/snapshot", data),
                                   params={k: v for k, v in data.items() if v is not None})

    async def snapshot_rollback(self, actor: audit.Actor, cluster: str, vmid: int, name: str,
                                confirm: str | None) -> ActionResult:
        t = self.target(cluster, vmid)
        self._confirm(t, confirm)
        self._check_snapname(name)
        return await self._execute(actor, t, "guest.snapshot.rollback", cluster=cluster,
                                   node=t.guest.node,
                                   call=("POST", f"{t.base}/snapshot/{name}/rollback", None),
                                   params={"snapname": name})

    async def snapshot_delete(self, actor: audit.Actor, cluster: str, vmid: int, name: str,
                              confirm: str | None) -> ActionResult:
        t = self.target(cluster, vmid)
        self._confirm(t, confirm)
        self._check_snapname(name)
        return await self._execute(actor, t, "guest.snapshot.delete", cluster=cluster,
                                   node=t.guest.node,
                                   call=("DELETE", f"{t.base}/snapshot/{name}", None),
                                   params={"snapname": name})

    @staticmethod
    def _check_snapname(name: str) -> None:
        if not SNAPNAME.match(name):
            raise ActionError(422, "Ungültiger Snapshot-Name")

    def backup_storages(self, cluster: str, node: str) -> list[str]:
        snap = self.store.get(cluster)
        return sorted({s.storage for s in (snap.storages if snap else [])
                       if s.node == node and "backup" in s.content and s.status == "available"})

    async def backup(self, actor: audit.Actor, cluster: str, vmid: int, storage: str,
                     mode: Literal["snapshot", "suspend", "stop"] = "snapshot",
                     notes: str | None = None) -> ActionResult:
        t = self.target(cluster, vmid)
        if storage not in self.backup_storages(cluster, t.guest.node):
            raise ActionError(422, f"Storage '{storage}' ist auf {t.guest.node} kein Backup-Ziel")
        data = {"vmid": vmid, "storage": storage, "mode": mode, "compress": "zstd",
                "notes-template": (notes or "{{guestname}} – manuell über Dashboard")[:200]}
        return await self._execute(actor, t, "guest.backup", cluster=cluster, node=t.guest.node,
                                   call=("POST", f"/nodes/{t.guest.node}/vzdump", data),
                                   params={"storage": storage, "mode": mode})

    async def migrate_precheck(self, cluster: str, vmid: int,
                               read_client: PveApi) -> dict[str, Any]:
        t = self.target(cluster, vmid)
        snap = self.store.get(cluster)
        online = [n.node for n in (snap.nodes if snap else [])
                  if n.status == "online" and n.node != t.guest.node]
        if t.guest.type == "lxc":
            # No precheck endpoint for containers; running CTs migrate in restart mode.
            return {"allowed_nodes": online, "not_allowed": {}, "local_disks": [],
                    "local_resources": [], "online": False,
                    "note": "Container werden für die Migration neu gestartet (restart mode)."
                    if t.guest.status == "running" else None}
        try:
            res = await read_client.get(f"{t.base}/migrate")
        except PveError as exc:
            raise ActionError(502, f"Proxmox: {exc.message}") from exc
        # An empty list is a real answer ("nowhere"), only a missing key means "unknown".
        allowed = res.get("allowed_nodes", online)
        return {"allowed_nodes": [n for n in allowed if n != t.guest.node],
                "not_allowed": res.get("not_allowed_nodes") or {},
                "local_disks": [d.get("volid") for d in res.get("local_disks") or []],
                "local_resources": res.get("local_resources") or [],
                "online": t.guest.status == "running", "note": None}

    async def migrate(self, actor: audit.Actor, cluster: str, vmid: int, target_node: str,
                      confirm: str | None = None) -> ActionResult:
        t = self.target(cluster, vmid)
        snap = self.store.get(cluster)
        online = {n.node for n in (snap.nodes if snap else []) if n.status == "online"}
        if target_node not in online or target_node == t.guest.node:
            raise ActionError(422, f"Ziel-Node '{target_node}' ungültig oder offline")
        running = t.guest.status == "running"
        if t.guest.type == "qemu":
            data: dict[str, Any] = {"target": target_node, "online": running,
                                    "with-local-disks": True}
        else:
            # Restart migration stops the container briefly - treat as destructive.
            if running:
                self._confirm(t, confirm)
            data = {"target": target_node, "restart": running}
        return await self._execute(actor, t, "guest.migrate", cluster=cluster, node=t.guest.node,
                                   call=("POST", f"{t.base}/migrate", data),
                                   params={"target": target_node, "online": running})

    # --- bulk -----------------------------------------------------------------------------------

    def select(self, cluster: str, vmids: list[int] | None, tag: str | None) -> list[GuestView]:
        snap = self.store.get(cluster)
        if snap is None:
            raise ActionError(404, f"Cluster '{cluster}' unbekannt")
        guests = [g for g in snap.guests if not g.template]
        if vmids:
            wanted = set(vmids)
            guests = [g for g in guests if g.vmid in wanted]
        elif tag:
            guests = [g for g in guests if tag in g.tags]
        else:
            raise ActionError(422, "vmids oder tag angeben")
        if not guests:
            raise ActionError(404, "Keine passenden Gäste")
        return guests

    async def bulk_power(self, actor: audit.Actor, cluster: str, action: PowerAction,
                         vmids: list[int] | None, tag: str | None,
                         confirm: str | None) -> list[dict[str, Any]]:
        self._require(cluster)
        guests = self.select(cluster, vmids, tag)
        # Skip guests that are already in the target state instead of producing errors.
        relevant = {"start": {"stopped"}, "resume": {"paused", "suspended"}}.get(
            action, {"running"})
        todo = [g for g in guests if g.status in relevant]
        if action in DESTRUCTIVE_POWER:
            expected = f"{action} {len(todo)}"
            if (confirm or "").strip().lower() != expected:
                raise ActionError(422, f"Zur Bestätigung '{expected}' eingeben")
        sem = asyncio.Semaphore(BULK_CONCURRENCY)

        async def one(g: GuestView) -> dict[str, Any]:
            async with sem:
                try:
                    r = await self.power(actor, cluster, g.vmid, action, confirm=g.name)
                    return {"vmid": g.vmid, "name": g.name, "ok": True, "upid": r.upid,
                            "audit_id": r.audit_id, "dry_run": r.dry_run}
                except ActionError as exc:
                    return {"vmid": g.vmid, "name": g.name, "ok": False, "error": exc.message}

        results = list(await asyncio.gather(*(one(g) for g in todo)))
        results += [{"vmid": g.vmid, "name": g.name, "ok": True, "skipped": True,
                     "error": f"bereits {g.status}"} for g in guests if g not in todo]
        return sorted(results, key=lambda r: r["vmid"])


class TaskWatcher:
    """Follows submitted Proxmox tasks and writes the final result into the audit log."""

    def __init__(self, db: Database, clients: dict[str, PveApi], interval: float = 3.0,
                 give_up_after: float = 86400.0):
        self.db = db
        self.clients = clients  # read clients (Sys.Audit sees all tasks)
        self.interval = interval
        self.give_up_after = give_up_after
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="task-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:  # noqa: BLE001
                log.exception("Task watcher iteration failed")
            await asyncio.sleep(self.interval)

    async def check_once(self) -> None:
        for entry in await audit.pending_tasks(self.db):
            client = self.clients.get(entry.cluster or "")
            if client is None or not entry.upid:
                continue
            node = entry.upid.split(":")[1]
            try:
                st = await client.get(f"/nodes/{node}/tasks/{entry.upid}/status")
            except (PveError, PveUnavailable) as exc:
                ts = entry.ts if entry.ts.tzinfo else entry.ts.replace(tzinfo=UTC)
                age = time.time() - ts.timestamp()
                if age > self.give_up_after:
                    await audit.finish(self.db, entry.id, "unknown", f"Task-Status: {exc}")
                continue
            if st.get("status") == "running":
                if entry.status != "running":
                    await audit.finish(self.db, entry.id, "running")
                continue
            exit_status = str(st.get("exitstatus") or "")
            ok = exit_status == "OK" or exit_status.startswith("WARNINGS")
            await audit.finish(self.db, entry.id, "ok" if ok else "error",
                               None if exit_status == "OK" else exit_status)
