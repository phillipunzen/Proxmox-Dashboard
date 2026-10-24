"""FastAPI application entry point.

Run locally:  ``uvicorn app.main:app --reload``  (with ``PVE_MOCK=true`` for synthetic data)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app import __version__
from app.actions.service import ActionService, TaskWatcher
from app.alerts.engine import AlertEngine
from app.alerts.notify import Notifier
from app.api import prometheus
from app.api.v1.actions import router as actions_router
from app.api.v1.alerts import router as alerts_router
from app.api.v1.auth import Limiters, keys_router, users_router
from app.api.v1.auth import router as auth_router
from app.api.v1.history import router as history_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.nodes import router as nodes_router
from app.api.v1.routes import public_router
from app.api.v1.routes import router as v1_router
from app.auth.apikeys import ApiKeyService
from app.auth.deps import require_role
from app.auth.service import AuthService
from app.collector.poller import Collector
from app.collector.state import SnapshotStore
from app.config import ClusterConfig, Settings, get_settings, load_clusters, mock_clusters
from app.core.crypto import Encryptor, load_or_create_app_key
from app.core.web import OriginCheckMiddleware, SecurityHeadersMiddleware, mount_spa
from app.db.engine import Database
from app.inventory.collector import InventoryCollector, InventoryStore
from app.jobs import JobManager
from app.nodes.service import NodeService
from app.pve import (
    MockPveClient,
    PveApi,
    make_client,
    make_nodeadmin_client,
    make_operator_client,
)
from app.pve.mock_ssh import MockSshRunner
from app.ssh import AsyncSshRunner, SshRunner
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


async def _housekeeping(auth: AuthService) -> None:
    while True:
        await asyncio.sleep(3600)
        await auth.cleanup_sessions()


def create_app(settings: Settings | None = None,
               clients: dict[str, PveApi] | None = None,
               tsdb_client: TsdbClient | None = None,
               ssh_runner: SshRunner | None = None,
               notifier: Notifier | None = None) -> FastAPI:
    """Application factory. Tests inject settings, (mock) clients, TSDB client, SSH runner."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # --- persistence & auth ---------------------------------------------------------------
        db = app.state.db = Database.for_data_dir(settings.data_dir)
        await db.create_all()
        key = load_or_create_app_key(settings.data_dir, settings.app_secret_key.get_secret_value())
        enc = app.state.encryptor = Encryptor(key)
        auth = app.state.auth = AuthService(db, settings, enc)
        app.state.api_keys = ApiKeyService(db)
        generated = await auth.bootstrap_admin()
        if generated:
            # Printed once; the admin has to change it at the first login.
            log.warning("=" * 72)
            log.warning("Initial admin user '%s' created with password: %s",
                        settings.admin_username, generated)
            log.warning("Change it at the first login (enforced).")
            log.warning("=" * 72)

        # --- Proxmox --------------------------------------------------------------------------
        clusters = resolve_clusters(settings)
        cl = clients or {c.name: make_client(c) for c in clusters}
        op_clients = {c.name: op for c in clusters
                      if (op := make_operator_client(c, cl[c.name])) is not None}
        na_clients = {c.name: na for c in clusters
                      if (na := make_nodeadmin_client(c, cl[c.name])) is not None}
        tsdb = app.state.tsdb = tsdb_client or (TsdbClient(settings.vm_url)
                                                 if settings.vm_url else None)
        background: list[asyncio.Task[None]] = [asyncio.create_task(_housekeeping(auth))]
        if tsdb:
            tsdb.start()
            mocks = [c for c in cl.values() if isinstance(c, MockPveClient)]
            if mocks and settings.mock_backfill:
                background.append(asyncio.create_task(backfill_if_empty(tsdb, mocks)))
        else:
            log.info("VM_URL not set - history and load analysis disabled")

        collector = app.state.collector = Collector(clusters, cl, app.state.store, settings,
                                                    tsdb=tsdb)
        log.info("Starting collector for %d cluster(s)%s: %s", len(clusters),
                 " [MOCK]" if settings.pve_mock else "", ", ".join(c.name for c in clusters))
        await collector.start()
        app.state.clients = cl
        inventory = app.state.inventory_collector = InventoryCollector(
            clusters, cl, app.state.store, app.state.inventory, settings.inventory_interval)
        inventory.start()

        app.state.actions = ActionService(settings, clusters, op_clients, app.state.store, db)
        watcher = app.state.task_watcher = TaskWatcher(db, cl)
        watcher.start()

        jobs = app.state.jobs = JobManager(db, settings.data_dir)
        await jobs.recover()
        mocks = {n: c for n, c in cl.items() if isinstance(c, MockPveClient)}
        ssh = ssh_runner or (MockSshRunner(mocks) if mocks else
                             AsyncSshRunner(settings.data_dir, settings.ssh_user))
        nodes = app.state.nodes = NodeService(settings, clusters, cl, op_clients, na_clients,
                                              app.state.store, db, jobs, ssh)
        nodes.start()
        inventory.mainboard = nodes.mainboard

        alerts = app.state.alerts = AlertEngine(db, settings, app.state.store, app.state.inventory,
                                                nodes, cl, enc, notifier)
        await alerts.seed_defaults()
        alerts.start()
        mode = "READ-ONLY" if settings.read_only else ("DRY-RUN" if settings.dry_run else "LIVE")
        log.info("Control actions: %s (operator token for: %s)", mode,
                 ", ".join(op_clients) or "none")
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            await alerts.stop()
            await nodes.stop()
            await jobs.shutdown()
            await watcher.stop()
            await inventory.stop()
            await collector.stop()
            for extra in [*op_clients.values(), *na_clients.values()]:
                if extra not in cl.values():
                    await extra.aclose()
            if tsdb:
                await tsdb.aclose()
            await db.dispose()

    app = FastAPI(
        title="Proxmox Dashboard API",
        version=__version__,
        description="Monitoring API for Proxmox VE clusters. Versioned under `/api/v1`.\n\n"
                    "Authentication: session cookie (`POST /api/v1/auth/login`); write calls "
                    "need the `X-CSRF-Token` header from `GET /api/v1/auth/me`.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = SnapshotStore()
    app.state.tsdb = None
    app.state.inventory = InventoryStore()
    app.state.limiters = Limiters()
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(OriginCheckMiddleware)

    # Every data endpoint needs at least the viewer role; actions check finer roles themselves.
    logged_in = [Depends(require_role("viewer"))]
    app.include_router(public_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(keys_router)
    app.include_router(v1_router, dependencies=logged_in)
    app.include_router(history_router, dependencies=logged_in)
    app.include_router(inventory_router, dependencies=logged_in)
    app.include_router(actions_router)
    app.include_router(nodes_router)
    app.include_router(alerts_router)
    app.include_router(prometheus.make_router(settings.metrics_public))
    if not mount_spa(app, settings.static_dir):
        log.info("No frontend build at %s - serving API only", settings.static_dir)
    return app


app = create_app()
