from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings
from .cursors import MemoryCursorStore
from .es_client import ElasticsearchClient
from .log_search import LogSearchService
from .metrics_client import MetricsClient
from .profiles import LogProfileRegistry
from .skywalking_client import SkyWalkingClient
from .skywalking_logs import SkyWalkingLogService
from .version import __version__

settings = Settings.from_env()
profiles = LogProfileRegistry.from_file(settings.log_profiles_file)
es_client = ElasticsearchClient(settings)
metrics_client = MetricsClient(settings)
skywalking_client = SkyWalkingClient(settings)
cursor_store = MemoryCursorStore(settings.cursor_ttl_seconds)
log_service = LogSearchService(settings, es_client, cursor_store, profiles)
skywalking_service = SkyWalkingLogService(settings, skywalking_client)


@asynccontextmanager
async def app_lifespan(_: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.gather(
            es_client.close(),
            metrics_client.close(),
            skywalking_client.close(),
        )


mcp = MCPServer(
    "Observability MCP",
    version=__version__,
    instructions=(
        "Environment routing policy: use Elasticsearch search_logs for production/formal "
        "environment logs. Use list_skywalking_services/search_skywalking_logs for "
        "test/testing environment logs, and get_skywalking_trace for test environment "
        "traces. If the user does not specify an environment, default to the production "
        "environment and use search_logs. Do not automatically retry a log query in the "
        "other environment; ask the user to confirm the environment when results are "
        "missing or the request is ambiguous. Metrics use query_instant/query_range. "
        "Prefer a narrow index expression. For pagination, send only the returned cursor."
    ),
    lifespan=app_lifespan,
)


@mcp.tool()
async def list_log_indices(
    index: Annotated[
        str | None,
        Field(
            description=(
                "Optional allowed index expression; defaults to all "
                "ES_ALLOWED_INDEX_PATTERNS"
            )
        ),
    ] = None,
    target_type: Annotated[
        Literal["all", "alias", "data_stream", "index"],
        Field(description="Search target type to return; use alias to list only aliases"),
    ] = "all",
    limit: Annotated[
        int,
        Field(ge=1, le=1000, description="Maximum number of search targets to return"),
    ] = 200,
    include_system: Annotated[
        bool,
        Field(description="Include dot-prefixed and Elasticsearch system indices"),
    ] = False,
) -> dict[str, Any]:
    """List allowed search targets, excluding system indices by default."""
    return await log_service.list_indices(
        index=index,
        target_type=target_type,
        limit=limit,
        include_system=include_system,
    )


@mcp.tool()
async def search_logs(
    trace_id: Annotated[
        str | None,
        Field(description="Trace ID; message fallback is controlled by the selected profile"),
    ] = None,
    node_ip: Annotated[
        str | None,
        Field(description="Exact node IP or configured machine/host name"),
    ] = None,
    keyword: Annotated[
        str | None,
        Field(description="Keyword or phrase searched in the profile's message fields"),
    ] = None,
    level: Annotated[
        Literal["DEBUG", "ERROR", "FATAL", "INFO", "TRACE", "WARN"] | None,
        Field(description="Log level; message fallback is controlled by the selected profile"),
    ] = None,
    start_time: Annotated[
        str | None,
        Field(description="Inclusive lower time bound, ISO 8601"),
    ] = None,
    end_time: Annotated[
        str | None,
        Field(description="Inclusive upper time bound, ISO 8601"),
    ] = None,
    index: Annotated[
        str | None,
        Field(description="Index expression; defaults to ES_DEFAULT_INDEX"),
    ] = None,
    cursor: Annotated[
        str | None,
        Field(description="One-time next_cursor returned by the previous page"),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=1000, description="Logs per page; server ES_MAX_LIMIT also applies"),
    ] = 50,
) -> dict[str, Any]:
    """Search production/formal environment logs stored in Elasticsearch."""
    return await log_service.search(
        trace_id=trace_id,
        node_ip=node_ip,
        keyword=keyword,
        level=level,
        start_time=start_time,
        end_time=end_time,
        index=index,
        cursor=cursor,
        limit=limit,
    )


@mcp.tool()
async def list_skywalking_services(
    keyword: Annotated[
        str | None,
        Field(description="Optional SkyWalking service name keyword"),
    ] = None,
    start_time: Annotated[
        str | None,
        Field(description="Optional ISO 8601 service discovery start time"),
    ] = None,
    end_time: Annotated[
        str | None,
        Field(description="Optional ISO 8601 service discovery end time"),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=1000, description="Maximum services to return"),
    ] = 200,
) -> dict[str, Any]:
    """List services that have reported to the test SkyWalking environment."""
    return await skywalking_service.list_services(
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )


@mcp.tool()
async def search_skywalking_logs(
    service_name: Annotated[
        str,
        Field(description="SkyWalking service name"),
    ],
    keyword: Annotated[
        str | None,
        Field(description="Optional content keyword"),
    ] = None,
    trace_id: Annotated[
        str | None,
        Field(description="Optional related SkyWalking trace ID"),
    ] = None,
    start_time: Annotated[
        str | None,
        Field(description="Optional ISO 8601 start; defaults to 30 minutes ago"),
    ] = None,
    end_time: Annotated[
        str | None,
        Field(description="Optional ISO 8601 end; defaults to now"),
    ] = None,
    page: Annotated[
        int,
        Field(ge=1, description="SkyWalking result page number"),
    ] = 1,
    limit: Annotated[
        int,
        Field(ge=1, le=1000, description="Logs per page; server limit also applies"),
    ] = 50,
) -> dict[str, Any]:
    """Search test/testing environment logs stored in SkyWalking."""
    return await skywalking_service.search_logs(
        service_name=service_name,
        keyword=keyword,
        trace_id=trace_id,
        start_time=start_time,
        end_time=end_time,
        page=page,
        limit=limit,
    )


@mcp.tool()
async def get_skywalking_trace(
    trace_id: Annotated[str, Field(description="SkyWalking trace ID")],
) -> dict[str, Any]:
    """Return a complete test-environment SkyWalking trace span chain."""
    return await skywalking_service.get_trace(trace_id)


@mcp.tool()
async def query_instant(
    promql: Annotated[str, Field(description="PromQL expression")],
    query_time: Annotated[
        str | int | float | None,
        Field(description="Optional Unix timestamp or ISO 8601 evaluation time"),
    ] = None,
) -> dict[str, Any]:
    """Execute an instant Prometheus-compatible query."""
    return await metrics_client.query_instant(promql, query_time)


@mcp.tool()
async def query_range(
    promql: Annotated[str, Field(description="PromQL expression")],
    start: Annotated[str | int | float, Field(description="Unix or ISO 8601 start time")],
    end: Annotated[str | int | float, Field(description="Unix or ISO 8601 end time")],
    step: Annotated[
        str | int | float,
        Field(description="Positive step in seconds or duration such as 30s or 5m"),
    ] = "60s",
) -> dict[str, Any]:
    """Execute a range Prometheus-compatible query."""
    return await metrics_client.query_range(promql, start, end, step)


@mcp.tool()
async def get_label_values(
    label: Annotated[str, Field(description="Prometheus label name, e.g. application")],
) -> dict[str, Any]:
    """List visible values for one Prometheus label."""
    return await metrics_client.get_label_values(label)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "observability-mcp",
        "version": __version__,
        "transport": "streamable-http",
        "cursor_store": "memory",
        "es_default_index": settings.es_default_index,
        "skywalking_configured": bool(settings.skywalking_graphql_url),
        "log_profiles": [profile.name for profile in profiles.profiles],
    })
