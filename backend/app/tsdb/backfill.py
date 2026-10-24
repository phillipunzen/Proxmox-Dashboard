"""Synthetic history for mock mode, so trends and analyses have data right away.

Resolution follows what the chart ranges query (see ``app.services.history.RANGES``):
30 days every 10 min, the last 24 h every 5 min and the last 2 h every 10 s.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from app.pve.mock import MockPveClient
from app.tsdb.client import TsdbClient
from app.tsdb.metrics import _labels, line, storage_lines

log = logging.getLogger(__name__)

SEGMENTS = ((30 * 86400, 600), (86400, 300), (7200, 10))  # (look-back seconds, step)
CHUNK_LINES = 50_000


def timestamps(now: float) -> list[float]:
    ts: set[float] = set()
    for back, step in SEGMENTS:
        start = (now - back) // step * step
        ts.update(float(t) for t in range(int(start), int(now) - 5, step))
    return sorted(ts)


def history_lines(mock: MockPveClient, t: float) -> Iterator[str]:
    """All series of one mock cluster at time ``t`` (mirrors ``metrics.snapshot_lines``)."""
    spec = mock.spec
    ts = int(t * 1000)
    cluster = spec.name
    cpus = mem = maxmem = 0
    cores_used = 0.0
    sums = {"netin": 0.0, "netout": 0.0, "diskread": 0.0, "diskwrite": 0.0}

    for n in spec.nodes:
        lbl = _labels(cluster=cluster, node=n.name)
        cpu = mock.node_cpu(n, t)
        used = mock.node_mem(n, t)
        cpus += n.cpus
        cores_used += cpu * n.cpus
        mem += used
        maxmem += n.mem
        yield line("pve_node_up", lbl, 1, ts)
        yield line("pve_node_cpu_ratio", lbl, cpu, ts)
        yield line("pve_node_cpus", lbl, n.cpus, ts)
        yield line("pve_node_cpu_cores_used", lbl, cpu * n.cpus, ts)
        yield line("pve_node_mem_used_bytes", lbl, used, ts)
        yield line("pve_node_mem_total_bytes", lbl, n.mem, ts)
        yield line("pve_node_iowait_ratio", lbl, mock.node_iowait(n, t), ts)
        yield line("pve_node_load1", lbl, cpu * n.cpus, ts)

    for g in spec.guests:
        if g.status != "running" or g.template:
            continue
        lbl = _labels(cluster=cluster, node=g.node, vmid=g.vmid, name=g.name, type=g.type)
        cpu = mock.guest_cpu(g, t)
        yield line("pve_guest_cpu_cores", lbl, cpu * g.cpus, ts)
        yield line("pve_guest_cpu_ratio", lbl, cpu, ts)
        yield line("pve_guest_mem_used_bytes", lbl, mock.guest_mem(g, t), ts)
        for key, rate, salt in (("diskread", g.io_rate * 0.6, "r"),
                                ("diskwrite", g.io_rate * 0.4, "w"),
                                ("netin", g.net_rate * 0.55, "i"),
                                ("netout", g.net_rate * 0.45, "o")):
            value = mock.guest_rate(g, rate, salt, t)
            sums[key] += value
            yield line(f"pve_guest_{key}_bps", lbl, value, ts)

    c = _labels(cluster=cluster)
    yield line("pve_cluster_cpu_ratio", c, cores_used / cpus if cpus else 0, ts)
    yield line("pve_cluster_cpus", c, cpus, ts)
    yield line("pve_cluster_mem_used_bytes", c, mem, ts)
    yield line("pve_cluster_mem_total_bytes", c, maxmem, ts)
    for key, value in sums.items():
        yield line(f"pve_cluster_{key}_bps", c, value, ts)

    rows = ((n.name, s.storage, s.type, s.shared, mock.storage_used(s, t), s.total)
            for n in spec.nodes for s in spec.storages)
    yield from storage_lines(cluster, rows, ts)


def generate(mocks: list[MockPveClient], now: float) -> Iterator[str]:
    """Chunks of newline-joined samples, ready for ``/api/v1/import/prometheus``."""
    buf: list[str] = []
    for t in timestamps(now):
        for mock in mocks:
            buf.extend(history_lines(mock, t))
        if len(buf) >= CHUNK_LINES:
            yield "\n".join(buf) + "\n"
            buf = []
    if buf:
        yield "\n".join(buf) + "\n"


async def backfill_if_empty(tsdb: TsdbClient, mocks: list[MockPveClient]) -> None:
    """Write 30 days of mock history unless the TSDB already has older mock data."""
    import asyncio

    try:
        existing = await tsdb.query("count_over_time(pve_cluster_cpus[30d])")
    except Exception as exc:  # noqa: BLE001
        log.warning("Mock backfill skipped, TSDB not ready: %s", exc)
        return
    if existing and max(s.points[0][1] for s in existing) > 1000:
        log.info("Mock history already present - no backfill")
        return

    started = time.monotonic()
    now = time.time()
    loop = asyncio.get_running_loop()
    chunks = generate(mocks, now)
    sent = 0
    while True:
        # Generating is CPU-bound - keep the event loop responsive.
        chunk = await loop.run_in_executor(None, next, chunks, None)
        if chunk is None:
            break
        await tsdb.write_text(chunk)
        sent += chunk.count("\n")
    log.info("Mock backfill: %d samples in %.1f s", sent, time.monotonic() - started)
