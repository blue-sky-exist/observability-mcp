from __future__ import annotations

from typing import Any

import httpx

from .config import Settings, tls_context
from .errors import ToolError
from .http_auth import httpx_auth_and_headers
from .version import __version__

SEARCH_SERVICES_QUERY = """
query searchServices($duration: Duration!, $keyword: String!) {
  searchServices(duration: $duration, keyword: $keyword) { id name }
}
"""

GET_ALL_SERVICES_QUERY = """
query getAllServices($duration: Duration!) {
  getAllServices(duration: $duration) { id name }
}
"""

QUERY_LOGS_QUERY = """
query queryLogs($condition: LogQueryCondition) {
  queryLogs(condition: $condition) {
    logs {
      serviceName serviceId serviceInstanceName serviceInstanceId
      endpointName endpointId traceId timestamp contentType content
      tags { key value }
    }
  }
}
"""

QUERY_TRACE_QUERY = """
query queryTrace($traceId: ID!) {
  queryTrace(traceId: $traceId) {
    spans {
      traceId segmentId spanId parentSpanId
      refs { traceId parentSegmentId parentSpanId type }
      serviceCode serviceInstanceName startTime endTime endpointName
      type peer component isError layer
      tags { key value }
      logs { time data { key value } }
    }
  }
}
"""


class SkyWalkingClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        auth, headers = httpx_auth_and_headers(settings.skywalking_auth)
        headers["User-Agent"] = f"observability-mcp/{__version__}"
        headers["Content-Type"] = "application/json"
        self._client = httpx.AsyncClient(
            auth=auth,
            headers=headers,
            timeout=settings.skywalking_timeout,
            verify=tls_context(
                settings.skywalking_verify_tls,
                settings.skywalking_ca_file,
            ),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def execute(
        self,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.settings.skywalking_graphql_url:
            raise ToolError(
                "SkyWalking is not configured; set SKYWALKING_GRAPHQL_URL"
            )
        try:
            response = await self._client.post(
                self.settings.skywalking_graphql_url,
                json={"query": query, "variables": variables},
            )
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"SkyWalking request exceeded {self.settings.skywalking_timeout:g}s"
            ) from exc
        except httpx.RequestError as exc:
            raise ToolError(f"Cannot connect to SkyWalking: {exc}") from exc

        if response.is_error:
            detail = response.text.strip().replace("\n", " ")[:500]
            raise ToolError(
                f"SkyWalking returned HTTP {response.status_code}: "
                f"{detail or 'empty response'}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolError("SkyWalking response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ToolError("SkyWalking response must be a JSON object")
        errors = payload.get("errors")
        if errors:
            messages = [
                str(item.get("message", "unknown GraphQL error"))
                if isinstance(item, dict)
                else str(item)
                for item in errors[:5]
            ]
            raise ToolError("SkyWalking GraphQL query failed: " + "; ".join(messages))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ToolError("SkyWalking response is missing a valid data object")
        return data

    async def search_services(
        self,
        duration: dict[str, str],
        keyword: str | None,
    ) -> list[dict[str, Any]]:
        if keyword:
            data = await self.execute(
                SEARCH_SERVICES_QUERY,
                {"duration": duration, "keyword": keyword},
            )
            services = data.get("searchServices")
        else:
            data = await self.execute(
                GET_ALL_SERVICES_QUERY,
                {"duration": duration},
            )
            services = data.get("getAllServices")
        if services is None:
            return []
        if not isinstance(services, list):
            raise ToolError("SkyWalking services response is not a list")
        return [item for item in services if isinstance(item, dict)]

    async def query_logs(self, condition: dict[str, Any]) -> list[dict[str, Any]]:
        data = await self.execute(QUERY_LOGS_QUERY, {"condition": condition})
        result = data.get("queryLogs")
        if not isinstance(result, dict):
            raise ToolError("SkyWalking response is missing data.queryLogs")
        logs = result.get("logs") or []
        if not isinstance(logs, list):
            raise ToolError("SkyWalking queryLogs.logs response is not a list")
        return [item for item in logs if isinstance(item, dict)]

    async def query_trace(self, trace_id: str) -> list[dict[str, Any]]:
        data = await self.execute(QUERY_TRACE_QUERY, {"traceId": trace_id})
        trace = data.get("queryTrace")
        if trace is None:
            return []
        if not isinstance(trace, dict):
            raise ToolError("SkyWalking queryTrace response is invalid")
        spans = trace.get("spans") or []
        if not isinstance(spans, list):
            raise ToolError("SkyWalking queryTrace.spans response is not a list")
        return [item for item in spans if isinstance(item, dict)]
