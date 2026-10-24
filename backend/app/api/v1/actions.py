"""Control actions, Proxmox tasks and the audit log."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import audit
from app.actions.service import ActionError, ActionResult, ActionService, PowerAction
from app.auth.deps import Principal, actor, admin, operator, viewer
from app.pve import PveApi, PveError, PveUnavailable

router = APIRouter(prefix="/api/v1", tags=["actions"])

UPID = re.compile(r"^UPID:[\w.-]+:[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8,}:[0-9A-Fa-f]{8}:[\w-]+:[\w.-]*:"
                  r"[\w.@!-]+:$")


def _svc(request: Request) -> ActionService:
    return request.app.state.actions


def _read_client(request: Request, cluster: str) -> PveApi:
    client = request.app.state.clients.get(cluster)
    if client is None:
        raise HTTPException(404, f"Unknown cluster '{cluster}'")
    return client


async def _run(coro: Any) -> Any:
    try:
        return await coro
    except ActionError as exc:
        raise HTTPException(exc.status, exc.message) from exc


# --- capabilities ---------------------------------------------------------------------------


class Capability(BaseModel):
    cluster: str
    enabled: bool
    reason: str | None
    dry_run: bool


@router.get("/actions/capabilities", response_model=list[Capability])
async def capabilities(request: Request, _: Principal = viewer) -> list[Capability]:
    svc = _svc(request)
    out = []
    for name in svc.clusters:
        ok, why = svc.availability(name)
        out.append(Capability(cluster=name, enabled=ok, reason=why,
                              dry_run=request.app.state.settings.dry_run))
    return out


# --- guest actions --------------------------------------------------------------------------


class PowerRequest(BaseModel):
    action: PowerAction
    confirm: str | None = Field(None, description="Guest name - required for 'stop'")
    timeout: int | None = Field(None, ge=10, le=3600, description="Shutdown timeout (s)")


class SnapshotCreate(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    description: str = Field("", max_length=500)
    vmstate: bool = Field(False, description="Include RAM (running VMs only)")


class Confirm(BaseModel):
    confirm: str = Field(description="Guest name")


class BackupRequest(BaseModel):
    storage: str
    mode: Literal["snapshot", "suspend", "stop"] = "snapshot"
    notes: str | None = Field(None, max_length=200)


class MigrateRequest(BaseModel):
    target: str
    confirm: str | None = Field(None, description="Guest name - required for running LXC")


class BulkRequest(BaseModel):
    cluster: str
    action: PowerAction
    vmids: list[int] | None = Field(None, max_length=500)
    tag: str | None = None
    confirm: str | None = Field(None, description="'stop <count>' for hard stop")


G = "/clusters/{cluster}/guests/{vmid}"


@router.post(G + "/power", response_model=ActionResult)
async def power(request: Request, cluster: str, vmid: int, body: PowerRequest,
                info: Principal = operator) -> ActionResult:
    return await _run(_svc(request).power(actor(request, info), cluster, vmid, body.action,
                                          body.confirm, body.timeout))


@router.get(G + "/snapshots")
async def snapshots(request: Request, cluster: str, vmid: int,
                    _: Principal = viewer) -> list[dict[str, Any]]:
    return await _run(_svc(request).list_snapshots(cluster, vmid, _read_client(request, cluster)))


@router.post(G + "/snapshots", response_model=ActionResult)
async def snapshot_create(request: Request, cluster: str, vmid: int, body: SnapshotCreate,
                          info: Principal = operator) -> ActionResult:
    return await _run(_svc(request).snapshot_create(actor(request, info), cluster, vmid,
                                                    body.name, body.description, body.vmstate))


@router.post(G + "/snapshots/{name}/rollback", response_model=ActionResult)
async def snapshot_rollback(request: Request, cluster: str, vmid: int, name: str, body: Confirm,
                            info: Principal = operator) -> ActionResult:
    return await _run(_svc(request).snapshot_rollback(actor(request, info), cluster, vmid, name,
                                                      body.confirm))


@router.delete(G + "/snapshots/{name}", response_model=ActionResult)
async def snapshot_delete(request: Request, cluster: str, vmid: int, name: str,
                          confirm: Annotated[str, Query(description="Guest name")],
                          info: Principal = operator) -> ActionResult:
    return await _run(_svc(request).snapshot_delete(actor(request, info), cluster, vmid, name,
                                                    confirm))


@router.get(G + "/backup-storages")
async def backup_storages(request: Request, cluster: str, vmid: int,
                          _: Principal = viewer) -> list[str]:
    svc = _svc(request)
    try:
        t = svc.target(cluster, vmid)
    except ActionError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return svc.backup_storages(cluster, t.guest.node)


@router.post(G + "/backup", response_model=ActionResult)
async def backup(request: Request, cluster: str, vmid: int, body: BackupRequest,
                 info: Principal = operator) -> ActionResult:
    return await _run(_svc(request).backup(actor(request, info), cluster, vmid, body.storage,
                                           body.mode, body.notes))


@router.get(G + "/migrate")
async def migrate_precheck(request: Request, cluster: str, vmid: int,
                           _: Principal = viewer) -> dict[str, Any]:
    return await _run(_svc(request).migrate_precheck(cluster, vmid,
                                                     _read_client(request, cluster)))


@router.post(G + "/migrate", response_model=ActionResult)
async def migrate(request: Request, cluster: str, vmid: int, body: MigrateRequest,
                  info: Principal = operator) -> ActionResult:
    return await _run(_svc(request).migrate(actor(request, info), cluster, vmid, body.target,
                                            body.confirm))


@router.post("/actions/bulk", summary="Power action for several guests (by VMIDs or tag)")
async def bulk(request: Request, body: BulkRequest,
               info: Principal = operator) -> list[dict[str, Any]]:
    return await _run(_svc(request).bulk_power(actor(request, info), body.cluster, body.action,
                                               body.vmids, body.tag, body.confirm))


# --- tasks ----------------------------------------------------------------------------------


class TaskView(BaseModel):
    cluster: str
    upid: str
    node: str
    type: str
    id: str | None
    user: str | None
    starttime: int
    endtime: int | None
    status: str | None  # None = running, "OK" or error text


@router.get("/tasks", response_model=list[TaskView], tags=["tasks"])
async def tasks(request: Request, cluster: str | None = None, vmid: int | None = None,
                errors_only: bool = False,
                limit: Annotated[int, Query(ge=1, le=500)] = 100,
                _: Principal = viewer) -> list[TaskView]:
    out: list[TaskView] = []
    for name, client in request.app.state.clients.items():
        if cluster and name != cluster:
            continue
        try:
            rows = await client.get("/cluster/tasks")
        except (PveError, PveUnavailable):
            continue
        for r in rows or []:
            if vmid is not None and str(r.get("id")) != str(vmid):
                continue
            status = r.get("status")
            if errors_only and (status is None or status == "OK"):
                continue
            out.append(TaskView(cluster=name, upid=r["upid"], node=r.get("node", ""),
                                type=r.get("type", ""), id=r.get("id") or None,
                                user=r.get("user"), starttime=int(r.get("starttime", 0)),
                                endtime=r.get("endtime"), status=status))
    out.sort(key=lambda t: t.starttime, reverse=True)
    return out[:limit]


def _task_path(upid: str) -> str:
    if not UPID.match(upid):
        raise HTTPException(422, "Ungültige UPID")
    return f"/nodes/{upid.split(':')[1]}/tasks/{upid}"


@router.get("/tasks/{cluster}/{upid}/status", tags=["tasks"])
async def task_status(request: Request, cluster: str, upid: str,
                      _: Principal = viewer) -> dict[str, Any]:
    try:
        return await _read_client(request, cluster).get(_task_path(upid) + "/status")
    except (PveError, PveUnavailable) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/tasks/{cluster}/{upid}/log", tags=["tasks"])
async def task_log(request: Request, cluster: str, upid: str,
                   start: Annotated[int, Query(ge=0)] = 0,
                   limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
                   _: Principal = viewer) -> list[dict[str, Any]]:
    try:
        return await _read_client(request, cluster).get(_task_path(upid) + "/log",
                                                        {"start": start, "limit": limit})
    except (PveError, PveUnavailable) as exc:
        raise HTTPException(502, str(exc)) from exc


# --- audit log ------------------------------------------------------------------------------


@router.get("/audit", response_model=list[audit.AuditView], tags=["audit"],
            summary="Audit log (admins: everything, others: own entries)")
async def audit_log(request: Request, info: Principal = viewer, actor_name: str | None = None,
                    action: str | None = None, status: str | None = None,
                    cluster: str | None = None, vmid: int | None = None,
                    before_id: int | None = None,
                    limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[audit.AuditView]:
    if info.role != "admin":
        actor_name = info.name
    return await audit.query(request.app.state.db, actor=actor_name, action=action, status=status,
                             cluster=cluster, vmid=vmid, before_id=before_id, limit=limit)


@router.get("/audit/{entry_id}", response_model=audit.AuditView, tags=["audit"])
async def audit_entry(request: Request, entry_id: int,
                      info: Principal = admin) -> audit.AuditView:
    rows = await audit.query(request.app.state.db, before_id=entry_id + 1, limit=1)
    if not rows or rows[0].id != entry_id:
        raise HTTPException(404, "Eintrag nicht gefunden")
    return rows[0]
