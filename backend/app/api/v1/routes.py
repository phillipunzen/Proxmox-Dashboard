"""Read-only REST API v1 (phase 1): clusters, nodes, guests, storage, live stream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import __version__
from app.auth.deps import COOKIE, api_key_from
from app.collector.models import (
    ClusterSnapshot,
    ClusterWarning,
    GuestView,
    NodeView,
    StorageView,
)
from app.collector.state import SnapshotStore

router = APIRouter(prefix="/api/v1")
# Unauthenticated: only the container health check.
public_router = APIRouter(prefix="/api/v1")

SSE_HEARTBEAT = 15.0


def _store(request: Request) -> SnapshotStore:
    return request.app.state.store


def _cluster(store: SnapshotStore, name: str) -> ClusterSnapshot:
    snap = store.get(name)
    if snap is None:
        raise HTTPException(404, f"Unknown cluster '{name}'")
    return snap


def _selected(store: SnapshotStore, cluster: str | None) -> list[ClusterSnapshot]:
    return [_cluster(store, cluster)] if cluster else store.all()


# --- meta -----------------------------------------------------------------------------------


class Meta(BaseModel):
    version: str
    mock: bool
    read_only: bool
    dry_run: bool
    poll_interval: float
    history: bool
    clusters: list[str]


class Health(BaseModel):
    status: Literal["ok", "degraded"]


@router.get("/meta", response_model=Meta, tags=["meta"])
async def meta(request: Request) -> Meta:
    s = request.app.state.settings
    return Meta(version=__version__, mock=s.pve_mock, read_only=s.read_only, dry_run=s.dry_run,
                poll_interval=s.poll_interval, history=request.app.state.tsdb is not None,
                clusters=[c.name for c in _store(request).all()])


@public_router.get("/health", response_model=Health, tags=["meta"])
async def health(request: Request) -> Health:
    """Liveness/readiness without details: ``degraded`` if any cluster is unreachable."""
    ok = all(c.reachable for c in _store(request).all())
    return Health(status="ok" if ok else "degraded")


# --- clusters -------------------------------------------------------------------------------


@router.get(
    "/clusters",
    response_model=list[ClusterSnapshot],
    response_model_exclude={"__all__": {"guests", "storages"}},
    tags=["cluster"],
    summary="All clusters with nodes, totals and warnings (without guest/storage lists)",
)
async def list_clusters(request: Request) -> list[ClusterSnapshot]:
    return _store(request).all()


@router.get("/clusters/{cluster}", response_model=ClusterSnapshot, tags=["cluster"],
            summary="Full snapshot of one cluster")
async def get_cluster(request: Request, cluster: str) -> ClusterSnapshot:
    return _cluster(_store(request), cluster)


@router.get("/warnings", response_model=list[ClusterWarning], tags=["cluster"])
async def list_warnings(request: Request, cluster: str | None = None) -> list[ClusterWarning]:
    out: list[ClusterWarning] = []
    for snap in _selected(_store(request), cluster):
        if not snap.reachable:
            out.append(ClusterWarning(severity="critical", target=f"cluster/{snap.name}",
                                      message=f"Cluster {snap.name} nicht erreichbar"))
        out.extend(w.model_copy(update={"target": f"{snap.name}:{w.target}"})
                   for w in snap.warnings)
    return out


# --- nodes ----------------------------------------------------------------------------------


@router.get("/nodes", response_model=list[NodeView], tags=["nodes"])
async def list_nodes(request: Request, cluster: str | None = None) -> list[NodeView]:
    return [n for snap in _selected(_store(request), cluster) for n in snap.nodes]


@router.get("/clusters/{cluster}/nodes/{node}", response_model=NodeView, tags=["nodes"])
async def get_node(request: Request, cluster: str, node: str) -> NodeView:
    snap = _cluster(_store(request), cluster)
    for n in snap.nodes:
        if n.node == node:
            return n
    raise HTTPException(404, f"Unknown node '{node}' in cluster '{cluster}'")


# --- guests ---------------------------------------------------------------------------------

GuestSort = Literal["vmid", "name", "node", "cpu", "cpu_host", "mem", "maxmem",
                    "diskread_bps", "diskwrite_bps", "netin_bps", "netout_bps", "uptime"]


@router.get("/guests", response_model=list[GuestView], tags=["guests"])
async def list_guests(
    request: Request,
    cluster: str | None = None,
    node: str | None = None,
    type: Annotated[Literal["qemu", "lxc"] | None, Query()] = None,  # noqa: A002
    status: str | None = None,
    tag: Annotated[list[str] | None, Query(description="All given tags must match")] = None,
    pool: str | None = None,
    q: Annotated[str | None, Query(description="Substring of name or VMID")] = None,
    include_templates: bool = False,
    sort: GuestSort = "vmid",
    order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int | None, Query(ge=1, le=10000)] = None,
) -> list[GuestView]:
    guests = [g for snap in _selected(_store(request), cluster) for g in snap.guests]
    if not include_templates:
        guests = [g for g in guests if not g.template]
    if node:
        guests = [g for g in guests if g.node == node]
    if type:
        guests = [g for g in guests if g.type == type]
    if status:
        guests = [g for g in guests if g.status == status]
    if tag:
        wanted = set(tag)
        guests = [g for g in guests if wanted.issubset(g.tags)]
    if pool:
        guests = [g for g in guests if g.pool == pool]
    if q:
        needle = q.lower()
        guests = [g for g in guests if needle in g.name.lower() or needle in str(g.vmid)]

    def key(g: GuestView) -> tuple[bool, object]:
        v = getattr(g, sort)
        # None values (no rate yet) always sort last.
        return (v is None) != (order == "desc"), v if v is not None else 0

    guests.sort(key=key, reverse=order == "desc")
    return guests[:limit] if limit else guests


@router.get("/clusters/{cluster}/guests/{vmid}", response_model=GuestView, tags=["guests"])
async def get_guest(request: Request, cluster: str, vmid: int) -> GuestView:
    snap = _cluster(_store(request), cluster)
    for g in snap.guests:
        if g.vmid == vmid:
            return g
    raise HTTPException(404, f"Unknown guest {vmid} in cluster '{cluster}'")


# --- storage --------------------------------------------------------------------------------


@router.get("/storage", response_model=list[StorageView], tags=["storage"])
async def list_storage(request: Request, cluster: str | None = None,
                       node: str | None = None) -> list[StorageView]:
    out = [s for snap in _selected(_store(request), cluster) for s in snap.storages]
    return [s for s in out if s.node == node] if node else out


# --- live stream ----------------------------------------------------------------------------


@router.get(
    "/stream",
    tags=["live"],
    summary="Server-Sent Events: full snapshot of all clusters after every poll",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream(request: Request) -> StreamingResponse:
    store = _store(request)

    auth = request.app.state.auth
    token = request.cookies.get(COOKIE)
    key = api_key_from(request)
    keys = request.app.state.api_keys

    async def still_valid() -> bool:
        if key:
            return await keys.verify(key, None) is not None
        info = await auth.resolve(token)
        return info is not None and not info.pending

    async def events() -> AsyncIterator[str]:
        version = -1
        while True:
            if await request.is_disconnected():
                break
            changed = await store.wait_for_change(version, SSE_HEARTBEAT)
            if not changed:
                # Long-lived stream: end it when the session expired or was revoked.
                if not await still_valid():
                    yield "event: logout\ndata: {}\n\n"
                    break
                yield ": keepalive\n\n"
                continue
            version = store.version
            payload = {
                "version": version,
                "clusters": [s.model_dump(mode="json") for s in store.all()],
            }
            yield f"event: snapshot\nid: {version}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
