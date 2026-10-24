"""Alert rule evaluation and notification.

State machine per (rule, target):
    ok --condition true--> pending --held for `for_seconds`--> firing --condition false--> resolved
Firing alerts are persisted as AlertEvent rows (also the history). Notifications go out on
firing and (optionally) on resolve; `repeat_minutes` re-sends while an alert keeps firing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.alerts.metrics import REGISTRY, Context, compare, matches
from app.alerts.notify import SEVERITY_ORDER, Notification, Notifier
from app.collector.state import SnapshotStore
from app.config import Settings
from app.core.crypto import Encryptor
from app.db.engine import Database
from app.db.models import AlertEvent, AlertRule, KeyValue, NotificationChannel, utcnow
from app.inventory.collector import InventoryStore
from app.pve import PveApi, PveError, PveUnavailable

log = logging.getLogger(__name__)

BACKUP_WINDOW = 26 * 3600
BACKUP_REFRESH = 300

DEFAULT_RULES: list[dict[str, Any]] = [
    {"name": "Cluster nicht erreichbar", "metric": "cluster_unreachable", "operator": ">=",
     "threshold": 1, "for_seconds": 60, "severity": "critical"},
    {"name": "Kein Quorum", "metric": "cluster_no_quorum", "operator": ">=", "threshold": 1,
     "for_seconds": 0, "severity": "critical"},
    {"name": "Node offline", "metric": "node_offline", "operator": ">=", "threshold": 1,
     "for_seconds": 60, "severity": "critical"},
    {"name": "RAM-Belegung hoch", "metric": "node_mem", "operator": ">", "threshold": 0.9,
     "for_seconds": 300, "severity": "warning"},
    {"name": "IO-Delay hoch", "metric": "node_iowait", "operator": ">", "threshold": 0.15,
     "for_seconds": 300, "severity": "warning"},
    {"name": "Temperatur hoch", "metric": "node_temperature", "operator": ">", "threshold": 80,
     "for_seconds": 120, "severity": "warning"},
    {"name": "Storage fast voll", "metric": "storage_usage", "operator": ">", "threshold": 0.85,
     "for_seconds": 0, "severity": "warning"},
    {"name": "Storage voll", "metric": "storage_usage", "operator": ">", "threshold": 0.95,
     "for_seconds": 0, "severity": "critical"},
    {"name": "SMART-Fehler", "metric": "disk_smart_failed", "operator": ">=", "threshold": 1,
     "for_seconds": 0, "severity": "critical"},
    {"name": "SSD-Verschleiß hoch", "metric": "disk_wearout", "operator": ">", "threshold": 80,
     "for_seconds": 0, "severity": "warning"},
    {"name": "Backup fehlgeschlagen", "metric": "backup_failed", "operator": ">=",
     "threshold": 1, "for_seconds": 0, "severity": "warning"},
    {"name": "Gast mit Autostart läuft nicht", "metric": "guest_down", "operator": ">=",
     "threshold": 1, "for_seconds": 300, "severity": "warning"},
    {"name": "Neustart erforderlich", "metric": "node_reboot_required", "operator": ">=",
     "threshold": 1, "for_seconds": 0, "severity": "info"},
    {"name": "Kein aktuelles Backup", "metric": "backup_age_hours", "operator": ">",
     "threshold": 48, "for_seconds": 0, "severity": "warning"},
    {"name": "Alter Snapshot", "metric": "snapshot_age_days", "operator": ">", "threshold": 30,
     "for_seconds": 0, "severity": "info"},
    {"name": "Storage läuft voll (Prognose)", "metric": "storage_days_until_full",
     "operator": "<", "threshold": 14, "for_seconds": 0, "severity": "warning"},
    {"name": "Gast ohne Backup-Job", "metric": "backup_not_covered", "operator": ">=",
     "threshold": 1, "for_seconds": 0, "severity": "info", "enabled": False},
]


def _fmt(value: float, unit: str) -> str:
    if unit == "ratio":
        return f"{value * 100:.0f} %"
    if unit == "celsius":
        return f"{value:.0f} °C"
    if unit == "percent":
        return f"{value:.0f} %"
    if unit == "bool":
        return "ja" if value else "nein"
    if unit == "hours":
        return "nie" if value >= 99999 else f"{value:.0f} h"
    if unit == "days":
        return f"{value:.0f} Tage"
    return f"{value:g}"


class AlertEngine:
    def __init__(self, db: Database, settings: Settings, store: SnapshotStore,
                 inventory: InventoryStore, nodes: Any, read_clients: dict[str, PveApi],
                 encryptor: Encryptor, notifier: Notifier | None = None):
        self.db = db
        self.settings = settings
        self.store = store
        self.inventory = inventory
        self.nodes = nodes
        self.clients = read_clients
        self.enc = encryptor
        self.notifier = notifier or Notifier()
        self._pending: dict[tuple[int, str], float] = {}
        self._failed_backups: dict[tuple[str, str], str] = {}
        self._backups_at = 0.0
        self._task: asyncio.Task[None] | None = None
        self.clock = time.time
        self.backups: Any = None  # BackupService, set after construction

    # --- lifecycle ------------------------------------------------------------------------------

    async def seed_defaults(self) -> None:
        """Add default rules that were never seeded (new versions bring new defaults).

        Rules the user deleted are not re-created: seeded names are remembered.
        """
        async with self.db.session() as s:
            kv = await s.get(KeyValue, "alert_defaults")
            if kv is None:
                existing = {r.name for r in await s.scalars(select(AlertRule))}
                kv = KeyValue(key="alert_defaults", value={"seeded": sorted(existing)})
                s.add(kv)
            seeded = set(kv.value.get("seeded", []))
            for r in DEFAULT_RULES:
                if r["name"] not in seeded:
                    s.add(AlertRule(**r))
                    seeded.add(r["name"])
            kv.value = {"seeded": sorted(seeded)}
            await s.commit()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="alerts")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.notifier.aclose()

    async def _loop(self) -> None:
        version = -1
        while True:
            await self.store.wait_for_change(version, 60)
            version = self.store.version
            try:
                await self.evaluate()
            except Exception:  # noqa: BLE001
                log.exception("Alert evaluation failed")
            await asyncio.sleep(self.settings.alert_eval_interval)

    # --- data -----------------------------------------------------------------------------------

    async def _refresh_backups(self) -> None:
        """Latest vzdump result per guest (or per node for multi-guest jobs) within 26 h."""
        if self.clock() - self._backups_at < BACKUP_REFRESH:
            return
        self._backups_at = self.clock()
        cutoff = self.clock() - BACKUP_WINDOW
        failed: dict[tuple[str, str], str] = {}
        for cluster, client in self.clients.items():
            try:
                tasks = await client.get("/cluster/tasks")
            except (PveError, PveUnavailable):
                continue
            latest: dict[str, dict[str, Any]] = {}
            for t in tasks or []:
                if t.get("type") != "vzdump" or t.get("status") is None:
                    continue
                if t.get("starttime", 0) < cutoff:
                    continue
                key = str(t.get("id") or f"node:{t.get('node')}")
                if key not in latest or t["starttime"] > latest[key]["starttime"]:
                    latest[key] = t
            for key, t in latest.items():
                if t["status"] != "OK" and not str(t["status"]).startswith("WARNINGS"):
                    failed[(cluster, key)] = f"{t['status']} ({t.get('node')})"
        self._failed_backups = failed

    async def context(self) -> Context:
        await self._refresh_backups()
        upd = self.nodes.state.updates if self.nodes else {}
        return Context(
            snapshots=self.store.all(), inventory=self.inventory.all(),
            temps=dict(self.nodes.state.temps) if self.nodes else {},
            updates={k: len(v.packages) for k, v in upd.items() if v.error is None},
            reboot_required={k: bool(v.reboot_required) for k, v in upd.items()
                             if v.reboot_required is not None},
            failed_backups=dict(self._failed_backups),
            backups=[g for rows in self.backups.guests.values() for g in rows]
            if self.backups else [],
            forecasts=list(self.backups.forecasts) if self.backups else [],
        )

    # --- evaluation -----------------------------------------------------------------------------

    async def evaluate(self) -> None:
        ctx = await self.context()
        reachable = {s.name for s in ctx.snapshots if s.reachable}
        now = self.clock()
        async with self.db.session() as s:
            rules = list(await s.scalars(select(AlertRule)))
            firing = {(e.rule_id, e.target): e for e in await s.scalars(
                select(AlertEvent).where(AlertEvent.state == "firing"))}
        active_rules = {r.id for r in rules if r.enabled}

        # Rules that were disabled or deleted: resolve what they left firing.
        for (rule_id, target), ev in list(firing.items()):
            if rule_id not in active_rules:
                await self._resolve(ev, None, "Regel deaktiviert oder gelöscht")
                firing.pop((rule_id, target))

        for rule in rules:
            if not rule.enabled or rule.metric not in REGISTRY:
                continue
            info, extractor = REGISTRY[rule.metric]
            seen: set[str] = set()
            for sample in extractor(ctx):
                if not matches(rule.selector or {}, sample.attrs):
                    continue
                seen.add(sample.target)
                key = (rule.id, sample.target)
                hit = compare(sample.value, rule.operator, rule.threshold)
                ev = firing.get(key)
                if hit:
                    since = self._pending.setdefault(key, now)
                    if ev is None and now - since >= rule.for_seconds:
                        if info.unit == "bool":
                            msg = f"{sample.label}: {info.label}"
                        else:
                            msg = (f"{sample.label}: {info.label} = "
                                   f"{_fmt(sample.value, info.unit)} (Schwelle {rule.operator} "
                                   f"{_fmt(rule.threshold, info.unit)})")
                        if sample.attrs.get("detail"):
                            msg += f" – {sample.attrs['detail']}"
                        await self._fire(rule, sample.target, sample.value, msg)
                    elif ev is not None:
                        await self._maybe_repeat(rule, ev, sample.value)
                else:
                    self._pending.pop(key, None)
                    if ev is not None:
                        await self._resolve(ev, rule, None, sample.value)
            # Targets that vanished (guest deleted, disk removed ...) resolve - but not while
            # their cluster is unreachable (then we simply don't know).
            for (rid, target), ev in firing.items():
                if rid != rule.id or target in seen:
                    continue
                parts = target.split("/")
                if len(parts) > 1 and parts[1] not in reachable and rule.metric != \
                        "cluster_unreachable":
                    continue
                self._pending.pop((rid, target), None)
                await self._resolve(ev, rule, "Ziel nicht mehr vorhanden")

    async def _fire(self, rule: AlertRule, target: str, value: float, message: str) -> None:
        ev = AlertEvent(rule_id=rule.id, rule_name=rule.name, target=target,
                        severity=rule.severity, state="firing", value=value, message=message,
                        last_notified_at=utcnow())
        async with self.db.session() as s:
            s.add(ev)
            await s.commit()
        log.warning("ALERT %s: %s", rule.severity.upper(), message)
        await self._notify(rule, ev, "firing")

    async def _maybe_repeat(self, rule: AlertRule, ev: AlertEvent, value: float) -> None:
        if not rule.repeat_minutes or ev.acknowledged_by:
            return
        last = ev.last_notified_at or ev.started_at
        if utcnow() - last < timedelta(minutes=rule.repeat_minutes):
            return
        async with self.db.session() as s:
            row = await s.get(AlertEvent, ev.id)
            if row:
                row.last_notified_at = utcnow()
                row.value = value
                await s.commit()
        await self._notify(rule, ev, "firing")

    async def _resolve(self, ev: AlertEvent, rule: AlertRule | None, note: str | None,
                       value: float | None = None) -> None:
        async with self.db.session() as s:
            row = await s.get(AlertEvent, ev.id)
            if row is None or row.state != "firing":
                return
            row.state = "resolved"
            row.resolved_at = utcnow()
            if note:
                row.message += f" ({note})"
            await s.commit()
            ev = row
        log.info("RESOLVED: %s", ev.message)
        if rule is not None:
            await self._notify(rule, ev, "resolved")

    # --- notifications --------------------------------------------------------------------------

    async def _notify(self, rule: AlertRule, ev: AlertEvent, kind: str) -> None:
        if not rule.channel_ids:
            return
        async with self.db.session() as s:
            channels = list(await s.scalars(select(NotificationChannel).where(
                NotificationChannel.id.in_(rule.channel_ids),
                NotificationChannel.enabled.is_(True))))
        n = self._notification(ev, kind)
        for ch in channels:
            if SEVERITY_ORDER.get(ev.severity, 0) < SEVERITY_ORDER.get(ch.min_severity, 0):
                continue
            if kind == "resolved" and not ch.send_resolved:
                continue
            try:
                await self.notifier.send(ch.type, self.channel_config(ch), n)
            except Exception as exc:  # noqa: BLE001 - one broken channel must not stop others
                log.warning("Notification via %s (%s) failed: %s", ch.name, ch.type, exc)

    def _notification(self, ev: AlertEvent, kind: str) -> Notification:
        base = self.settings.dashboard_url.rstrip("/")
        return Notification(kind=kind, rule=ev.rule_name, severity=ev.severity,  # type: ignore[arg-type]
                            target=ev.target, message=ev.message, value=ev.value,
                            started_at=ev.started_at.isoformat() if ev.started_at else None,
                            resolved_at=ev.resolved_at.isoformat() if ev.resolved_at else None,
                            url=f"{base}/alerts" if base else None)

    def channel_config(self, ch: NotificationChannel) -> dict[str, Any]:
        return json.loads(self.enc.decrypt(ch.config_enc))

    async def test_channel(self, ch: NotificationChannel) -> None:
        n = Notification(kind="test", rule="Testnachricht", severity="info",
                         target="Proxmox Dashboard",
                         message=f"Kanal '{ch.name}' funktioniert ({datetime.now(UTC):%H:%M:%S} "
                                 "UTC).",
                         url=self.settings.dashboard_url or None)
        await self.notifier.send(ch.type, self.channel_config(ch), n)

    # --- queries --------------------------------------------------------------------------------

    async def firing_counts(self) -> dict[str, int]:
        async with self.db.session() as s:
            rows = await s.execute(select(AlertEvent.severity, func.count()).where(
                AlertEvent.state == "firing").group_by(AlertEvent.severity))
            return {sev: int(n) for sev, n in rows}

    async def acknowledge(self, event_id: int, user: str) -> AlertEvent:
        async with self.db.session() as s:
            ev = await s.get(AlertEvent, event_id)
            if ev is None:
                raise LookupError(event_id)
            ev.acknowledged_by = user
            await s.commit()
            return ev
