from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings, tls_context
from .errors import ToolError
from .http_auth import httpx_auth_and_headers
from .version import __version__

_DURATION_RE = re.compile(r"^(?:\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y))+$")
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_DURATION_FACTORS = {
    "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400,
    "w": 604800, "y": 31536000,
}


def parse_timestamp(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"Time {value!r} is not a Unix timestamp or ISO 8601 time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def parse_duration_seconds(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError as exc:
            if not _DURATION_RE.fullmatch(text):
                raise ToolError(
                    f"Invalid step {value!r}; examples: 30s, 5m, 1h"
                ) from exc
            seconds = sum(
                float(amount) * _DURATION_FACTORS[unit]
                for amount, unit in _DURATION_PART_RE.findall(text)
            )
    if seconds <= 0:
        raise ToolError("step must be greater than zero")
    return seconds


class MetricsClient:
    """Prometheus HTTP API client, also compatible with VictoriaMetrics."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        auth, headers = httpx_auth_and_headers(settings.metrics_auth)
        headers["User-Agent"] = f"observability-mcp/{__version__}"
        self._client = httpx.AsyncClient(
            auth=auth,
            headers=headers,
            timeout=settings.metrics_timeout,
            verify=tls_context(settings.metrics_verify_tls, settings.metrics_ca_file),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self.settings.metrics_api_base_url}{path}", params=params
            )
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"Metrics request exceeded {self.settings.metrics_timeout:g}s"
            ) from exc
        except httpx.RequestError as exc:
            raise ToolError(f"Cannot connect to metrics backend: {exc}") from exc
        if response.is_error:
            detail = response.text.strip().replace("\n", " ")[:500]
            raise ToolError(f"Metrics backend returned HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolError("Metrics backend response is not valid JSON") from exc
        if payload.get("status") != "success":
            raise ToolError(
                f"Metrics query failed ({payload.get('errorType', 'unknown')}): "
                f"{payload.get('error', 'unknown error')}"
            )
        return payload

    def _limit_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data") or {}
        result_type = data.get("resultType")
        result = data.get("result")
        truncated = False
        series_matched = 0
        points_returned = 0
        if isinstance(result, list) and result_type in {"vector", "matrix"}:
            series_matched = len(result)
            if len(result) > self.settings.metrics_max_series:
                result = result[: self.settings.metrics_max_series]
                truncated = True
            if result_type == "matrix":
                limited: list[dict[str, Any]] = []
                remaining = self.settings.metrics_max_points
                for series in result:
                    values = series.get("values") or []
                    if remaining <= 0:
                        truncated = True
                        break
                    kept = values[:remaining]
                    truncated = truncated or len(kept) < len(values)
                    limited.append({**series, "values": kept})
                    points_returned += len(kept)
                    remaining -= len(kept)
                result = limited
            else:
                points_returned = len(result)
        return {
            "status": "success",
            "result_type": result_type,
            "result": result,
            "series_matched": series_matched,
            "series_returned": len(result) if isinstance(result, list) else None,
            "points_returned": points_returned,
            "truncated": truncated,
            "warning": (
                "Result was truncated; add label filters, shorten the range, or increase step"
                if truncated else None
            ),
        }

    async def query_instant(
        self, promql: str, query_time: str | int | float | None = None
    ) -> dict[str, Any]:
        if not promql.strip():
            raise ToolError("promql cannot be empty")
        params: dict[str, Any] = {"query": promql}
        if query_time is not None:
            parse_timestamp(query_time)
            params["time"] = query_time
        return self._limit_result(await self._get("/query", params))

    async def query_range(
        self,
        promql: str,
        start: str | int | float,
        end: str | int | float,
        step: str | int | float = "60s",
    ) -> dict[str, Any]:
        if not promql.strip():
            raise ToolError("promql cannot be empty")
        start_seconds = parse_timestamp(start)
        end_seconds = parse_timestamp(end)
        step_seconds = parse_duration_seconds(step)
        if end_seconds <= start_seconds:
            raise ToolError("end must be later than start")
        duration = end_seconds - start_seconds
        if duration > self.settings.metrics_max_range_seconds:
            raise ToolError(
                f"Range cannot exceed {self.settings.metrics_max_range_seconds} seconds"
            )
        estimated_points = int(duration / step_seconds) + 1
        if estimated_points > self.settings.metrics_max_points:
            raise ToolError(
                f"Each series would contain about {estimated_points} points, exceeding "
                f"METRICS_MAX_POINTS={self.settings.metrics_max_points}; increase step"
            )
        return self._limit_result(await self._get(
            "/query_range",
            {"query": promql, "start": start, "end": end, "step": step},
        ))

    async def get_label_values(self, label: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", label):
            raise ToolError("label must be a valid Prometheus label name")
        payload = await self._get(f"/label/{quote(label, safe='')}/values")
        values = payload.get("data") or []
        limited = values[: self.settings.metrics_max_series]
        return {
            "status": "success",
            "label": label,
            "values": limited,
            "values_matched": len(values),
            "truncated": len(limited) < len(values),
        }
