"""Backups, snapshots and storage forecast.

* Backup jobs (``/cluster/backup``) and which guests they cover (all / VMID list / pool,
  minus excludes, optionally restricted to one node).
* Backup volumes on every backup-capable storage → newest backup per guest, verification.
* Snapshots per guest → count and age of the oldest one (old snapshots eat space).
* Storage forecast from the stored history: linear trend over 14 days → "full in ~N days".
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.collector.models import ClusterSnapshot, GuestView
from app.collector.state import SnapshotStore
from app.pve import PveApi, PveError, PveUnavailable
from app.tsdb.client import TsdbClient, TsdbError

log = logging.getLogger(__name__)

CONCURRENCY = 6
FORECAST_WINDOW = 14 * 86400
FORECAST_STEP = 6 * 3600


class BackupJob(BaseModel):
    cluster: str
    id: str
    enabled: bool
    schedule: str | None
    storage: str | None
    selection: str  # human readable
    node: str | None
    mode: str | None
    comment: str | None
    vmids: list[int]  # resolved guests


class BackupVolume(BaseModel):
    volid: str
    storage: str
    ctime: datetime
    size: int | None
    verified: str | None  # ok | failed | None (not verified / not PBS)
    protected: bool = False
    notes: str | None = None


class GuestBackups(BaseModel):
    cluster: str
    vmid: int
    name: str
    type: str
    node: str
    status: str
    last_backup: BackupVolume | None
    backup_count: int
    age_hours: float | None
    covered_by: list[str]  # job ids
    snapshots: int
    oldest_snapshot_days: float | None
    state: str  # ok | old | missing | not_covered | failed_verify


class StorageForecast(BaseModel):
    cluster: str
    node: str  # "shared" for shared storages
    storage: str
    type: str
    used: int
    total: int
    growth_per_day: float | None  # bytes/day (negative = shrinking)
    days_until_full: float | None
    days_until_90: float | None
    samples: int


def linear_trend(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (value per second). None with fewer than 3 points."""
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    var = sum((t - mx) ** 2 for t, _ in points)
    if var == 0:
        return None
    return sum((t - mx) * (v - my) for t, v in points) / var


def forecast(used: float, total: float, slope_per_s: float | None) -> tuple[float | None,
                                                                            float | None]:
    """Days until 100 % and until 90 % (None if not growing)."""
    if slope_per_s is None or slope_per_s <= 0 or total <= 0:
        return None, None
    per_day = slope_per_s * 86400
    full = max(0.0, (total - used) / per_day)
    ninety = max(0.0, (total * 0.9 - used) / per_day)
    return full, ninety


def resolve_job(job: dict[str, Any], snap: ClusterSnapshot) -> tuple[list[int], str]:
    """Which guests does a vzdump job include? Returns (vmids, description)."""
    guests = [g for g in snap.guests if not g.template]
    node = job.get("node")
    if node:
        guests = [g for g in guests if g.node == node]
    excluded = {int(x) for x in str(job.get("exclude") or "").split(",") if x.strip().isdigit()}
    if str(job.get("all", "0")) == "1":
        desc = "alle Gäste" + (f" ohne {', '.join(sorted(map(str, excluded)))}" if excluded else "")
        vmids = [g.vmid for g in guests if g.vmid not in excluded]
    elif job.get("pool"):
        desc = f"Pool {job['pool']}"
        vmids = [g.vmid for g in guests if g.pool == job["pool"] and g.vmid not in excluded]
    else:
        listed = {int(x) for x in str(job.get("vmid") or "").split(",") if x.strip().isdigit()}
        desc = f"VMIDs {', '.join(map(str, sorted(listed)))}"
        vmids = [g.vmid for g in guests if g.vmid in listed]
    if node:
        desc += f" auf {node}"
    return sorted(vmids), desc


def classify(g: GuestView, last: BackupVolume | None, covered: list[str], max_age_h: float,
             now: float) -> tuple[str, float | None]:
    age = (now - last.ctime.timestamp()) / 3600 if last else None
    if last and last.verified == "failed":
        return "failed_verify", age
    if age is not None and age <= max_age_h:
        return "ok", age
    if not covered:
        return "not_covered", age
    return ("old" if last else "missing"), age


