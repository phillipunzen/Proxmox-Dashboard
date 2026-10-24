"""Notification channels: webhook, ntfy, Gotify, e-mail (SMTP), Home Assistant."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Literal

import httpx
from pydantic import BaseModel

ChannelType = Literal["webhook", "ntfy", "gotify", "email", "homeassistant"]
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

# Fields that contain secrets: never returned by the API (masked), kept on update when masked.
SECRET_FIELDS = {"token", "password", "secret"}
MASK = "••••••"

# Required config fields per type (for validation and the UI form).
FIELDS: dict[str, list[str]] = {
    "webhook": ["url", "secret?"],
    "ntfy": ["url", "topic", "token?"],
    "gotify": ["url", "token"],
    "email": ["host", "port", "security", "username?", "password?", "sender", "recipients"],
    "homeassistant": ["url", "webhook_id"],
}


class Notification(BaseModel):
    kind: Literal["firing", "resolved", "test"]
    rule: str
    severity: str
    target: str
    message: str
    value: float | None = None
    started_at: str | None = None
    resolved_at: str | None = None
    url: str | None = None

    @property
    def title(self) -> str:
        tag = {"firing": {"critical": "KRITISCH", "warning": "WARNUNG", "info": "INFO"}.get(
            self.severity, self.severity.upper()), "resolved": "OK", "test": "TEST"}[self.kind]
        return f"[{tag}] {self.rule}: {self.target}"

    @property
    def body(self) -> str:
        lines = [self.message]
        if self.kind == "resolved":
            lines.append("Der Zustand ist wieder normal.")
        if self.url:
            lines.append(self.url)
        return "\n".join(lines)


def validate_config(ctype: str, cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in FIELDS[ctype]:
        name, optional = f.rstrip("?"), f.endswith("?")
        v = cfg.get(name)
        if v in (None, "") and not optional:
            raise ValueError(f"Feld '{name}' fehlt")
        if v not in (None, ""):
            out[name] = v
    if "url" in out and not str(out["url"]).startswith(("http://", "https://")):
        raise ValueError("URL muss mit http:// oder https:// beginnen")
    if ctype == "email":
        out["port"] = int(out["port"])
        if out["security"] not in ("starttls", "ssl", "none"):
            raise ValueError("security: starttls, ssl oder none")
        rcpt = out["recipients"]
        out["recipients"] = [r.strip() for r in (rcpt.split(",") if isinstance(rcpt, str)
                                                 else rcpt) if r.strip()]
    return out


def masked(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: (MASK if k in SECRET_FIELDS and v else v) for k, v in cfg.items()}


def merge_secrets(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Keep stored secrets when the UI sends the mask back unchanged."""
    return {**new, **{k: old[k] for k in SECRET_FIELDS if new.get(k) == MASK and k in old}}


def payload(n: Notification) -> dict[str, Any]:
    return {"event": n.kind, "title": n.title, **n.model_dump()}


class Notifier:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15):
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def send(self, ctype: str, cfg: dict[str, Any], n: Notification) -> None:
        """Raise on failure (callers log it)."""
        if ctype == "webhook":
            body = json.dumps(payload(n)).encode()
            headers = {"Content-Type": "application/json"}
            if cfg.get("secret"):
                sig = hmac.new(cfg["secret"].encode(), body, hashlib.sha256).hexdigest()
                headers["X-Signature-256"] = f"sha256={sig}"
            await self._post(cfg["url"], content=body, headers=headers)
        elif ctype == "homeassistant":
            url = f"{cfg['url'].rstrip('/')}/api/webhook/{cfg['webhook_id']}"
            await self._post(url, json=payload(n))
        elif ctype == "ntfy":
            # JSON publishing (POST to the server root) keeps UTF-8 titles intact.
            prio = 2 if n.kind == "resolved" else {"critical": 5, "warning": 4}.get(
                n.severity, 3)
            emoji = {"critical": "rotating_light", "warning": "warning"}
            tag = ("white_check_mark" if n.kind == "resolved"
                   else emoji.get(n.severity, "information_source"))
            body_ntfy: dict[str, Any] = {"topic": cfg["topic"], "title": n.title,
                                         "message": n.body, "priority": prio, "tags": [tag]}
            if n.url:
                body_ntfy["click"] = n.url
            headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
            await self._post(cfg["url"].rstrip("/"), json=body_ntfy, headers=headers)
        elif ctype == "gotify":
            prio = 2 if n.kind == "resolved" else {"critical": 9, "warning": 6}.get(n.severity, 4)
            body: dict[str, Any] = {"title": n.title, "message": n.body, "priority": prio}
            if n.url:
                body["extras"] = {"client::notification": {"click": {"url": n.url}}}
            await self._post(f"{cfg['url'].rstrip('/')}/message", json=body,
                             headers={"X-Gotify-Key": cfg["token"]})
        elif ctype == "email":
            await asyncio.get_running_loop().run_in_executor(None, _send_mail, cfg, n)
        else:
            raise ValueError(f"Unbekannter Kanaltyp {ctype}")

    async def _post(self, url: str, **kwargs: Any) -> None:
        resp = await self._http.post(url, **kwargs)
        if resp.status_code >= 300:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    async def aclose(self) -> None:
        await self._http.aclose()


def _send_mail(cfg: dict[str, Any], n: Notification) -> None:
    msg = EmailMessage()
    msg["Subject"] = n.title
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg.set_content(n.body)
    ctx = ssl.create_default_context()
    if cfg["security"] == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=20)
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
    with smtp:
        if cfg["security"] == "starttls":
            smtp.starttls(context=ctx)
        if cfg.get("username"):
            smtp.login(cfg["username"], cfg.get("password", ""))
        smtp.send_message(msg)
