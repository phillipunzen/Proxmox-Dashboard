"""Audit log: who did what, when, with which result."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app.db.engine import Database
from app.db.models import AuditEntry, utcnow

log = logging.getLogger("audit")


class Actor(BaseModel):
    name: str
    role: str | None = None
    ip: str | None = None


class AuditView(BaseModel):
    id: int
    ts: datetime
    actor: str
    actor_role: str | None
    ip: str | None
    action: str
    cluster: str | None
    node: str | None
    vmid: int | None
    target: str | None
    params: dict[str, Any]
    dry_run: bool
    status: str
    upid: str | None
    message: str | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


async def record(db: Database, actor: Actor, action: str, status: str, *,
                 cluster: str | None = None, node: str | None = None, vmid: int | None = None,
                 target: str | None = None, params: dict[str, Any] | None = None,
                 dry_run: bool = False, upid: str | None = None,
                 message: str | None = None) -> int:
    entry = AuditEntry(actor=actor.name, actor_role=actor.role, ip=actor.ip, action=action,
                       status=status, cluster=cluster, node=node, vmid=vmid, target=target,
                       params=params or {}, dry_run=dry_run, upid=upid, message=message,
                       finished_at=None if status in ("submitted", "running") else utcnow())
    async with db.session() as s:
        s.add(entry)
        await s.commit()
    # Also to stdout, so it ends up in `docker compose logs` / a log collector.
    log.info("%s %s %s/%s/%s %s%s %s", actor.name, action, cluster or "-", node or "-",
             vmid or "-", status, " [dry-run]" if dry_run else "", message or "")
    return entry.id


async def finish(db: Database, entry_id: int, status: str, message: str | None = None) -> None:
    async with db.session() as s:
        entry = await s.get(AuditEntry, entry_id)
        if entry is None:
            return
        entry.status = status
        if message:
            entry.message = message
        if status not in ("submitted", "running"):
            entry.finished_at = utcnow()
        await s.commit()


async def pending_tasks(db: Database) -> list[AuditEntry]:
    async with db.session() as s:
        res = await s.scalars(select(AuditEntry).where(
            AuditEntry.status.in_(("submitted", "running")), AuditEntry.upid.is_not(None)))
        return list(res)


async def query(db: Database, *, actor: str | None = None, action: str | None = None,
                status: str | None = None, cluster: str | None = None, vmid: int | None = None,
                before_id: int | None = None, limit: int = 100) -> list[AuditView]:
    stmt = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
    if actor:
        stmt = stmt.where(AuditEntry.actor == actor)
    if action:
        stmt = stmt.where(AuditEntry.action.startswith(action))
    if status:
        stmt = stmt.where(AuditEntry.status == status)
    if cluster:
        stmt = stmt.where(AuditEntry.cluster == cluster)
    if vmid is not None:
        stmt = stmt.where(AuditEntry.vmid == vmid)
    if before_id:
        stmt = stmt.where(AuditEntry.id < before_id)
    async with db.session() as s:
        return [AuditView.model_validate(e) for e in await s.scalars(stmt)]
