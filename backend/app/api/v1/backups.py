"""Backups, snapshots and storage forecast."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.auth.deps import Principal, viewer
from app.backups.service import BackupJob, BackupService, GuestBackups, StorageForecast

router = APIRouter(prefix="/api/v1", tags=["backups"])


def _svc(request: Request) -> BackupService:
    return request.app.state.backups


class BackupOverview(BaseModel):
    updated_at: datetime | None
    max_age_hours: float
    errors: dict[str, str]
    counts: dict[str, int]
    guests: list[GuestBackups]


@router.get("/backups", response_model=BackupOverview,
            summary="Last backup, coverage by jobs and snapshots per guest")
async def backups(request: Request, cluster: str | None = None,
                  _: Principal = viewer) -> BackupOverview:
    svc = _svc(request)
    rows = [g for c, gs in svc.guests.items() if cluster in (None, c) for g in gs]
    counts: dict[str, int] = {}
    for g in rows:
        counts[g.state] = counts.get(g.state, 0) + 1
    return BackupOverview(updated_at=svc.updated_at, max_age_hours=svc.max_age_hours,
                          errors=svc.errors, counts=counts, guests=rows)


@router.get("/backups/jobs", response_model=list[BackupJob])
async def backup_jobs(request: Request, cluster: str | None = None,
                      _: Principal = viewer) -> list[BackupJob]:
    return [j for c, js in _svc(request).jobs.items() if cluster in (None, c) for j in js]


@router.post("/backups/refresh", summary="Re-read backups and snapshots now")
async def refresh(request: Request, _: Principal = viewer) -> dict[str, bool]:
    await _svc(request).refresh()
    return {"ok": True}


@router.get("/storage/forecast", response_model=list[StorageForecast],
            summary="Linear trend of the last 14 days: growth per day, days until full")
async def storage_forecast(request: Request, cluster: str | None = None,
                           _: Principal = viewer) -> list[StorageForecast]:
    svc = _svc(request)
    if not svc.forecasts:
        # Right after startup the history may not have existed yet - compute on demand.
        svc.forecasts = await svc.forecast_now()
    return [f for f in svc.forecasts if cluster in (None, f.cluster)]
