from __future__ import annotations

import json

import httpx
import pytest
from conftest import load_profiles, make_settings

from observability_mcp.cursors import MemoryCursorStore
from observability_mcp.errors import ToolError
from observability_mcp.es_client import ElasticsearchClient
from observability_mcp.log_search import LogSearchService


@pytest.mark.asyncio
async def test_pagination_normalizes_logs_and_closes_pit() -> None:
    huge_pit = "P" * 100_000
    search_calls = 0
    pit_closed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls, pit_closed
        if request.method == "POST" and request.url.path.endswith("/_pit"):
            assert request.content == b""
            return httpx.Response(200, json={"id": huge_pit})
        if request.method == "POST" and request.url.path == "/_search":
            search_calls += 1
            body = json.loads(request.content)
            if search_calls == 1:
                assert "search_after" not in body
                hits = [
                    {
                        "sort": ["2026-08-04T00:00:00Z", 1],
                        "_source": {
                            "@timestamp": "2026-08-04T00:00:00Z",
                            "message": "first",
                            "service": {"name": "demo"},
                        },
                    },
                    {
                        "sort": ["2026-08-04T00:00:00Z", 2],
                        "_source": {
                            "@timestamp": "2026-08-04T00:00:00Z",
                            "message": "second",
                            "service": {"name": "demo"},
                        },
                    },
                ]
            else:
                assert body["search_after"] == ["2026-08-04T00:00:00Z", 2]
                hits = [{
                    "sort": ["2026-08-04T00:00:01Z", 3],
                    "_source": {"@timestamp": "2026-08-04T00:00:01Z", "message": "third"},
                }]
            return httpx.Response(200, json={
                "pit_id": huge_pit,
                "took": 3,
                "hits": {"total": {"value": 3, "relation": "eq"}, "hits": hits},
            })
        if request.method == "DELETE" and request.url.path == "/_pit":
            pit_closed = True
            return httpx.Response(200, json={"succeeded": True, "num_freed": 1})
        return httpx.Response(500, text="unexpected request")

    settings = make_settings()
    es = ElasticsearchClient(settings, httpx.MockTransport(handler))
    service = LogSearchService(settings, es, MemoryCursorStore(120), load_profiles())
    try:
        first = await service.search(keyword="hello", index="logs-demo-*", limit=2)
        assert first["profile"] == "generic"
        assert first["index_expression"] == "logs-demo-*"
        assert first["has_more"] is True
        assert first["logs"][0]["project"] == "demo"
        assert len(first["next_cursor"]) < 64
        assert huge_pit not in first["next_cursor"]

        second = await service.search(cursor=first["next_cursor"])
        assert second["index_expression"] == "logs-demo-*"
        assert second["count"] == 1
        assert second["has_more"] is False
        assert pit_closed is True
        with pytest.raises(ToolError, match="does not exist or has expired"):
            await service.search(cursor=first["next_cursor"])
    finally:
        await es.close()


@pytest.mark.asyncio
async def test_cursor_rejects_repeated_arguments() -> None:
    settings = make_settings()
    es = ElasticsearchClient(settings, httpx.MockTransport(lambda _: httpx.Response(500)))
    service = LogSearchService(settings, es, MemoryCursorStore(120), load_profiles())
    try:
        with pytest.raises(ToolError, match="do not repeat"):
            await service.search(cursor="lc_token", keyword="x")
    finally:
        await es.close()


@pytest.mark.asyncio
async def test_list_indices_uses_allowlist_and_limits_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/_resolve/index/logs-*,filebeat-*,applog-*"
        assert request.url.params["expand_wildcards"] == "all"
        return httpx.Response(200, json={
            "indices": [
                {"name": "logs-orders-2026.08.04"},
                {"name": ".security-7", "attributes": ["open", "system"]},
                {"name": "logs-internal-system", "attributes": ["open", "system"]},
                {"name": "private-secret"},
            ],
            "aliases": [{"name": "logs-orders"}],
            "data_streams": [{"name": "filebeat-production"}],
        })

    settings = make_settings()
    es = ElasticsearchClient(settings, httpx.MockTransport(handler))
    service = LogSearchService(settings, es, MemoryCursorStore(120), load_profiles())
    try:
        result = await service.list_indices(limit=2)
    finally:
        await es.close()

    assert result["matched"] == 3
    assert result["count"] == 2
    assert result["truncated"] is True
    assert result["system_indices_excluded"] == 1
    assert result["targets"] == [
        {"name": "filebeat-production", "type": "data_stream"},
        {"name": "logs-orders", "type": "alias"},
    ]


@pytest.mark.asyncio
async def test_list_indices_can_include_system_indices_explicitly() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "indices": [
                {"name": ".security-7", "attributes": ["open", "system"]},
                {"name": "logs-orders-2026.08.04"},
            ]
        })

    settings = make_settings(es_allowed_index_patterns=("*",))
    es = ElasticsearchClient(settings, httpx.MockTransport(handler))
    service = LogSearchService(settings, es, MemoryCursorStore(120), load_profiles())
    try:
        result = await service.list_indices(include_system=True)
    finally:
        await es.close()

    assert result["system_indices_excluded"] == 0
    assert result["targets"][0] == {"name": ".security-7", "type": "index"}


@pytest.mark.asyncio
async def test_list_indices_can_return_only_aliases() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "indices": [{"name": "logs-orders-2026.08.04"}],
            "aliases": [
                {"name": "logs-orders"},
                {"name": "logs-payments"},
            ],
            "data_streams": [{"name": "filebeat-production"}],
        })

    settings = make_settings()
    es = ElasticsearchClient(settings, httpx.MockTransport(handler))
    service = LogSearchService(settings, es, MemoryCursorStore(120), load_profiles())
    try:
        result = await service.list_indices(target_type="alias")
    finally:
        await es.close()

    assert result["target_type"] == "alias"
    assert result["matched"] == 2
    assert result["targets"] == [
        {"name": "logs-orders", "type": "alias"},
        {"name": "logs-payments", "type": "alias"},
    ]


@pytest.mark.asyncio
async def test_list_indices_rejects_disallowed_expression() -> None:
    settings = make_settings()
    es = ElasticsearchClient(settings, httpx.MockTransport(lambda _: httpx.Response(500)))
    service = LogSearchService(settings, es, MemoryCursorStore(120), load_profiles())
    try:
        with pytest.raises(ToolError, match="outside ES_ALLOWED_INDEX_PATTERNS"):
            await service.list_indices(index="private-*")
    finally:
        await es.close()
