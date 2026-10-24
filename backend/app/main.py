"""FastAPI application entry point.

Run locally:  ``uvicorn app.main:app --reload``  (with ``PVE_MOCK=true`` for synthetic data)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.v1.routes import router as v1_router
from app.collector.poller import Collector
from app.collector.state import SnapshotStore
from app.config import ClusterConfig, Settings, get_settings, load_clusters, mock_clusters
from app.core.web import SecurityHeadersMiddleware, mount_spa
from app.pve import PveApi, make_client

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
               clients: dict[str, PveApi] | None = None) -> FastAPI:
    """Application factory. Tests inject settings and (mock) clients."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        clusters = resolve_clusters(settings)
        cl = clients or {c.name: make_client(c) for c in clusters}
        collector = Collector(clusters, cl, app.state.store, settings)
        log.info("Starting collector for %d cluster(s)%s: %s", len(clusters),
                 " [MOCK]" if settings.pve_mock else "", ", ".join(c.name for c in clusters))
        await collector.start()
        try:
            yield
        finally:
            await collector.stop()

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
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(v1_router)
    if not mount_spa(app, settings.static_dir):
        log.info("No frontend build at %s - serving API only", settings.static_dir)
    return app


app = create_app()
