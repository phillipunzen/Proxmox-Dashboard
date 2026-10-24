import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import ClusterConfig
from app.pve.client import PveClient, PveError, PveUnavailable


def make_cfg(urls: list[str]) -> ClusterConfig:
    return ClusterConfig(name="t", urls=urls, token_id="dash@pve!ro",
                         token_secret=SecretStr("secret-uuid"))


@respx.mock
async def test_token_header_and_data_unwrapped():
    route = respx.get("https://a:8006/api2/json/version").mock(
        return_value=httpx.Response(200, json={"data": {"version": "9.0.10"}})
    )
    client = PveClient(make_cfg(["https://a:8006"]))
    assert await client.get("/version") == {"version": "9.0.10"}
    assert route.calls.last.request.headers["Authorization"] == (
        "PVEAPIToken=dash@pve!ro=secret-uuid"
    )
    await client.aclose()


@respx.mock
async def test_failover_to_second_url_and_remember_it():
    a = respx.get("https://a:8006/api2/json/cluster/status").mock(
        side_effect=httpx.ConnectError("refused")
    )
    b = respx.get("https://b:8006/api2/json/cluster/status").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = PveClient(make_cfg(["https://a:8006", "https://b:8006"]))
    assert await client.get("/cluster/status") == []
    assert client.current_url == "https://b:8006"
    # The next request goes straight to b.
    await client.get("/cluster/status")
    assert a.call_count == 1 and b.call_count == 2
    await client.aclose()


@respx.mock
async def test_5xx_fails_over_but_4xx_raises_immediately():
    respx.get("https://a:8006/api2/json/x").mock(return_value=httpx.Response(503))
    respx.get("https://b:8006/api2/json/x").mock(return_value=httpx.Response(200, json={"data": 1}))
    respx.get("https://a:8006/api2/json/forbidden").mock(
        return_value=httpx.Response(403, json={"data": None})
    )
    b_forbidden = respx.get("https://b:8006/api2/json/forbidden")
    client = PveClient(make_cfg(["https://a:8006", "https://b:8006"]))
    assert await client.get("/x") == 1
    client._preferred = 0
    with pytest.raises(PveError) as exc:
        await client.get("/forbidden")
    assert exc.value.status == 403
    assert b_forbidden.call_count == 0  # permission errors are not retried elsewhere
    await client.aclose()


@respx.mock
async def test_all_urls_down():
    respx.get(url__regex=r"https://.*").mock(side_effect=httpx.ConnectTimeout("t"))
    client = PveClient(make_cfg(["https://a:8006", "https://b:8006"]))
    with pytest.raises(PveUnavailable):
        await client.get("/version")
    await client.aclose()


def test_missing_token_rejected():
    with pytest.raises(ValueError, match="token"):
        PveClient(ClusterConfig(name="t", urls=["https://a:8006"]))
