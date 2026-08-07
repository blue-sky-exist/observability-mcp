from __future__ import annotations

import pytest
from conftest import load_profiles, make_settings

from observability_mcp.errors import ToolError
from observability_mcp.log_search import (
    build_search_body,
    normalize_hit,
    resolve_index,
)


def test_profile_selection() -> None:
    profiles = load_profiles()
    assert profiles.select("filebeat-2026.08.04").name == "ecs"
    assert profiles.select("filebeat-production-*").name == "ecs"
    assert profiles.select("applog-service-0001").name == "offset-log"
    assert profiles.select("logs-custom-0001").name == "generic"
    assert profiles.select("logs-*").name == "generic"
    assert profiles.select("*").name == "generic"


def test_index_allowlist_blocks_broader_wildcard() -> None:
    settings = make_settings()
    assert resolve_index(None, settings) == "logs-*"
    assert resolve_index("logs-service-*", settings) == "logs-service-*"
    assert resolve_index("logs-a-*,filebeat-b-*", settings) == "logs-a-*,filebeat-b-*"
    with pytest.raises(ToolError, match="outside ES_ALLOWED_INDEX_PATTERNS"):
        resolve_index("*", settings)
    with pytest.raises(ToolError, match="outside ES_ALLOWED_INDEX_PATTERNS"):
        resolve_index("private-*", settings)


def test_build_search_body_uses_profile_fields_and_stable_sort() -> None:
    profile = load_profiles().get("offset-log")
    body = build_search_body(
        {
            "trace_id": "trace-123",
            "keyword": "timeout",
            "level": "warn",
            "start_time": "2026-08-04T00:00:00Z",
            "limit": 20,
        },
        profile,
        200,
    )
    assert body["sort"] == [
        {"offset": {"order": "asc", "unmapped_type": "long"}},
        {"_shard_doc": {"order": "asc"}},
    ]
    filters = body["query"]["bool"]["filter"]
    assert filters[0]["bool"]["should"] == [
        {"term": {"traceId.keyword": "trace-123"}}
    ]
    assert {"match_phrase": {"message": "timeout"}} in filters[1]["bool"]["should"]
    level_clauses = filters[2]["bool"]["should"]
    assert level_clauses == [
        {"term": {"log_level.keyword": {"value": "WARN", "case_insensitive": True}}}
    ]
    assert any("range" in clause for clause in filters[3]["bool"]["should"])
    assert "service.name" in body["_source"]
    assert "beat.hostname" in body["_source"]
    assert "error_stack_trace" in body["_source"]
    assert "log_logger" in body["_source"]
    assert "apiKey" not in body["_source"]
    assert "event.original" not in body["_source"]


def test_generic_profile_falls_back_to_message_for_trace_and_level() -> None:
    profile = load_profiles().get("generic")
    body = build_search_body(
        {"trace_id": "trace-123", "level": "ERROR", "limit": 5},
        profile,
        200,
    )

    filters = body["query"]["bool"]["filter"]
    trace_clauses = filters[0]["bool"]["should"]
    level_clauses = filters[1]["bool"]["should"]
    assert body["sort"] == [
        {"offset": {"order": "asc", "unmapped_type": "long"}},
        {"_shard_doc": {"order": "asc"}},
    ]
    assert trace_clauses == [
        {"match_phrase": {field: "trace-123"}}
        for field in profile.fields.message
    ]
    assert level_clauses == [
        {"match_phrase": {field: "ERROR"}}
        for field in profile.fields.message
    ]


def test_node_ip_uses_configured_machine_fields() -> None:
    profile = load_profiles().get("offset-log")
    body = build_search_body(
        {"node_ip": "zp-llm-app-12-193", "limit": 5},
        profile,
        200,
    )

    assert body["query"]["bool"]["filter"] == [{
        "bool": {
            "should": [
                {"term": {field: "zp-llm-app-12-193"}}
                for field in profile.fields.node_ip
            ],
            "minimum_should_match": 1,
        }
    }]


def test_normalize_hit_supports_nested_and_flattened_fields() -> None:
    profile = load_profiles().get("ecs")
    nested = normalize_hit(
        {
            "_source": {
                "@timestamp": "2026-08-04T00:00:00Z",
                "message": "hello",
                "service": {"name": "orders"},
                "host": {"ip": ["192.0.2.10"], "name": "node-a"},
                "trace": {"id": "abc"},
                "log": {"level": "INFO"},
            }
        },
        profile,
    )
    assert nested == {
        "timestamp": "2026-08-04T00:00:00Z",
        "message": "hello",
        "project": "orders",
        "node_ip": "192.0.2.10",
        "trace_id": "abc",
        "level": "INFO",
        "error": None,
        "attributes": {},
    }
    flattened = normalize_hit(
        {"_source": {"@timestamp": "t", "log.original": "raw", "service.name": "svc"}},
        profile,
    )
    assert flattened["message"] == "raw"
    assert flattened["project"] == "svc"


def test_normalize_hit_returns_safe_error_details_and_attributes() -> None:
    profile = load_profiles().get("offset-log")
    stack_trace = "ExampleError: head\n" + "x" * 120 + "\nCaused by: root cause"
    result = normalize_hit(
        {
            "_source": {
                "@timestamp": "2026-08-05T07:18:27.707Z",
                "message": "request failed",
                "project": "example-service",
                "log_level": "ERROR",
                "error_type": "ExampleError",
                "error_message": "request failed",
                "error_stack_trace": stack_trace,
                "spanId": "span-123",
                "log_logger": "example.Service",
                "process_thread_name": "worker-1",
                "source": "/var/log/example.log",
                "apiKey": "must-not-be-returned",
                "event": {"original": "must-not-be-returned"},
            }
        },
        profile,
        max_stack_trace_length=100,
    )

    assert result["error"]["type"] == "ExampleError"
    assert result["error"]["message"] == "request failed"
    assert result["error"]["stack_trace_truncated"] is True
    assert result["error"]["stack_trace"].startswith("ExampleError: head")
    assert result["error"]["stack_trace"].endswith("Caused by: root cause")
    assert len(result["error"]["stack_trace"]) == 100
    assert result["attributes"] == {
        "span_id": "span-123",
        "logger": "example.Service",
        "thread": "worker-1",
        "log_source": "/var/log/example.log",
    }
    assert "apiKey" not in result["attributes"]


def test_normalize_hit_falls_back_to_legacy_beat_hostname() -> None:
    profile = load_profiles().get("offset-log")
    result = normalize_hit(
        {
            "_source": {
                "@timestamp": "2026-08-05T06:37:46.628Z",
                "message": "hello",
                "project": "legacy-service",
                "beat": {
                    "hostname": "node-a",
                    "name": "node-a",
                },
            }
        },
        profile,
    )
    assert result["node_ip"] == "node-a"
