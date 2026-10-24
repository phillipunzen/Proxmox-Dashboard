"""Hardware and guest configuration inventory."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.inventory import export
from app.inventory.collector import InventoryCollector, InventoryStore
from app.inventory.models import ClusterInventory, GuestInventory, NodeHardware

router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])


def _store(request: Request) -> InventoryStore:
    return request.app.state.inventory


def _cluster(request: Request, cluster: str) -> ClusterInventory:
    inv = _store(request).get(cluster)
    if inv is None:
        raise HTTPException(404, f"No inventory for cluster '{cluster}' (yet)")
    return inv


@router.get("", response_model=list[ClusterInventory], summary="Complete inventory")
async def all_inventory(request: Request) -> list[ClusterInventory]:
    return _store(request).all()


@router.get("/clusters/{cluster}", response_model=ClusterInventory)
async def cluster_inventory(request: Request, cluster: str) -> ClusterInventory:
    return _cluster(request, cluster)


@router.get("/clusters/{cluster}/nodes/{node}", response_model=NodeHardware)
async def node_inventory(request: Request, cluster: str, node: str) -> NodeHardware:
    for n in _cluster(request, cluster).nodes:
        if n.node == node:
            return n
    raise HTTPException(404, f"Unknown node '{node}'")


@router.get("/clusters/{cluster}/guests/{vmid}", response_model=GuestInventory)
async def guest_inventory(request: Request, cluster: str, vmid: int) -> GuestInventory:
    for g in _cluster(request, cluster).guests:
        if g.vmid == vmid:
            return g
    raise HTTPException(404, f"Unknown guest {vmid}")


@router.get("/clusters/{cluster}/nodes/{node}/smart", summary="SMART details of one disk (live)")
async def smart(request: Request, cluster: str, node: str,
                disk: Annotated[str, Query(pattern=r"^/dev/[\w/-]+$")]) -> dict[str, Any]:
    hw = await node_inventory(request, cluster, node)
    # Only disks we know from the inventory - never pass arbitrary paths through.
    if disk not in {d.devpath for d in hw.disks}:
        raise HTTPException(404, f"Unknown disk '{disk}' on {node}")
    client = request.app.state.clients[cluster]
    try:
        return await client.get(f"/nodes/{node}/disks/smart", {"disk": disk})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"SMART query failed: {exc}") from exc


class RefreshResult(BaseModel):
    started: bool
    detail: str


@router.post("/refresh", response_model=RefreshResult, status_code=202,
             summary="Collect the inventory now (at most every 30 s)")
async def refresh(request: Request) -> RefreshResult:
    collector: InventoryCollector = request.app.state.inventory_collector
    started = await collector.refresh(force=True)
    return RefreshResult(started=started,
                         detail="Inventar aktualisiert" if started else "Läuft bereits oder zu "
                         "kurz nach der letzten Aktualisierung")


@router.get("/export", summary="Export as CSV (semicolon, Excel-friendly) or JSON",
            responses={200: {"content": {"text/csv": {}, "application/json": {}}}})
async def export_inventory(
    request: Request, kind: export.ExportKind = "guests",
    format: Literal["csv", "json"] = "csv",  # noqa: A002
    cluster: str | None = None,
) -> Response:
    invs = [_cluster(request, cluster)] if cluster else _store(request).all()
    data = export.rows(kind, invs)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    name = re.sub(r"[^\w.-]", "_", f"pve-{cluster or 'all'}-{kind}-{stamp}")
    if format == "json":
        return Response(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                        media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
    return Response(export.to_csv(data), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
