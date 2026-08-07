from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .errors import ToolError
from .skywalking_client import SkyWalkingClient

MAX_LOG_CONTENT_LENGTH = 5000
MAX_TRACE_SPANS = 2000
LEVEL_PATTERN = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\b", re.IGNORECASE)
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(access[_-]?token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(secret\s*[:=]\s*)[^\s,;]+"),
)


@dataclass(frozen=True)
class _CachedService:
    service: dict[str, Any]
    expires_at: float


class SkyWalkingLogService:
    def __init__(self, settings: Settings, client: SkyWalkingClient) -> None:
        self.settings = settings
        self.client = client
        try:
            self.timezone = ZoneInfo(settings.skywalking_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Unknown SKYWALKING_TIMEZONE: {settings.skywalking_timezone}"
            ) from exc
        self._services: dict[str, _CachedService] = {}
        self._cache_lock = asyncio.Lock()

    def _parse_time(self, value: str | None, default: datetime) -> datetime:
        if value is None:
            return default
        text = value.strip()
        if not text:
            raise ToolError("time values must be non-empty ISO 8601 strings")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolError(f"Invalid ISO 8601 time: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(self.timezone)

    def _duration(
        self,
        start_time: str | None,
        end_time: str | None,
        default_range: timedelta,
    ) -> tuple[datetime, datetime, dict[str, str]]:
        now = datetime.now(self.timezone)
        end = self._parse_time(end_time, now)
        start = self._parse_time(start_time, end - default_range)
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            raise ToolError("end_time must be later than start_time")
        if seconds > self.settings.skywalking_max_range_seconds:
            raise ToolError(
                "SkyWalking range cannot exceed "
                f"{self.settings.skywalking_max_range_seconds} seconds"
            )
        if seconds <= 7200:
            step = "MINUTE"
            start_text = start.strftime("%Y-%m-%d %H%M")
            end_text = end.strftime("%Y-%m-%d %H%M")
        else:
            step = "HOUR"
            start_text = start.strftime("%Y-%m-%d %H")
            end_text = end.strftime("%Y-%m-%d %H")
        return start, end, {"start": start_text, "end": end_text, "step": step}

    async def _cache_services(self, services: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        ttl = self.settings.skywalking_service_cache_ttl_seconds
        async with self._cache_lock:
            self._services = {
                key: item
                for key, item in self._services.items()
                if item.expires_at > now
            }
            for service in services:
                name = str(service.get("name") or "").strip()
                service_id = str(service.get("id") or "").strip()
                if name and service_id:
                    self._services[name.lower()] = _CachedService(
                        {"name": name, "id": service_id},
                        now + ttl,
                    )
            max_size = self.settings.skywalking_service_cache_max_size
            if len(self._services) > max_size:
                ordered = sorted(
                    self._services.items(),
                    key=lambda item: item[1].expires_at,
                    reverse=True,
                )
                self._services = dict(ordered[:max_size])

    async def _cached_service(self, name: str) -> dict[str, Any] | None:
        key = name.lower()
        now = time.monotonic()
        async with self._cache_lock:
            item = self._services.get(key)
            if item is None:
                return None
            if item.expires_at <= now:
                self._services.pop(key, None)
                return None
            return dict(item.service)

    async def list_services(
        self,
        *,
        keyword: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ToolError("limit must be from 1 to 1000")
        normalized_keyword = keyword.strip() if keyword and keyword.strip() else None
        start, end, duration = self._duration(
            start_time,
            end_time,
            timedelta(
                seconds=min(self.settings.skywalking_max_range_seconds, 86400)
            ),
        )
        services = await self.client.search_services(duration, normalized_keyword)
        await self._cache_services(services)
        normalized = sorted(
            (
                {"service_name": item.get("name"), "service_id": item.get("id")}
                for item in services
                if item.get("name") and item.get("id")
            ),
            key=lambda item: str(item["service_name"]).lower(),
        )
        return {
            "source": "skywalking",
            "keyword": normalized_keyword,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "matched": len(normalized),
            "count": min(len(normalized), limit),
            "truncated": len(normalized) > limit,
            "services": normalized[:limit],
        }

    async def _resolve_service(
        self,
        service_name: str,
        duration: dict[str, str],
    ) -> tuple[dict[str, Any], bool]:
        name = service_name.strip()
        if not name:
            raise ToolError("service_name cannot be empty")
        cached = await self._cached_service(name)
        if cached:
            return cached, True
        matches = await self.client.search_services(duration, name)
        await self._cache_services(matches)
        exact = [item for item in matches if item.get("name") == name]
        if len(exact) == 1:
            return exact[0], False
        if not exact and len(matches) == 1:
            return matches[0], False
        if not matches:
            raise ToolError(f"SkyWalking service not found: {name!r}")
        choices = ", ".join(str(item.get("name")) for item in matches[:20])
        raise ToolError(f"Service name is ambiguous; available matches: {choices}")

    async def search_logs(
        self,
        *,
        service_name: str,
        keyword: str | None = None,
        trace_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        if page < 1:
            raise ToolError("page must be greater than zero")
        if not 1 <= limit <= self.settings.skywalking_max_page_size:
            raise ToolError(
                "limit must be from 1 to "
                f"{self.settings.skywalking_max_page_size}"
            )
        normalized_keyword = keyword.strip() if keyword and keyword.strip() else None
        normalized_trace = trace_id.strip() if trace_id and trace_id.strip() else None
        start, end, duration = self._duration(
            start_time,
            end_time,
            timedelta(minutes=30),
        )
        _, _, service_duration = self._duration(
            (
                end
                - timedelta(
                    seconds=min(self.settings.skywalking_max_range_seconds, 86400)
                )
            ).isoformat(),
            end.isoformat(),
            timedelta(minutes=30),
        )
        service, cache_hit = await self._resolve_service(
            service_name,
            service_duration,
        )
        condition: dict[str, Any] = {
            "queryDuration": duration,
            "paging": {"pageNum": page, "pageSize": limit},
            "serviceId": service["id"],
            "keywordsOfContent": [normalized_keyword] if normalized_keyword else [],
            "excludingKeywordsOfContent": [],
        }
        if normalized_trace:
            condition["relatedTrace"] = {"traceId": normalized_trace}
        raw_logs = await self.client.query_logs(condition)
        logs = [self._normalize_log(item) for item in raw_logs]
        return {
            "source": "skywalking",
            "service_name": service.get("name"),
            "service_id": service.get("id"),
            "service_cache_hit": cache_hit,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "page": page,
            "count": len(logs),
            "has_more": len(logs) == limit,
            "next_page": page + 1 if len(logs) == limit else None,
            "logs": logs,
        }

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        normalized = trace_id.strip()
        if not normalized:
            raise ToolError("trace_id cannot be empty")
        raw_spans = await self.client.query_trace(normalized)
        truncated = len(raw_spans) > MAX_TRACE_SPANS
        spans = [self._normalize_span(item) for item in raw_spans[:MAX_TRACE_SPANS]]
        spans.sort(key=lambda item: int(item.get("start_time_ms") or 0))
        return {
            "source": "skywalking",
            "trace_id": normalized,
            "span_count": len(raw_spans),
            "returned": len(spans),
            "truncated": truncated,
            "error_span_count": sum(item["is_error"] is True for item in spans),
            "spans": spans,
        }

    def _normalize_log(self, log: dict[str, Any]) -> dict[str, Any]:
        tags = log.get("tags") if isinstance(log.get("tags"), list) else []
        tag_values = {
            str(item.get("key")): item.get("value")
            for item in tags
            if isinstance(item, dict) and item.get("key")
        }
        content = _sanitize_content(str(log.get("content") or ""))
        level = next(
            (
                tag_values[key]
                for key in ("log.level", "level", "severity")
                if tag_values.get(key)
            ),
            None,
        )
        if level is None and (match := LEVEL_PATTERN.search(content)):
            level = match.group(1).upper()
        node = next(
            (
                tag_values[key]
                for key in ("host.ip", "node.ip", "host.name")
                if tag_values.get(key)
            ),
            log.get("serviceInstanceName"),
        )
        return {
            "timestamp": _format_timestamp(log.get("timestamp"), self.timezone),
            "message": content,
            "project": log.get("serviceName"),
            "node_ip": node,
            "trace_id": log.get("traceId"),
            "level": level,
            "endpoint": log.get("endpointName"),
            "service_instance_id": log.get("serviceInstanceId"),
        }

    def _normalize_span(self, span: dict[str, Any]) -> dict[str, Any]:
        start = span.get("startTime")
        end = span.get("endTime")
        try:
            duration_ms = int(end) - int(start)
        except (TypeError, ValueError):
            duration_ms = None
        return {
            "trace_id": span.get("traceId"),
            "segment_id": span.get("segmentId"),
            "span_id": span.get("spanId"),
            "parent_span_id": span.get("parentSpanId"),
            "service": span.get("serviceCode"),
            "node_ip": span.get("serviceInstanceName"),
            "endpoint": span.get("endpointName"),
            "start_time_ms": start,
            "start_time": _format_timestamp(start, self.timezone),
            "end_time_ms": end,
            "end_time": _format_timestamp(end, self.timezone),
            "duration_ms": duration_ms,
            "type": span.get("type"),
            "peer": span.get("peer"),
            "component": span.get("component"),
            "layer": span.get("layer"),
            "is_error": span.get("isError"),
            "refs": span.get("refs") or [],
            "tags": span.get("tags") or [],
            "logs": span.get("logs") or [],
        }


def _sanitize_content(content: str) -> str:
    result = content
    for pattern in SENSITIVE_PATTERNS:
        result = pattern.sub(lambda match: f"{match.group(1)}***", result)
    if len(result) > MAX_LOG_CONTENT_LENGTH:
        return result[:MAX_LOG_CONTENT_LENGTH] + "\n...[log content truncated]"
    return result


def _format_timestamp(value: Any, timezone: ZoneInfo) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
