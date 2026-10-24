"""In-memory store for the latest snapshot of every cluster.

The collector writes, API handlers and SSE streams read. All viewers share the same
data, so the load on Proxmox does not depend on the number of open dashboards.
"""

from __future__ import annotations

import asyncio

from app.collector.models import ClusterSnapshot


class SnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, ClusterSnapshot] = {}
        self._version = 0
        self._changed = asyncio.Event()

    @property
    def version(self) -> int:
        return self._version

    def register(self, name: str) -> None:
        """Create an empty placeholder so a cluster shows up before its first poll."""
        self._snapshots.setdefault(name, ClusterSnapshot(name=name))

    def publish(self, snap: ClusterSnapshot) -> None:
        self._snapshots[snap.name] = snap
        self._version += 1
        # Wake every waiter, then arm a fresh event for the next change.
        self._changed.set()
        self._changed = asyncio.Event()

    def get(self, name: str) -> ClusterSnapshot | None:
        return self._snapshots.get(name)

    def all(self) -> list[ClusterSnapshot]:
        return list(self._snapshots.values())

    async def wait_for_change(self, since: int, timeout: float) -> bool:
        """Wait until the version differs from ``since``. Returns False on timeout."""
        if self._version != since:
            return True
        event = self._changed
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return True