class BackupService:
    def __init__(self, store: SnapshotStore, clients: dict[str, PveApi],
                 tsdb: TsdbClient | None, interval: float = 900, max_age_hours: float = 48):
        self.store = store
        self.clients = clients
        self.tsdb = tsdb
        self.interval = interval
        self.max_age_hours = max_age_hours
        self.jobs: dict[str, list[BackupJob]] = {}
        self.guests: dict[str, list[GuestBackups]] = {}
        self.forecasts: list[StorageForecast] = []
        self.errors: dict[str, str] = {}
        self.updated_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="backups")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)

    async def refresh(self) -> None:
        async with self._lock:
            for snap in self.store.all():
                if snap.reachable and snap.name in self.clients:
                    try:
                        await self._refresh_cluster(snap)
                        self.errors.pop(snap.name, None)
                    except (PveError, PveUnavailable) as exc:
                        self.errors[snap.name] = str(exc)
                        log.warning("Backup overview for %s failed: %s", snap.name, exc)
            self.forecasts = await self._forecast()
            self.updated_at = datetime.now(UTC)

    async def _refresh_cluster(self, snap: ClusterSnapshot) -> None:
        client = self.clients[snap.name]
        sem = asyncio.Semaphore(CONCURRENCY)

        async def get(path: str, params: dict[str, Any] | None = None) -> Any:
            async with sem:
                return await client.get(path, params)

        raw_jobs = await get("/cluster/backup")
        jobs = []
        for j in raw_jobs or []:
            vmids, desc = resolve_job(j, snap)
            jobs.append(BackupJob(cluster=snap.name, id=str(j.get("id")),
                                  enabled=str(j.get("enabled", "1")) == "1",
                                  schedule=j.get("schedule") or j.get("starttime"),
                                  storage=j.get("storage"), selection=desc, node=j.get("node"),
                                  mode=j.get("mode"), comment=j.get("comment"), vmids=vmids))
        self.jobs[snap.name] = jobs

        # Every backup storage once (shared: on any online node).
        targets: dict[str, str] = {}
        online = {n.node for n in snap.nodes if n.status == "online"}
        for s in snap.storages:
            if "backup" not in s.content or s.status != "available" or s.node not in online:
                continue
            key = s.storage if s.shared else f"{s.node}/{s.storage}"
            targets.setdefault(key, s.node)

        async def volumes(key: str, node: str) -> list[BackupVolume]:
            storage = key.split("/")[-1]
            try:
                rows = await get(f"/nodes/{node}/storage/{storage}/content", {"content": "backup"})
            except (PveError, PveUnavailable) as exc:
                log.info("Backup list %s on %s failed: %s", storage, node, exc)
                return []
            out = []
            for r in rows or []:
                if r.get("vmid") is None:
                    continue
                out.append((int(r["vmid"]), BackupVolume(
                    volid=r["volid"], storage=storage,
                    ctime=datetime.fromtimestamp(int(r.get("ctime", 0)), UTC),
                    size=r.get("size"), protected=bool(r.get("protected")),
                    verified=(r.get("verification") or {}).get("state"), notes=r.get("notes"))))
            return out  # type: ignore[return-value]

        results = await asyncio.gather(*(volumes(k, n) for k, n in targets.items()))
        by_guest: dict[int, list[BackupVolume]] = {}
        for vols in results:
            for vmid, vol in vols:  # type: ignore[misc]
                by_guest.setdefault(vmid, []).append(vol)

        async def snaps(g: GuestView) -> tuple[int, list[dict[str, Any]]]:
            try:
                return g.vmid, await get(f"/nodes/{g.node}/{g.type}/{g.vmid}/snapshot")
            except (PveError, PveUnavailable):
                return g.vmid, []

        guests = [g for g in snap.guests if not g.template]
        snap_lists = dict(await asyncio.gather(*(snaps(g) for g in guests)))
        coverage: dict[int, list[str]] = {}
        for j in jobs:
            if j.enabled:
                for vmid in j.vmids:
                    coverage.setdefault(vmid, []).append(j.id)

        now = time.time()
        rows: list[GuestBackups] = []
        for g in guests:
            vols = sorted(by_guest.get(g.vmid, []), key=lambda v: v.ctime, reverse=True)
            last = vols[0] if vols else None
            real_snaps = [s for s in snap_lists.get(g.vmid, []) if s.get("name") != "current"]
            oldest = min((s.get("snaptime", now) for s in real_snaps), default=None)
            state, age = classify(g, last, coverage.get(g.vmid, []), self.max_age_hours, now)
            rows.append(GuestBackups(
                cluster=snap.name, vmid=g.vmid, name=g.name, type=g.type, node=g.node,
                status=g.status, last_backup=last, backup_count=len(vols), age_hours=age,
                covered_by=coverage.get(g.vmid, []), snapshots=len(real_snaps),
                oldest_snapshot_days=(now - oldest) / 86400 if oldest else None, state=state))
        self.guests[snap.name] = sorted(rows, key=lambda r: r.vmid)

    async def forecast_now(self) -> list[StorageForecast]:
        return await self._forecast()

    async def _forecast(self) -> list[StorageForecast]:
        if self.tsdb is None:
            return []
        end = time.time()
        try:
            used = await self.tsdb.query_range("pve_storage_used_bytes",
                                               end - FORECAST_WINDOW, end, FORECAST_STEP)
            total = await self.tsdb.query("last_over_time(pve_storage_total_bytes[1h])")
        except TsdbError as exc:
            log.info("Storage forecast unavailable: %s", exc)
            return []

        def key(lbl: dict[str, str]) -> tuple[str, str, str]:
            return lbl.get("cluster", ""), lbl.get("node", ""), lbl.get("storage", "")

        totals = {key(s.labels): s.points[0][1] for s in total}
        out = []
        for s in used:
            k = key(s.labels)
            cap = totals.get(k)
            if not s.points or not cap:
                continue
            slope = linear_trend(s.points)
            now_used = s.points[-1][1]
            full, ninety = forecast(now_used, cap, slope)
            out.append(StorageForecast(
                cluster=k[0], node=k[1], storage=k[2], type=s.labels.get("type", ""),
                used=int(now_used), total=int(cap),
                growth_per_day=slope * 86400 if slope is not None else None,
                days_until_full=full, days_until_90=ninety, samples=len(s.points)))
        return sorted(out, key=lambda f: (f.days_until_full is None, f.days_until_full or 0))
