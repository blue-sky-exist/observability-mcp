from __future__ import annotations

from pathlib import Path

from observability_mcp.config import BackendAuth, Settings
from observability_mcp.profiles import LogProfileRegistry

ROOT = Path(__file__).resolve().parents[1]


def make_settings(**overrides) -> Settings:
    values = {
        "es_urls": ("http://es-a:9200", "http://es-b:9200"),
        "es_auth": BackendAuth("basic", username="user", password="secret"),
        "es_timeout": 3.0,
        "es_verify_tls": True,
        "es_ca_file": "",
        "es_pit_keep_alive": "2m",
        "es_default_index": "logs-*",
        "es_allowed_index_patterns": ("logs-*", "filebeat-*", "applog-*"),
        "es_max_limit": 200,
        "log_max_stack_trace_length": 12000,
        "cursor_ttl_seconds": 120,
        "log_profiles_file": str(ROOT / "config" / "log-profiles.yaml"),
        "metrics_api_base_url": "http://prometheus:9090/api/v1",
        "metrics_auth": BackendAuth("none"),
        "metrics_timeout": 3.0,
        "metrics_verify_tls": True,
        "metrics_ca_file": "",
        "metrics_max_series": 1000,
        "metrics_max_points": 10000,
        "metrics_max_range_seconds": 604800,
        "skywalking_graphql_url": "http://skywalking:12800/graphql",
        "skywalking_auth": BackendAuth("none"),
        "skywalking_timeout": 3.0,
        "skywalking_verify_tls": True,
        "skywalking_ca_file": "",
        "skywalking_timezone": "Asia/Shanghai",
        "skywalking_max_range_seconds": 86400,
        "skywalking_max_page_size": 100,
        "skywalking_service_cache_ttl_seconds": 600,
        "skywalking_service_cache_max_size": 500,
        "mcp_host": "127.0.0.1",
        "mcp_port": 8000,
    }
    values.update(overrides)
    return Settings(**values)


def load_profiles() -> LogProfileRegistry:
    return LogProfileRegistry.from_file(str(ROOT / "config" / "log-profiles.yaml"))
