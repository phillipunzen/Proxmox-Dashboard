"""VictoriaMetrics client: buffered async writes and PromQL/MetricsQL queries."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TsdbError(Exception):
    pass


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]]  # (unix seconds, value)


class TsdbClient:
    """Thin wrapper around the VictoriaMetrics HTTP API.

    Writes go through a queue and a background task, so a slow or unavailable TSDB never
    delays polling. Failed batches are kept (bounded) and retried with the next flush.
    """

    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None,
                 max_pending_batches: int = 360):
        self.url = url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)
        self._queue: asyncio.Queue[list[str]] = asyncio.Queue()
        self._pending: deque[list[str]] = deque(maxlen=max_pending_batches)
        self._task: asyncio.Task[None] | None = None
        self.healthy: bool | None = None

    # -- writing ---------------------------------------------------------------------------------

    def enqueue(self, lines: list[str]) -> None:
        if lines:
            self._queue.put_nowait(lines)

    def start(self) -> None:
        self._task = asyncio.create_task(self._writer(), name="tsdb-writer")

    async def _writer(self) -> None:
        while True:
            batch = await self._queue.get()
            self._pending.append(batch)
            # Merge whatever else is waiting into one request.
            while not self._queue.empty():
                self._pending.append(self._queue.get_nowait())
            await self.flush()

    async def flush(self) -> bool:
        if not self._pending:
            return True
        body = "\n".join(line for batch in self._pending for line in batch) + "\n"
        try:
            await self.write_text(body)
        except (httpx.HTTPError, TsdbError) as exc:
            if self.healthy is not False:
                log.warning("TSDB write failed (%d batches kept for retry): %s",
                            len(self._pending), exc)
            self.healthy = False
            return False
        if self.healthy is False:
            log.info("TSDB writes recovered")
        self.healthy = True
        self._pending.clear()
        return True

    async def write_text(self, body: str) -> None:
        resp = await self._http.post(f"{self.url}/api/v1/import/prometheus", content=body)
        if resp.status_code >= 300:
            raise TsdbError(f"import returned {resp.status_code}: {resp.text[:200]}")

    # -- querying --------------------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._http.get(f"{self.url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise TsdbError(f"TSDB not reachable: {exc}") from exc
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") \
            else {}
        if resp.status_code != 200 or body.get("status") != "success":
            raise TsdbError(body.get("error") or f"HTTP {resp.status_code}: {resp.text[:200]}")
        return body["data"]

    async def query_range(self, query: str, start: float, end: float, step: int) -> list[Series]:
        data = await self._get("/api/v1/query_range",
                               {"query": query, "start": start, "end": end, "step": f"{step}s"})
        return [Series(r["metric"], [(float(t), float(v)) for t, v in r["values"]])
                for r in data["result"]]

    async def query(self, query: str, at: float | None = None) -> list[Series]:
        params: dict[str, Any] = {"query": query}
        if at is not None:
            params["time"] = at
        data = await self._get("/api/v1/query", params)
        return [Series(r["metric"], [(float(r["value"][0]), float(r["value"][1]))])
                for r in data["result"]]

    async def ping(self) -> bool:
        try:
            resp = await self._http.get(f"{self.url}/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        # Best effort on shutdown, but never block it for long.
        await asyncio.gather(asyncio.wait_for(self.flush(), 5), return_exceptions=True)
        await self._http.aclose()
