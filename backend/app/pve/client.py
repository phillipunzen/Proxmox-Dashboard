"""Async client for the Proxmox VE REST API (``/api2/json``) using API tokens.

Only API tokens are supported (no ticket/password login). The token is sent as::

    Authorization: PVEAPIToken=<user>@<realm>!<tokenid>=<secret>

A cluster may list several node URLs; on connection errors or 5xx responses the
client fails over to the next URL and remembers the last working one.
"""

from __future__ import annotations

import logging
import ssl
from typing import Any, Protocol

import httpx

from app.config import ClusterConfig

log = logging.getLogger(__name__)


class PveError(Exception):
    """The API answered, but with an error (4xx / 5xx after failover)."""

    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__(f"PVE API {status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


class PveUnavailable(Exception):
    """None of the configured URLs could be reached."""


class PveApi(Protocol):
    """Minimal interface shared by the real client and the mock."""

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...

    async def aclose(self) -> None: ...


def _verify_option(cfg: ClusterConfig) -> bool | ssl.SSLContext:
    if not cfg.verify_tls:
        return False
    if cfg.ca_file:
        return ssl.create_default_context(cafile=cfg.ca_file)
    return True


class PveClient:
    def __init__(self, cfg: ClusterConfig, transport: httpx.AsyncBaseTransport | None = None):
        if not cfg.token_id or not cfg.token_secret.get_secret_value():
            raise ValueError(f"Cluster '{cfg.name}': PVE token id/secret missing")
        self.cfg = cfg
        self._preferred = 0
        token = f"PVEAPIToken={cfg.token_id}={cfg.token_secret.get_secret_value()}"
        self._http = httpx.AsyncClient(
            headers={"Authorization": token, "Accept": "application/json"},
            verify=_verify_option(cfg),
            timeout=cfg.timeout,
            transport=transport,
        )

    @property
    def current_url(self) -> str:
        return self.cfg.urls[self._preferred]

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        urls = self.cfg.urls
        order = [(self._preferred + i) % len(urls) for i in range(len(urls))]
        last_exc: Exception | None = None

        for idx in order:
            url = f"{urls[idx]}/api2/json{path}"
            try:
                resp = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                log.warning("PVE %s unreachable (%s): %s", urls[idx], type(exc).__name__, exc)
                last_exc = exc
                continue

            if resp.status_code >= 500 and len(urls) > 1:
                # A node in trouble (e.g. pveproxy restarting) - try the next one.
                last_exc = PveError(resp.status_code, resp.reason_phrase, path)
                continue
            if resp.status_code >= 400:
                raise PveError(resp.status_code, _error_text(resp), path)

            if idx != self._preferred:
                log.info("PVE cluster '%s' now using %s", self.cfg.name, urls[idx])
                self._preferred = idx
            return resp.json().get("data")

        if isinstance(last_exc, PveError):
            raise last_exc
        raise PveUnavailable(f"Cluster '{self.cfg.name}': no URL reachable ({last_exc})")

    async def aclose(self) -> None:
        await self._http.aclose()


def _error_text(resp: httpx.Response) -> str:
    """PVE puts parameter errors into ``errors`` and the summary into the reason phrase."""
    try:
        body = resp.json()
    except ValueError:
        return resp.reason_phrase
    if isinstance(body, dict) and body.get("errors"):
        return f"{resp.reason_phrase}: {body['errors']}"
    return resp.reason_phrase
