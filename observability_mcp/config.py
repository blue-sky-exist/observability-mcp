from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AuthType = Literal["none", "basic", "bearer", "api-key"]


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _csv(name: str, default: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in os.getenv(name, default).split(",") if part.strip())
    if not values:
        raise ValueError(f"{name} must contain at least one value")
    return values


def _auth_type(name: str, default: AuthType) -> AuthType:
    value = os.getenv(name, default).strip().lower()
    if value not in {"none", "basic", "bearer", "api-key"}:
        raise ValueError(f"{name} must be none, basic, bearer, or api-key")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class BackendAuth:
    type: AuthType
    username: str = ""
    password: str = ""
    token: str = ""
    api_key: str = ""

    def validate(self, prefix: str) -> None:
        if self.type == "basic" and not (self.username and self.password):
            raise ValueError(f"{prefix}_USERNAME and {prefix}_PASSWORD are required for basic auth")
        if self.type == "bearer" and not self.token:
            raise ValueError(f"{prefix}_BEARER_TOKEN is required for bearer auth")
        if self.type == "api-key" and not self.api_key:
            raise ValueError(f"{prefix}_API_KEY is required for api-key auth")


@dataclass(frozen=True)
class Settings:
    es_urls: tuple[str, ...]
    es_auth: BackendAuth
    es_timeout: float
    es_verify_tls: bool
    es_ca_file: str
    es_pit_keep_alive: str
    es_default_index: str
    es_allowed_index_patterns: tuple[str, ...]
    es_max_limit: int
    log_max_stack_trace_length: int
    cursor_ttl_seconds: int
    log_profiles_file: str

    metrics_api_base_url: str
    metrics_auth: BackendAuth
    metrics_timeout: float
    metrics_verify_tls: bool
    metrics_ca_file: str
    metrics_max_series: int
    metrics_max_points: int
    metrics_max_range_seconds: int

    skywalking_graphql_url: str
    skywalking_auth: BackendAuth
    skywalking_timeout: float
    skywalking_verify_tls: bool
    skywalking_ca_file: str
    skywalking_timezone: str
    skywalking_max_range_seconds: int
    skywalking_max_page_size: int
    skywalking_service_cache_ttl_seconds: int
    skywalking_service_cache_max_size: int

    mcp_host: str
    mcp_port: int

    @classmethod
    def from_env(cls) -> Settings:
        es_auth = BackendAuth(
            type=_auth_type("ES_AUTH_TYPE", "none"),
            username=os.getenv("ES_USERNAME", "").strip(),
            password=os.getenv("ES_PASSWORD", ""),
            token=os.getenv("ES_BEARER_TOKEN", ""),
            api_key=os.getenv("ES_API_KEY", ""),
        )
        metrics_auth = BackendAuth(
            type=_auth_type("METRICS_AUTH_TYPE", "none"),
            username=os.getenv("METRICS_USERNAME", "").strip(),
            password=os.getenv("METRICS_PASSWORD", ""),
            token=os.getenv("METRICS_BEARER_TOKEN", ""),
            api_key=os.getenv("METRICS_API_KEY", ""),
        )
        skywalking_auth = BackendAuth(
            type=_auth_type("SKYWALKING_AUTH_TYPE", "none"),
            username=os.getenv("SKYWALKING_USERNAME", "").strip(),
            password=os.getenv("SKYWALKING_PASSWORD", ""),
            token=os.getenv("SKYWALKING_BEARER_TOKEN", ""),
            api_key=os.getenv("SKYWALKING_API_KEY", ""),
        )
        es_auth.validate("ES")
        metrics_auth.validate("METRICS")
        skywalking_auth.validate("SKYWALKING")

        max_limit = _positive_int("ES_MAX_LIMIT", 200)
        if max_limit > 1000:
            raise ValueError("ES_MAX_LIMIT cannot exceed the public hard limit of 1000")
        skywalking_max_page_size = _positive_int("SKYWALKING_MAX_PAGE_SIZE", 100)
        if skywalking_max_page_size > 1000:
            raise ValueError("SKYWALKING_MAX_PAGE_SIZE cannot exceed 1000")

        bundled_profiles = str(Path(__file__).with_name("default-log-profiles.yaml"))
        profiles_file = os.getenv("LOG_PROFILES_FILE", bundled_profiles).strip()
        if not profiles_file:
            raise ValueError("LOG_PROFILES_FILE cannot be empty")

        return cls(
            es_urls=tuple(url.rstrip("/") for url in _csv("ES_URLS", "http://localhost:9200")),
            es_auth=es_auth,
            es_timeout=_positive_float("ES_TIMEOUT_SECONDS", 30),
            es_verify_tls=_boolean("ES_VERIFY_TLS", True),
            es_ca_file=os.getenv("ES_CA_FILE", "").strip(),
            es_pit_keep_alive=os.getenv("ES_PIT_KEEP_ALIVE", "2m").strip(),
            es_default_index=os.getenv("ES_DEFAULT_INDEX", "logs-*").strip(),
            es_allowed_index_patterns=_csv("ES_ALLOWED_INDEX_PATTERNS", "logs-*"),
            es_max_limit=max_limit,
            log_max_stack_trace_length=_positive_int(
                "LOG_MAX_STACK_TRACE_LENGTH", 12000
            ),
            cursor_ttl_seconds=_positive_int("CURSOR_TTL_SECONDS", 120),
            log_profiles_file=profiles_file,
            metrics_api_base_url=os.getenv(
                "METRICS_API_BASE_URL", "http://localhost:9090/api/v1"
            ).rstrip("/"),
            metrics_auth=metrics_auth,
            metrics_timeout=_positive_float("METRICS_TIMEOUT_SECONDS", 30),
            metrics_verify_tls=_boolean("METRICS_VERIFY_TLS", True),
            metrics_ca_file=os.getenv("METRICS_CA_FILE", "").strip(),
            metrics_max_series=_positive_int("METRICS_MAX_SERIES", 1000),
            metrics_max_points=_positive_int("METRICS_MAX_POINTS", 10000),
            metrics_max_range_seconds=_positive_int("METRICS_MAX_RANGE_SECONDS", 604800),
            skywalking_graphql_url=os.getenv("SKYWALKING_GRAPHQL_URL", "").rstrip("/"),
            skywalking_auth=skywalking_auth,
            skywalking_timeout=_positive_float("SKYWALKING_TIMEOUT_SECONDS", 30),
            skywalking_verify_tls=_boolean("SKYWALKING_VERIFY_TLS", True),
            skywalking_ca_file=os.getenv("SKYWALKING_CA_FILE", "").strip(),
            skywalking_timezone=os.getenv(
                "SKYWALKING_TIMEZONE", "Asia/Shanghai"
            ).strip(),
            skywalking_max_range_seconds=_positive_int(
                "SKYWALKING_MAX_RANGE_SECONDS", 86400
            ),
            skywalking_max_page_size=skywalking_max_page_size,
            skywalking_service_cache_ttl_seconds=_positive_int(
                "SKYWALKING_SERVICE_CACHE_TTL_SECONDS", 600
            ),
            skywalking_service_cache_max_size=_positive_int(
                "SKYWALKING_SERVICE_CACHE_MAX_SIZE", 500
            ),
            mcp_host=os.getenv("MCP_HOST", "0.0.0.0").strip(),
            mcp_port=_positive_int("MCP_PORT", 8000),
        )


def tls_context(verify_tls: bool, ca_file: str) -> bool | ssl.SSLContext:
    if not verify_tls:
        return False
    if ca_file:
        path = Path(ca_file)
        if not path.is_file():
            raise ValueError(f"CA file does not exist: {ca_file}")
        return ssl.create_default_context(cafile=str(path))
    return True
