"""Node management, jobs and SSH setup."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import audit
from app.auth.deps import Principal, actor, admin, operator, viewer
from app.jobs import JobManager, JobView
from app.nodes.service import NodeError, NodeService, NodeStatusView, UpdateInfo

router = APIRouter(prefix="/api/v1", tags=["nodes"])

N = "/clusters/{cluster}/nodes/{node}"


def _svc(request: Request) -> NodeService:
    return request.app.state.nodes


def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs


async def _run(coro: Any) -> Any:
    try:
        return await coro
    except NodeError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    except RuntimeError as exc:  # job already running on this node
        raise HTTPException(409, str(exc)) from exc


class JobStarted(BaseModel):
    job_id: int


class Confirm(BaseModel):
    confirm: str = Field(description="Node name")


class MaintenanceRequest(BaseModel):
    enable: bool
    confirm: str | None = Field(None, description="Node name (required to enable)")
    shutdown_unmigratable: bool = Field(
        False, description="Shut down guests that cannot be migrated (e.g. PCI passthrough)")
    return_guests: bool = Field(True, description="On disable: migrate evacuated guests back")


class RebootRequest(Confirm):
    force: bool = Field(False, description="Reboot even though guests are still running")


# --- status & updates -----------------------------------------------------------------------


@router.get("/node-status", response_model=list[NodeStatusView], summary="Maintenance, "
            "updates, reboot-required, temperature and SSH state per node")
async def node_status(request: Request, cluster: str | None = None,
                      _: Principal = viewer) -> list[NodeStatusView]:
    return await _svc(request).statuses(cluster)


@router.get(N + "/updates", response_model=UpdateInfo)
async def updates(request: Request, cluster: str, node: str,
                  _: Principal = viewer) -> UpdateInfo:
    svc = _svc(request)
    info = svc.state.updates.get((cluster, node))
    return info or await _run(svc.check_updates(cluster, node))


@router.post(N + "/updates/refresh", response_model=JobStarted,
             summary="apt-get update via the API (NodeAdmin token)")
async def refresh_updates(request: Request, cluster: str, node: str,
                          p: Principal = admin) -> JobStarted:
    return JobStarted(job_id=await _run(_svc(request).refresh_package_lists(
        actor(request, p), cluster, node)))


@router.get(N + "/updates/changelog", summary="Changelog of a package (text)")
async def changelog(request: Request, cluster: str, node: str,
                    package: Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9+.-]{0,99}$")],
                    version: Annotated[str | None, Query(pattern=r"^[\w.:~+-]{1,100}$")] = None,
                    _: Principal = viewer) -> dict[str, str]:
    return {"package": package,
            "changelog": await _run(_svc(request).changelog(cluster, node, package, version))}


@router.post(N + "/upgrade", response_model=JobStarted,
             summary="apt-get dist-upgrade via SSH, live output in the job log")
async def upgrade(request: Request, cluster: str, node: str, body: Confirm,
                  p: Principal = admin) -> JobStarted:
    return JobStarted(job_id=await _run(_svc(request).upgrade(actor(request, p), cluster, node,
                                                              body.confirm)))


# --- maintenance & reboot -------------------------------------------------------------------


@router.get(N + "/maintenance/plan", summary="What entering maintenance would do per guest")
async def maintenance_plan(request: Request, cluster: str, node: str,
                           _: Principal = viewer) -> list[dict[str, Any]]:
    return await _run(_svc(request).maintenance_plan(cluster, node))


@router.post(N + "/maintenance", response_model=JobStarted)
async def maintenance(request: Request, cluster: str, node: str, body: MaintenanceRequest,
                      p: Principal = admin) -> JobStarted:
    svc = _svc(request)
    if body.enable:
        job = await _run(svc.enter_maintenance(actor(request, p), cluster, node,
                                               body.confirm or "", body.shutdown_unmigratable))
    else:
        job = await _run(svc.exit_maintenance(actor(request, p), cluster, node,
                                              body.return_guests))
    return JobStarted(job_id=job)


@router.get(N + "/reboot/precheck")
async def reboot_precheck(request: Request, cluster: str, node: str,
                          _: Principal = viewer) -> dict[str, Any]:
    return await _run(_svc(request).reboot_precheck(cluster, node))


@router.post(N + "/reboot", response_model=JobStarted)
async def reboot(request: Request, cluster: str, node: str, body: RebootRequest,
                 p: Principal = admin) -> JobStarted:
    return JobStarted(job_id=await _run(_svc(request).reboot(actor(request, p), cluster, node,
                                                             body.confirm, body.force)))


# --- jobs -----------------------------------------------------------------------------------


@router.get("/jobs", response_model=list[JobView], tags=["jobs"])
async def jobs(request: Request, cluster: str | None = None, node: str | None = None,
               kind: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50,
               _: Principal = viewer) -> list[JobView]:
    return await _jobs(request).list(cluster, node, kind, limit)


@router.get("/jobs/{job_id}", response_model=JobView, tags=["jobs"])
async def job(request: Request, job_id: int, _: Principal = viewer) -> JobView:
    j = await _jobs(request).get(job_id)
    if j is None:
        raise HTTPException(404, "Job nicht gefunden")
    return j


@router.get("/jobs/{job_id}/log", tags=["jobs"])
async def job_log(request: Request, job_id: int, start: Annotated[int, Query(ge=0)] = 0,
                  _: Principal = viewer) -> dict[str, Any]:
    m = _jobs(request)
    return {"lines": m.lines(job_id, start), "running": m.is_running(job_id)}


@router.get("/jobs/{job_id}/stream", tags=["jobs"],
            summary="SSE: job log lines live (event 'line', final event 'done')")
async def job_stream(request: Request, job_id: int, _: Principal = viewer) -> StreamingResponse:
    m = _jobs(request)
    if await m.get(job_id) is None:
        raise HTTPException(404, "Job nicht gefunden")

    async def events() -> AsyncIterator[str]:
        sent = 0
        while True:
            lines = m.lines(job_id, sent)
            for line in lines:
                yield f"event: line\ndata: {json.dumps(line)}\n\n"
            sent += len(lines)
            if not m.is_running(job_id):
                j = await m.get(job_id)
                yield f"event: done\ndata: {j.model_dump_json() if j else '{}'}\n\n"
                return
            if await request.is_disconnected():
                return
            await m.wait(job_id, 15)
            if not m.lines(job_id, sent):
                yield ": keepalive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/jobs/{job_id}/cancel", tags=["jobs"])
async def cancel_job(request: Request, job_id: int, p: Principal = admin) -> dict[str, bool]:
    ok = await _jobs(request).cancel(job_id)
    if ok:
        await audit.record(request.app.state.db, actor(request, p), "job.cancel", "ok",
                           target=f"job #{job_id}")
    return {"cancelled": ok}


# --- SSH setup (admin) ----------------------------------------------------------------------


class SshNode(BaseModel):
    cluster: str
    node: str
    host: str | None
    trusted: bool
    host_key: str | None


class SshOverview(BaseModel):
    available: bool
    user: str
    port: int
    public_key: str | None
    nodes: list[SshNode]


class TrustRequest(BaseModel):
    cluster: str
    node: str
    key: str | None = Field(None, description="OpenSSH public host key; null = forget")


class NodeRef(BaseModel):
    cluster: str
    node: str


@router.get("/admin/ssh", response_model=SshOverview, tags=["admin"])
async def ssh_overview(request: Request, _: Principal = admin) -> SshOverview:
    svc = _svc(request)
    nodes = []
    for st in await svc.statuses():
        state = await svc.get_state(st.cluster, st.node)
        nodes.append(SshNode(cluster=st.cluster, node=st.node, host=st.ssh_host,
                             trusted=st.ssh_trusted, host_key=state.ssh_host_key))
    s = request.app.state.settings
    return SshOverview(available=svc.ssh is not None, user=s.ssh_user, port=s.ssh_port,
                       public_key=svc.ssh.public_key if svc.ssh else None, nodes=nodes)


@router.post("/admin/ssh/scan", tags=["admin"], summary="Fetch a node's SSH host key")
async def ssh_scan(request: Request, body: NodeRef, _: Principal = admin) -> dict[str, str]:
    return await _run(_svc(request).scan_host_key(body.cluster, body.node))


@router.post("/admin/ssh/trust", tags=["admin"], summary="Trust (or forget) a host key")
async def ssh_trust(request: Request, body: TrustRequest, p: Principal = admin) -> dict[str, bool]:
    if body.key is not None and not body.key.startswith(("ssh-", "ecdsa-")):
        raise HTTPException(422, "Ungültiger Host-Key")
    await _run(_svc(request).trust_host_key(body.cluster, body.node, body.key))
    await audit.record(request.app.state.db, actor(request, p),
                       "ssh.trust" if body.key else "ssh.forget", "ok", cluster=body.cluster,
                       node=body.node, params={"key": (body.key or "")[:80]})
    return {"trusted": body.key is not None}


@router.post("/admin/ssh/test", tags=["admin"], summary="Test the SSH setup on a node")
async def ssh_test(request: Request, body: NodeRef, _: Principal = admin) -> dict[str, Any]:
    svc = _svc(request)
    try:
        out = await asyncio.wait_for(svc._ssh(body.cluster, body.node, "version", timeout=20), 30)
    except NodeError as exc:
        return {"ok": False, "message": exc.message}
    except TimeoutError:
        return {"ok": False, "message": "Zeitüberschreitung"}
    mainboard = await svc.mainboard(body.cluster, body.node)
    return {"ok": True, "message": out.strip(), "mainboard": mainboard}


# --- guest updates (optional) ---------------------------------------------------------------


@router.get("/clusters/{cluster}/guests/{vmid}/os-updates", tags=["guest-updates"],
            summary="Count pending apt updates inside a guest (GUEST_UPDATES=true)")
async def guest_updates_check(request: Request, cluster: str, vmid: int,
                              _: Principal = operator) -> dict[str, Any]:
    return await _run(request.app.state.guest_updates.check(cluster, vmid))


@router.post("/clusters/{cluster}/guests/{vmid}/os-upgrade", response_model=JobStarted,
             tags=["guest-updates"], summary="apt dist-upgrade inside a guest (job)")
async def guest_upgrade(request: Request, cluster: str, vmid: int, body: Confirm,
                        p: Principal = operator) -> JobStarted:
    return JobStarted(job_id=await _run(request.app.state.guest_updates.upgrade(
        actor(request, p), cluster, vmid, body.confirm)))
