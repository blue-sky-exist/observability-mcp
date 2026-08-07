from __future__ import annotations

import json

import httpx
import pytest
from conftest import make_settings

from observability_mcp.config import BackendAuth
from observability_mcp.errors import ToolError
from observability_mcp.skywalking_client import SkyWalkingClient
from observability_mcp.skywalking_logs import SkyWalkingLogService


@pytest.mark.asyncio
async def test_list_services_uses_graphql_auth_and_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        assert request.headers["Authorization"] == "Bearer sw-token"
        payload = json.loads(request.content)
        assert "searchServices" in payload["query"]
        assert payload["variables"]["keyword"] == "robo"
        return httpx.Response(200, json={
            "data": {
                "searchServices": [
                    {"id": "svc-b", "name": "robo-write"},
                    {"id": "svc-a", "name": "robo-api"},
                ]
            }
        })

    settings = make_settings(
        skywalking_auth=BackendAuth("bearer", token="sw-token")
    )
    client = SkyWalkingClient(settings, httpx.MockTransport(handler))
    service = SkyWalkingLogService(settings, client)
    try:
        result = await service.list_services(keyword="robo", limit=1)
    finally:
        await client.close()

    assert result["matched"] == 2
    assert result["truncated"] is True
    assert result["services"] == [
        {"service_name": "robo-api", "service_id": "svc-a"}
    ]


@pytest.mark.asyncio
async def test_search_logs_normalizes_instance_and_redacts_content() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        if "searchServices" in query:
            calls.append("services")
            return httpx.Response(200, json={
                "data": {
                    "searchServices": [
                        {"id": "svc-1", "name": "orders-api"}
                    ]
                }
            })
        if "queryLogs" in query:
            calls.append("logs")
            condition = payload["variables"]["condition"]
            assert condition["serviceId"] == "svc-1"
            assert condition["paging"] == {"pageNum": 1, "pageSize": 50}
            assert condition["keywordsOfContent"] == ["OrderService"]
            return httpx.Response(200, json={
                "data": {
                    "queryLogs": {
                        "logs": [{
                            "serviceName": "orders-api",
                            "serviceId": "svc-1",
                            "serviceInstanceName": "node-a",
                            "serviceInstanceId": "instance-1",
                            "endpointName": "/report",
                            "traceId": "trace-123",
                            "timestamp": 1785911866628,
                            "contentType": "TEXT",
                            "content": "INFO password=secret OrderService",
                            "tags": [],
                        }]
                    }
                }
            })
        return httpx.Response(500, text="unexpected query")

    settings = make_settings()
    client = SkyWalkingClient(settings, httpx.MockTransport(handler))
    service = SkyWalkingLogService(settings, client)
    try:
        result = await service.search_logs(
            service_name="orders-api",
            keyword="OrderService",
        )
    finally:
        await client.close()

    assert calls == ["services", "logs"]
    assert result["source"] == "skywalking"
    assert result["logs"][0] == {
        "timestamp": "2026-08-05T14:37:46.628000+08:00",
        "message": "INFO password=*** OrderService",
        "project": "orders-api",
        "node_ip": "node-a",
        "trace_id": "trace-123",
        "level": "INFO",
        "endpoint": "/report",
        "service_instance_id": "instance-1",
    }


@pytest.mark.asyncio
async def test_get_trace_normalizes_spans() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["variables"] == {"traceId": "trace-123"}
        return httpx.Response(200, json={
            "data": {
                "queryTrace": {
                    "spans": [{
                        "traceId": "trace-123",
                        "segmentId": "segment-1",
                        "spanId": 0,
                        "parentSpanId": -1,
                        "refs": [],
                        "serviceCode": "orders",
                        "serviceInstanceName": "10.0.0.8",
                        "startTime": 1785911866000,
                        "endTime": 1785911866125,
                        "endpointName": "/orders",
                        "type": "Entry",
                        "peer": "",
                        "component": "SpringMVC",
                        "isError": True,
                        "layer": "HTTP",
                        "tags": [],
                        "logs": [],
                    }]
                }
            }
        })

    settings = make_settings()
    client = SkyWalkingClient(settings, httpx.MockTransport(handler))
    service = SkyWalkingLogService(settings, client)
    try:
        result = await service.get_trace("trace-123")
    finally:
        await client.close()

    assert result["span_count"] == 1
    assert result["error_span_count"] == 1
    assert result["spans"][0]["duration_ms"] == 125
    assert result["spans"][0]["node_ip"] == "10.0.0.8"


@pytest.mark.asyncio
async def test_skywalking_requires_graphql_url() -> None:
    settings = make_settings(skywalking_graphql_url="")
    client = SkyWalkingClient(settings, httpx.MockTransport(lambda _: httpx.Response(500)))
    service = SkyWalkingLogService(settings, client)
    try:
        with pytest.raises(ToolError, match="SKYWALKING_GRAPHQL_URL"):
            await service.list_services()
    finally:
        await client.close()
