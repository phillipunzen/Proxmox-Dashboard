"""Thin async client for the dashboard REST API (authenticated with an API key)."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class DashboardError(ToolError):
    """API error - a ToolError, so the message reaches the model (not hidden as a crash)."""


class DashboardClient:
    def __init__(self, base_url: str, api_key: str, verify_tls: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key.startswith("pvd_"):
            raise ValueError("DASHBOARD_API_KEY fehlt oder hat das falsche Format (pvd_…)")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=30, verify=verify_tls, transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    async def _handle(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise DashboardError(f"Dashboard-API {resp.status_code}: {detail}")
        return resp.json() if resp.content else None

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            return await self._handle(await self._http.get(path, params=clean))
        except httpx.HTTPError as exc:
            raise DashboardError(f"Dashboard nicht erreichbar: {exc}") from exc

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            return await self._handle(await self._http.post(path, json=body or {}))
        except httpx.HTTPError as exc:
            raise DashboardError(f"Dashboard nicht erreichbar: {exc}") from exc

    async def aclose(self) -> None:
        await self._http.aclose()
