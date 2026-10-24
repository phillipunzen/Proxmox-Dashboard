from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import Settings

ADMIN_PASSWORD = "admin-password-123"


class FakeClock:
    """Manually advanced clock so rate calculations are deterministic."""

    def __init__(self, start: float = 1_760_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(pve_mock=True, poll_interval=10, node_status_interval=30,
                    static_dir="/nonexistent", data_dir=str(tmp_path),
                    admin_password=SecretStr(ADMIN_PASSWORD), totp_required_roles="",
                    _env_file=None)


async def login(client: httpx.AsyncClient, username: str = "admin",
                password: str = ADMIN_PASSWORD) -> httpx.Response:
    """Log in and attach the CSRF token to all further requests of this client."""
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if r.status_code == 200:
        client.headers["X-CSRF-Token"] = r.json()["me"]["csrf_token"]
    return r


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@asynccontextmanager
async def logged_in(app: FastAPI, username: str = "admin",
                    password: str = ADMIN_PASSWORD) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app) as client:
        r = await login(client, username, password)
        assert r.status_code == 200, r.text
        yield client
