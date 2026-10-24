"""Long-running dashboard jobs (node upgrade, maintenance, reboot) with a live log.

Each job runs as an asyncio task. Log lines go to memory (for live streaming) and to
``<data_dir>/jobs/<id>.log`` (so they survive a restart). Only one exclusive job may run per
node at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update

from app import audit
from app.db.engine import Database
from app.db.models import Job, utcnow

log = logging.getLogger(__name__)


class JobView(BaseModel):
    id: int
    kind: str
    cluster: str
    node: str | None
    status: str
    started_by: str
    started_at: datetime
    finished_at: datetime | None
    summary: str | None
    params: dict[str, Any]

    model_config = {"from_attributes": True}


class JobFailed(Exception):
    """Raise inside a job to finish it with status ``error`` and this message."""


class JobContext:
    def __init__(self, manager: JobManager, job_id: int):
        self.manager = manager
        self.job_id = job_id
        self.summary: str | None = None

    def log(self, line: str) -> None:
        self.manager._append(self.job_id, line)


JobFn = Callable[[JobContext], Awaitable[str | None]]


class JobManager:
    def __init__(self, db: Database, data_dir: str):
        self.db = db
        self.dir = Path(data_dir) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lines: dict[int, list[str]] = {}
        self._changed: dict[int, asyncio.Event] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._busy: dict[tuple[str, str], int] = {}

    async def recover(self) -> None:
        """Jobs that were running when the dashboard stopped can't be resumed."""
        async with self.db.session() as s:
            await s.execute(update(Job).where(Job.status == "running").values(
                status="error", finished_at=utcnow(),
                summary="Abgebrochen: Dashboard wurde neu gestartet"))
            await s.commit()

    def running_for(self, cluster: str, node: str) -> int | None:
        return self._busy.get((cluster, node))

    async def start(self, kind: str, cluster: str, node: str | None, actor: audit.Actor,
                    fn: JobFn, params: dict[str, Any] | None = None,
                    exclusive: bool = True) -> Job:
        key = (cluster, node or "")
        if exclusive and node and key in self._busy:
            raise RuntimeError(f"Auf {node} läuft bereits Job #{self._busy[key]}")
        audit_id = await audit.record(self.db, actor, f"node.{kind}", "running", cluster=cluster,
                                      node=node, params=params or {})
        job = Job(kind=kind, cluster=cluster, node=node, status="running", started_by=actor.name,
                  params={**(params or {}), "audit_id": audit_id})
        async with self.db.session() as s:
            s.add(job)
            await s.commit()
        self._lines[job.id] = []
        self._changed[job.id] = asyncio.Event()
        if exclusive and node:
            self._busy[key] = job.id
        self._tasks[job.id] = asyncio.create_task(self._run(job.id, key, fn, audit_id),
                                                  name=f"job-{job.id}")
        return job

    async def _run(self, job_id: int, key: tuple[str, str], fn: JobFn, audit_id: int) -> None:
        ctx = JobContext(self, job_id)
        status, summary = "ok", None
        try:
            summary = await fn(ctx)
        except JobFailed as exc:
            status, summary = "error", str(exc)
        except asyncio.CancelledError:
            status, summary = "cancelled", "Abgebrochen"
        except Exception as exc:  # noqa: BLE001 - must always finish the job record
            log.exception("Job %s failed", job_id)
            status, summary = "error", f"Interner Fehler: {exc}"
        self._append(job_id, f"== {status.upper()}{': ' + summary if summary else ''}")
        async with self.db.session() as s:
            job = await s.get(Job, job_id)
            if job:
                job.status, job.summary, job.finished_at = status, summary, utcnow()
                await s.commit()
        await audit.finish(self.db, audit_id, "ok" if status == "ok" else "error", summary)
        if self._busy.get(key) == job_id:
            del self._busy[key]
        self._tasks.pop(job_id, None)
        self._signal(job_id)

    def _append(self, job_id: int, line: str) -> None:
        stamp = utcnow().strftime("%H:%M:%S")
        text = f"[{stamp}] {line.rstrip()}"
        self._lines.setdefault(job_id, []).append(text)
        with (self.dir / f"{job_id}.log").open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        self._signal(job_id)

    def _signal(self, job_id: int) -> None:
        ev = self._changed.get(job_id)
        if ev:
            ev.set()
            self._changed[job_id] = asyncio.Event()

    def lines(self, job_id: int, start: int = 0) -> list[str]:
        if job_id in self._lines:
            return self._lines[job_id][start:]
        path = self.dir / f"{job_id}.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()[start:]

    def is_running(self, job_id: int) -> bool:
        return job_id in self._tasks

    async def wait(self, job_id: int, timeout: float) -> None:
        ev = self._changed.get(job_id)
        if ev is None:
            await asyncio.sleep(min(timeout, 1))
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout)

    async def cancel(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def get(self, job_id: int) -> JobView | None:
        async with self.db.session() as s:
            job = await s.get(Job, job_id)
            return JobView.model_validate(job) if job else None

    async def list(self, cluster: str | None = None, node: str | None = None,
                   kind: str | None = None, limit: int = 50) -> list[JobView]:
        stmt = select(Job).order_by(Job.id.desc()).limit(limit)
        if cluster:
            stmt = stmt.where(Job.cluster == cluster)
        if node:
            stmt = stmt.where(Job.node == node)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        async with self.db.session() as s:
            return [JobView.model_validate(j) for j in await s.scalars(stmt)]

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
