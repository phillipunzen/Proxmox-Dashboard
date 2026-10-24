"""Alerts, rules and notification channels."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import audit
from app.alerts.engine import AlertEngine
from app.alerts.metrics import REGISTRY, MetricInfo, catalogue
from app.alerts.notify import ChannelType, masked, merge_secrets, validate_config
from app.auth.deps import Principal, actor, admin, operator, viewer
from app.db.models import AlertEvent, AlertRule, NotificationChannel

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

Severity = Literal["info", "warning", "critical"]


def _engine(request: Request) -> AlertEngine:
    return request.app.state.alerts


class EventView(BaseModel):
    id: int
    rule_id: int | None
    rule_name: str
    target: str
    severity: str
    state: str
    value: float | None
    message: str
    started_at: datetime
    resolved_at: datetime | None
    acknowledged_by: str | None

    model_config = {"from_attributes": True}


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    metric: str
    operator: Literal[">", ">=", "<", "<="] = ">"
    threshold: float
    for_seconds: int = Field(0, ge=0, le=86400)
    severity: Severity = "warning"
    selector: dict[str, Any] = Field(default_factory=dict,
                                     description="cluster, node, vmid, tag, storage")
    channel_ids: list[int] = []
    repeat_minutes: int = Field(0, ge=0, le=10080)


class RuleView(RuleIn):
    id: int
    model_config = {"from_attributes": True}


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: ChannelType
    config: dict[str, Any]
    enabled: bool = True
    min_severity: Severity = "warning"
    send_resolved: bool = True


class ChannelView(BaseModel):
    id: int
    name: str
    type: str
    config: dict[str, Any]
    enabled: bool
    min_severity: str
    send_resolved: bool


# --- events ---------------------------------------------------------------------------------


@router.get("", response_model=list[EventView])
async def events(request: Request, state: Literal["firing", "resolved", "all"] = "firing",
                 limit: Annotated[int, Query(ge=1, le=1000)] = 200,
                 _: Principal = viewer) -> list[EventView]:
    stmt = select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit)
    if state != "all":
        stmt = stmt.where(AlertEvent.state == state)
    async with request.app.state.db.session() as s:
        return [EventView.model_validate(e) for e in await s.scalars(stmt)]


@router.post("/{event_id}/ack", response_model=EventView,
             summary="Acknowledge (stops repeated notifications)")
async def acknowledge(request: Request, event_id: int, p: Principal = operator) -> EventView:
    try:
        ev = await _engine(request).acknowledge(event_id, p.name)
    except LookupError as exc:
        raise HTTPException(404, "Alarm nicht gefunden") from exc
    await audit.record(request.app.state.db, actor(request, p), "alert.ack", "ok",
                       target=ev.target, params={"event": event_id})
    return EventView.model_validate(ev)


@router.get("/metrics", response_model=list[MetricInfo], summary="Metrics usable in rules")
async def metrics(_: Principal = viewer) -> list[MetricInfo]:
    return catalogue()


# --- rules ----------------------------------------------------------------------------------


@router.get("/rules", response_model=list[RuleView])
async def rules(request: Request, _: Principal = viewer) -> list[RuleView]:
    async with request.app.state.db.session() as s:
        return [RuleView.model_validate(r)
                for r in await s.scalars(select(AlertRule).order_by(AlertRule.id))]


def _check_rule(body: RuleIn) -> None:
    if body.metric not in REGISTRY:
        raise HTTPException(422, f"Unbekannte Metrik '{body.metric}'")
    unknown = set(body.selector) - {"cluster", "node", "vmid", "tag", "storage"}
    if unknown:
        raise HTTPException(422, f"Unbekannte Selektor-Felder: {', '.join(sorted(unknown))}")


@router.post("/rules", response_model=RuleView, status_code=201)
async def create_rule(request: Request, body: RuleIn, p: Principal = admin) -> RuleView:
    _check_rule(body)
    rule = AlertRule(**body.model_dump())
    async with request.app.state.db.session() as s:
        s.add(rule)
        await s.commit()
    await audit.record(request.app.state.db, actor(request, p), "alert.rule.create", "ok",
                       target=body.name, params=body.model_dump(mode="json"))
    return RuleView.model_validate(rule)


@router.put("/rules/{rule_id}", response_model=RuleView)
async def update_rule(request: Request, rule_id: int, body: RuleIn,
                      p: Principal = admin) -> RuleView:
    _check_rule(body)
    async with request.app.state.db.session() as s:
        rule = await s.get(AlertRule, rule_id)
        if rule is None:
            raise HTTPException(404, "Regel nicht gefunden")
        for k, v in body.model_dump().items():
            setattr(rule, k, v)
        await s.commit()
    await audit.record(request.app.state.db, actor(request, p), "alert.rule.update", "ok",
                       target=body.name, params=body.model_dump(mode="json"))
    return RuleView.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(request: Request, rule_id: int, p: Principal = admin) -> None:
    async with request.app.state.db.session() as s:
        rule = await s.get(AlertRule, rule_id)
        if rule is None:
            raise HTTPException(404, "Regel nicht gefunden")
        await s.delete(rule)
        await s.commit()
    await audit.record(request.app.state.db, actor(request, p), "alert.rule.delete", "ok",
                       target=rule.name)


# --- channels -------------------------------------------------------------------------------


def _channel_view(request: Request, ch: NotificationChannel) -> ChannelView:
    return ChannelView(id=ch.id, name=ch.name, type=ch.type, enabled=ch.enabled,
                       min_severity=ch.min_severity, send_resolved=ch.send_resolved,
                       config=masked(_engine(request).channel_config(ch)))


@router.get("/channels", response_model=list[ChannelView])
async def channels(request: Request, _: Principal = admin) -> list[ChannelView]:
    async with request.app.state.db.session() as s:
        rows = await s.scalars(select(NotificationChannel).order_by(NotificationChannel.name))
        return [_channel_view(request, ch) for ch in rows]


@router.post("/channels", response_model=ChannelView, status_code=201)
async def create_channel(request: Request, body: ChannelIn, p: Principal = admin) -> ChannelView:
    try:
        cfg = validate_config(body.type, body.config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    ch = NotificationChannel(name=body.name, type=body.type, enabled=body.enabled,
                             min_severity=body.min_severity, send_resolved=body.send_resolved,
                             config_enc=request.app.state.encryptor.encrypt(json.dumps(cfg)))
    async with request.app.state.db.session() as s:
        if await s.scalar(select(NotificationChannel).where(NotificationChannel.name == body.name)):
            raise HTTPException(422, f"Kanal '{body.name}' existiert bereits")
        s.add(ch)
        await s.commit()
    await audit.record(request.app.state.db, actor(request, p), "alert.channel.create", "ok",
                       target=body.name, params={"type": body.type})
    return _channel_view(request, ch)


@router.put("/channels/{channel_id}", response_model=ChannelView)
async def update_channel(request: Request, channel_id: int, body: ChannelIn,
                         p: Principal = admin) -> ChannelView:
    async with request.app.state.db.session() as s:
        ch = await s.get(NotificationChannel, channel_id)
        if ch is None:
            raise HTTPException(404, "Kanal nicht gefunden")
        old = _engine(request).channel_config(ch)
        try:
            cfg = validate_config(body.type, merge_secrets(body.config, old))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        ch.name, ch.type, ch.enabled = body.name, body.type, body.enabled
        ch.min_severity, ch.send_resolved = body.min_severity, body.send_resolved
        ch.config_enc = request.app.state.encryptor.encrypt(json.dumps(cfg))
        await s.commit()
    await audit.record(request.app.state.db, actor(request, p), "alert.channel.update", "ok",
                       target=body.name)
    return _channel_view(request, ch)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(request: Request, channel_id: int, p: Principal = admin) -> None:
    async with request.app.state.db.session() as s:
        ch = await s.get(NotificationChannel, channel_id)
        if ch is None:
            raise HTTPException(404, "Kanal nicht gefunden")
        await s.delete(ch)
        await s.commit()
    await audit.record(request.app.state.db, actor(request, p), "alert.channel.delete", "ok",
                       target=ch.name)


@router.post("/channels/{channel_id}/test", summary="Send a test notification")
async def test_channel(request: Request, channel_id: int,
                       _: Principal = admin) -> dict[str, Any]:
    async with request.app.state.db.session() as s:
        ch = await s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "Kanal nicht gefunden")
    try:
        await _engine(request).test_channel(ch)
    except Exception as exc:  # noqa: BLE001 - report any delivery problem to the admin
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "Testnachricht gesendet"}
