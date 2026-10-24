"""Background polling of all configured clusters."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.collector.builder import CounterSample, build_snapshot
from app.collector.state import SnapshotStore
from app.config import ClusterConfig, Settings
from app.pve import PveApi
from app.tsdb.client import TsdbClient
from app.tsdb.metrics import snapshot_lines

log = logging.getLogger(__name__)


class ClusterPoller:
    """Polls one cluster and publishes snapshots into the store.

    * every ``poll_interval``: ``/cluster/resources`` + ``/cluster/status``
      (one call each covers all nodes, guests and storages)
    * every ``node_status_interval``: ``/nodes/{node}/status`` for online nodes
      (load average, IO delay, swap, kernel, versions)
    """

    def __init__(self, cfg: ClusterConfig, client: PveApi, store: SnapshotStore, settings: Settings,
                 clock: Any = time.time, tsdb: TsdbClient | None = None):
        self.cfg = cfg
        self.tsdb = tsdb
        self.client = client
        self.store = store
        self.settings = settings
        self.clock = clock
        self._counters: dict[int, CounterSample] = {}
        self._node_status: dict[str, dict[str, Any] | None] = {}
        self._node_status_at = 0.0
        store.register(cfg.name)

    async def poll_once(self) -> None:
        now = self.clock()
        try:
            resources, cluster_status = await asyncio.gather(
                self.client.get("/cluster/resources"),
                self.client.get("/cluster/status"),
            )
            if now - self._node_status_at >= self.settings.node_status_interval:
                await self._refresh_node_status(resources)
                self._node_status_at = now
            snap, self._counters = build_snapshot(
                self.cfg, resources or [], cluster_status or [], self._node_status,
                self._counters, now,
                api_url=getattr(self.client, "current_url", None),
            )
        except Exception as exc:  # noqa: BLE001 - any failure must keep the loop alive
            log.warning("Polling cluster '%s' failed: %s", self.cfg.name, exc)
            self._mark_unreachable(str(exc))
            return
        self.store.publish(snap)
        if self.tsdb:
            self.tsdb.enqueue(snapshot_lines(snap))

    async def _refresh_node_status(self, resources: list[dict[str, Any]]) -> None:
        online = [r["node"] for r in resources
                  if r.get("type") == "node" and r.get("status") == "online"]
        results = await asyncio.gather(
            *(self.client.get(f"/nodes/{n}/status") for n in online), return_exceptions=True
        )
        fresh: dict[str, dict[str, Any] | None] = {}
        for node, res in zip(online, results, strict=True):
            if isinstance(res, BaseException):
                log.info("Node status for %s/%s failed: %s", self.cfg.name, node, res)
                fresh[node] = self._node_status.get(node)  # keep last known values
            else:
                fresh[node] = res
        self._node_status = fresh

    def _mark_unreachable(self, error: str) -> None:
        """Keep the last data visible, but flag it as stale."""
        prev = self.store.get(self.cfg.name)
        snap = prev.model_copy(deep=True) if prev else None
        if snap is None:
            return
        snap.reachable = False
        snap.last_error = error
        # Rates would be computed over the outage gap - start fresh after recovery.
        self._counters = {}
        self.store.publish(snap)

    async def run(self) -> None:
        interval = self.settings.poll_interval
        # start() already did the first poll, so wait one interval before the next.
        while True:
            await asyncio.sleep(interval)
            started = time.monotonic()
            await self.poll_once()
            # Keep the cadence stable even if a poll was slow.
            interval = max(0.5, self.settings.poll_interval - (time.monotonic() - started))


class Collector:
    """Owns one poller task per cluster."""

    def __init__(self, clusters: list[ClusterConfig], clients: dict[str, PveApi],
                 store: SnapshotStore, settings: Settings, tsdb: TsdbClient | None = None):
        self.pollers = [ClusterPoller(c, clients[c.name], store, settings, tsdb=tsdb)
                        for c in clusters]
        self.clients = clients
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        # First poll synchronously so the API has data immediately after startup.
        await asyncio.gather(*(p.poll_once() for p in self.pollers))
        self._tasks = [asyncio.create_task(p.run(), name=f"poll-{p.cfg.name}")
                       for p in self.pollers]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*(c.aclose() for c in self.clients.values()))
