"""FastAPI application entry point.

Run locally:  ``uvicorn app.main:app --reload``  (with ``PVE_MOCK=true`` for synthetic data)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.v1.history import router as history_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.routes import router as v1_router
from app.collector.poller import Collector
from app.collector.state import SnapshotStore
from app.config import ClusterConfig, Settings, get_settings, load_clusters, mock_clusters
from app.core.web import SecurityHeadersMiddleware, mount_spa
from app.inventory.collector import InventoryCollector, InventoryStore
from app.pve import MockPveClient, PveApi, make_client
from app.tsdb.backfill import backfill_if_empty
from app.tsdb.client import TsdbClient

log = logging.getLogger("app")


def resolve_clusters(settings: Settings) -> list[ClusterConfig]:
    if settings.pve_mock:
        return mock_clusters()
    clusters = load_clusters()
    if not clusters:
        raise RuntimeError(
            "No clusters configured. Set PVE_1_NAME/PVE_1_URLS/PVE_1_TOKEN_ID/PVE_1_TOKEN_SECRET "
            "or PVE_MOCK=true."
        )
    return clusters


def create_app(settings: Settings | None = None,
               clients: dict[str, PveApi] | None = None,
               tsdb_client: TsdbClient | None = None) -> FastAPI:
    """Application factory. Tests inject settings, (mock) clients and a TSDB client."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        clusters = resolve_clusters(settings)
        cl = clients or {c.name: make_client(c) for c in clusters}
        tsdb = app.state.tsdb = tsdb_client or (TsdbClient(settings.vm_url)
                                                 if settings.vm_url else None)
        background: list[asyncio.Task[None]] = []
        if tsdb:
            tsdb.start()
            mocks = [c for c in cl.values() if isinstance(c, MockPveClient)]
            if mocks and settings.mock_backfill:
                background.append(asyncio.create_task(backfill_if_empty(tsdb, mocks)))
        else:
            log.info("VM_URL not set - history and load analysis disabled")

        collector = Collector(clusters, cl, app.state.store, settings, tsdb=tsdb)
        log.info("Starting collector for %d cluster(s)%s: %s", len(clusters),
                 " [MOCK]" if settings.pve_mock else "", ", ".join(c.name for c in clusters))
        await collector.start()
        app.state.clients = cl
        inventory = app.state.inventory_collector = InventoryCollector(
            clusters, cl, app.state.store, app.state.inventory, settings.inventory_interval)
        inventory.start()
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            await inventory.stop()
            await collector.stop()
            if tsdb:
                await tsdb.aclose()

    app = FastAPI(
        title="Proxmox Dashboard API",
        version=__version__,
        description="Monitoring API for Proxmox VE clusters. Versioned under `/api/v1`.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = SnapshotStore()
    app.state.tsdb = None
    app.state.inventory = InventoryStore()
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(v1_router)
    app.include_router(history_router)
    app.include_router(inventory_router)
    if not mount_spa(app, settings.static_dir):
        log.info("No frontend build at %s - serving API only", settings.static_dir)
    return app


app = create_app()
