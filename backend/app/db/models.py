"""SQLite schema: users, sessions, audit log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite has no time zones: store UTC, hand out timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[dict[str, Any]]: JSON, list[int]: JSON,
                           list[str]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # viewer | operator | admin
    totp_secret_enc: Mapped[str | None] = mapped_column(String(255), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last accepted TOTP time step - prevents replaying the same code.
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class Session(Base):
    """Server-side session. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token) hex
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    # False until the second factor was entered (or TOTP is not used by this user).
    mfa_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    actor_role: Mapped[str | None] = mapped_column(String(16), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    # e.g. guest.start, guest.snapshot.rollback, auth.login, user.create
    action: Mapped[str] = mapped_column(String(64), index=True)
    cluster: Mapped[str | None] = mapped_column(String(64), default=None)
    node: Mapped[str | None] = mapped_column(String(64), default=None)
    vmid: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    target: Mapped[str | None] = mapped_column(String(128), default=None)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    # submitted -> running -> ok | error ; or directly ok | error | denied
    status: Mapped[str] = mapped_column(String(16), index=True)
    upid: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    message: Mapped[str | None] = mapped_column(Text, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class ApiKey(Base):
    """API key for tools (MCP server, Home Assistant, scripts). Only the SHA-256 is stored."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # shown in the UI to recognise a key
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON)  # read | control | admin
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class NodeState(Base):
    """Dashboard-side node state (maintenance flag, trusted SSH host key)."""

    __tablename__ = "node_state"

    cluster: Mapped[str] = mapped_column(String(64), primary_key=True)
    node: Mapped[str] = mapped_column(String(64), primary_key=True)
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    maintenance_since: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    maintenance_by: Mapped[str | None] = mapped_column(String(64), default=None)
    # Guests moved away when entering maintenance: [{"vmid": .., "target": ..}]
    evacuated: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    ssh_host_key: Mapped[str | None] = mapped_column(Text, default=None)  # OpenSSH public key


class Job(Base):
    """Long-running dashboard job (node upgrade, maintenance, reboot) with a log file."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # upgrade | maintenance | ...
    cluster: Mapped[str] = mapped_column(String(64))
    node: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), index=True)  # running | ok | error | cancelled
    started_by: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    metric: Mapped[str] = mapped_column(String(48))
    operator: Mapped[str] = mapped_column(String(2), default=">")  # > >= < <=
    threshold: Mapped[float] = mapped_column(default=0.0)
    for_seconds: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    # Optional filters: {"cluster": "...", "node": "...", "tag": "...", "vmid": 100}
    selector: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    channel_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    repeat_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = notify once
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(160), index=True)  # e.g. node/homelab/pve1
    severity: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), index=True)  # firing | resolved
    value: Mapped[float | None] = mapped_column(default=None)
    message: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), default=None)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(16))  # webhook | ntfy | gotify | email | homeassistant
    config_enc: Mapped[str] = mapped_column(Text)  # JSON, encrypted (contains tokens/passwords)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_severity: Mapped[str] = mapped_column(String(16), default="warning")
    send_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
