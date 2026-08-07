from __future__ import annotations

import httpx
import pytest
from conftest import make_settings

from observability_mcp.config import BackendAuth
from observability_mcp.metrics_client import MetricsClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url,expected_path",
    [
        ("http://prometheus:9090/api/v1", "/api/v1/query"),
        ("http://vm:8428/api/v1", "/api/v1/query"),
        ("http://vmselect:8481/select/7/prometheus/api/v1", "/select/7/prometheus/api/v1/query"),
    ],
)
async def test_configurable_metrics_api_base_url(base_url: str, expected_path: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        return httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        })

    client = MetricsClient(
        make_settings(metrics_api_base_url=base_url), httpx.MockTransport(handler)
    )
    try:
        result = await client.query_instant("up")
    finally:
        await client.close()
    assert result["result_type"] == "vector"


@pytest.mark.asyncio
async def test_bearer_auth_and_point_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-value"
        return httpx.Response(200, json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"instance": "a"}, "values": [[1, "1"], [2, "1"]]},
                    {"metric": {"instance": "b"}, "values": [[1, "1"], [2, "1"]]},
                ],
            },
        })

    settings = make_settings(
        metrics_auth=BackendAuth("bearer", token="token-value"), metrics_max_points=3
    )
    client = MetricsClient(settings, httpx.MockTransport(handler))
    try:
        result = await client.query_range("up", 0, 120, 60)
    finally:
        await client.close()
    assert result["points_returned"] == 3
    assert result["truncated"] is True
