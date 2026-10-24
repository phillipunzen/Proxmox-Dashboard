import httpx
import pytest
import respx

from app.collector.builder import build_snapshot
from app.config import ClusterConfig
from app.services.history import resolve_window, selector
from app.tsdb.backfill import history_lines, timestamps
from app.tsdb.client import TsdbClient, TsdbError
from app.tsdb.metrics import snapshot_lines
from tests.test_builder import NODE_STATUS, STATUS, _resources

VM = "http://vm:8428"


def _snapshot(now=1000.0, prev=None):
    cfg = ClusterConfig(name="c1", urls=["https://pve1:8006"])
    return build_snapshot(cfg, _resources(), STATUS, NODE_STATUS, prev or {}, now=now)


def test_snapshot_lines_cover_cluster_nodes_guests_storage():
    snap, _ = _snapshot()
    lines = snapshot_lines(snap)
    text = "\n".join(lines)
    assert 'pve_cluster_cpus{cluster="c1"} 8 1000000' in text
    assert 'pve_node_up{cluster="c1",node="n2"} 0' in text  # offline node: only "up"
    assert 'pve_node_cpu_ratio{cluster="c1",node="n2"}' not in text
    # Running guest: 50 % of 4 vCPUs = 2 host threads
    assert ('pve_guest_cpu_cores{cluster="c1",node="n1",vmid="100",name="web",type="qemu"} 2 '
            in text)
    assert 'vmid="200"' not in text  # stopped guest is not written
    assert "pve_guest_netin_bps" not in text  # no rate on first sample
    # Shared storage once with node="shared", local per node
    assert text.count('storage="nfs"') == 2  # used + total
    assert 'node="shared",storage="nfs"' in text


def test_label_values_are_escaped():
    snap, _ = _snapshot()
    snap.guests[0].name = 'we"ird\\name'
    assert 'name="we\\"ird\\\\name"' in "\n".join(snapshot_lines(snap))


def test_unreachable_snapshot_writes_nothing():
    snap, _ = _snapshot()
    snap.reachable = False
    assert snapshot_lines(snap) == []


def test_large_values_keep_precision():
    snap, _ = _snapshot()
    snap.storages[0].total = 107374182400
    assert "107374182400" in "\n".join(snapshot_lines(snap))


def test_window_presets_and_alignment():
    w = resolve_window("24h", end=1_000_123.0)
    assert w.step == 300 and w.end % 300 == 0 and w.end - w.start == 86400
    assert w.rollup == "300s"
    # 10 s step never uses a window below 20 s (would hit empty buckets)
    assert resolve_window("1h", end=1000.0).rollup == "20s"
    # explicit range -> ~720 points
    w = resolve_window(start=0.0, end=72000.0)
    assert w.step == 100


def test_selector_builds_and_escapes():
    assert selector("m", cluster="a", node=None, vmid__re="1|2") == 'm{cluster="a",vmid=~"1|2"}'
    assert selector("m", cluster='x"y') == 'm{cluster="x\\"y"}'


def test_backfill_timestamps_cover_30_days_dense_at_the_end():
    now = 10_000_000.0
    ts = timestamps(now)
    assert ts[0] <= now - 30 * 86400 + 600
    assert ts == sorted(set(ts))
    last_2h = [t for t in ts if t > now - 7200]
    assert len(last_2h) >= 700  # 10 s resolution


def test_backfill_lines_parseable():
    from app.pve.mock import MockPveClient

    mock = MockPveClient("standalone", clock=lambda: 5_000_000.0)
    lines = list(history_lines(mock, 4_999_000.0))
    assert any(line.startswith("pve_guest_cpu_cores{") for line in lines)
    for line in lines:
        name_labels, value, ts = line.rsplit(" ", 2)
        float(value)
        assert ts == "4999000000"


@respx.mock
async def test_write_retries_failed_batches():
    route = respx.post(f"{VM}/api/v1/import/prometheus")
    route.side_effect = [httpx.ConnectError("down"), httpx.Response(204)]
    client = TsdbClient(VM)
    client._pending.append(["a 1 1"])
    assert await client.flush() is False and client.healthy is False
    client._pending.append(["b 2 2"])
    assert await client.flush() is True
    assert route.calls.last.request.content == b"a 1 1\nb 2 2\n"
    assert not client._pending
    await client.aclose()


@respx.mock
async def test_query_range_parses_matrix_and_errors():
    respx.get(f"{VM}/api/v1/query_range").mock(return_value=httpx.Response(200, json={
        "status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"vmid": "100"}, "values": [[1, "0.5"], [2, "1.5"]]}]}}))
    respx.get(f"{VM}/api/v1/query").mock(return_value=httpx.Response(422, json={
        "status": "error", "error": "bad query"}))
    client = TsdbClient(VM)
    series = await client.query_range("x", 0, 10, 1)
    assert series[0].labels == {"vmid": "100"} and series[0].points == [(1.0, 0.5), (2.0, 1.5)]
    with pytest.raises(TsdbError, match="bad query"):
        await client.query("y")
    await client.aclose()
