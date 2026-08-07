from __future__ import annotations

import fnmatch
from typing import Any

from .config import Settings
from .cursors import LogCursorState, MemoryCursorStore
from .errors import ToolError
from .es_client import ElasticsearchClient
from .profiles import LogProfile, LogProfileRegistry

LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
FORBIDDEN_INDEX_CHARS = "/\\?#\r\n\t "


def _optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be a non-empty string")
    return value.strip()


def _static_prefix(pattern: str) -> str:
    positions = [position for token in "*?[" if (position := pattern.find(token)) >= 0]
    return pattern[: min(positions)] if positions else pattern


def _index_token_allowed(token: str, allowed_pattern: str) -> bool:
    if allowed_pattern == "*":
        return True
    token_has_glob = any(char in token for char in "*?[")
    allowed_has_glob = any(char in allowed_pattern for char in "*?[")
    if not token_has_glob:
        return fnmatch.fnmatchcase(token, allowed_pattern)
    if not allowed_has_glob:
        return token == allowed_pattern
    return _static_prefix(token).startswith(_static_prefix(allowed_pattern))


def resolve_index(index: str | None, settings: Settings) -> str:
    value = _optional_text(index, "index") or settings.es_default_index
    if any(char in value for char in FORBIDDEN_INDEX_CHARS):
        raise ToolError("index contains a forbidden character")
    tokens = [part for part in value.split(",") if part]
    if not tokens or any(
        not any(
            _index_token_allowed(token, allowed)
            for allowed in settings.es_allowed_index_patterns
        )
        for token in tokens
    ):
        allowed = ", ".join(settings.es_allowed_index_patterns)
        raise ToolError(f"index is outside ES_ALLOWED_INDEX_PATTERNS: {allowed}")
    return value


def _is_allowed_resolved_name(name: str, settings: Settings) -> bool:
    return any(
        _index_token_allowed(name, allowed)
        for allowed in settings.es_allowed_index_patterns
    )


def _is_system_target(item: dict[str, Any], name: str) -> bool:
    attributes = item.get("attributes") or []
    return name.startswith(".") or (
        isinstance(attributes, list) and "system" in attributes
    )


def _should(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    return {"bool": {"should": clauses, "minimum_should_match": 1}}


def build_search_body(
    args: dict[str, Any],
    profile: LogProfile,
    max_limit: int,
) -> dict[str, Any]:
    trace_id = _optional_text(args.get("trace_id"), "trace_id")
    node_ip = _optional_text(args.get("node_ip"), "node_ip")
    keyword = _optional_text(args.get("keyword"), "keyword")
    level = _optional_text(args.get("level"), "level")
    start_time = _optional_text(args.get("start_time"), "start_time")
    end_time = _optional_text(args.get("end_time"), "end_time")
    limit = args.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
        raise ToolError(f"limit must be an integer from 1 to {max_limit}")
    if level:
        level = level.upper()
        if level not in LEVELS:
            raise ToolError("level must be TRACE, DEBUG, INFO, WARN, ERROR, or FATAL")
    if not any((trace_id, node_ip, keyword, level, start_time, end_time)):
        raise ToolError("Provide at least one search condition or time bound")

    fields = profile.fields
    filters: list[dict[str, Any]] = []
    if trace_id:
        clauses = [{"term": {field: trace_id}} for field in fields.trace_exact]
        clauses.extend({"match_phrase": {field: trace_id}} for field in fields.trace_text)
        if fields.trace_message_fallback:
            clauses.extend({"match_phrase": {field: trace_id}} for field in fields.message)
        filters.append(_should(clauses))
    if node_ip:
        if not fields.node_ip:
            raise ToolError(f"Log profile {profile.name!r} does not configure node_ip fields")
        filters.append(_should([{"term": {field: node_ip}} for field in fields.node_ip]))
    if keyword:
        filters.append(_should([{"match_phrase": {field: keyword}} for field in fields.message]))
    if level:
        clauses = [
            {"term": {field: {"value": level, "case_insensitive": True}}}
            for field in fields.level_exact
        ]
        clauses.extend({"match_phrase": {field: level}} for field in fields.level_text)
        if fields.level_message_fallback:
            clauses.extend({"match_phrase": {field: level}} for field in fields.message)
        filters.append(_should(clauses))
    if start_time or end_time:
        bounds = {
            key: value for key, value in (("gte", start_time), ("lte", end_time)) if value
        }
        filters.append(_should([{"range": {field: bounds}} for field in fields.timestamp]))

    source_fields = tuple(dict.fromkeys(
        fields.source_fields + tuple(sort.field for sort in profile.sort)
    ))
    sort = [
        {item.field: {"order": item.order, "unmapped_type": item.unmapped_type}}
        for item in profile.sort
    ]
    sort.append({"_shard_doc": {"order": "asc"}})
    return {
        "size": limit,
        "track_total_hits": True,
        "_source": list(source_fields),
        "query": {"bool": {"filter": filters}},
        "sort": sort,
    }


def get_path(source: dict[str, Any], path: str) -> Any:
    if path in source:
        return source[path]
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def first_value(source: dict[str, Any], paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = get_path(source, path)
        if value is not None:
            return value
    return None


def first_scalar_value(source: dict[str, Any], paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = get_path(source, path)
        if isinstance(value, list):
            value = next((item for item in value if item is not None and item != ""), None)
        if value is not None and value != "":
            return value
    return None


def _truncate_stack_trace(value: Any, max_length: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    if len(text) <= max_length:
        return text, False
    marker = "\n...[stack trace truncated]...\n"
    available = max(0, max_length - len(marker))
    if available == 0:
        return text[:max_length], True
    head_length = available * 2 // 3
    tail_length = available - head_length
    return text[:head_length] + marker + text[-tail_length:], True


def normalize_hit(
    hit: dict[str, Any],
    profile: LogProfile,
    max_stack_trace_length: int = 12000,
) -> dict[str, Any]:
    source = hit.get("_source") or {}
    fields = profile.fields
    stack_trace, stack_trace_truncated = _truncate_stack_trace(
        first_scalar_value(source, fields.error.stack_trace),
        max_stack_trace_length,
    )
    error_type = first_scalar_value(source, fields.error.type)
    error_message = first_scalar_value(source, fields.error.message)
    error = None
    if any(value is not None for value in (error_type, error_message, stack_trace)):
        error = {
            "type": error_type,
            "message": error_message,
            "stack_trace": stack_trace,
            "stack_trace_truncated": stack_trace_truncated,
        }
    attributes = {
        name: value
        for name, candidates in fields.attributes
        if (value := first_scalar_value(source, candidates)) is not None
    }
    return {
        "timestamp": first_value(source, fields.timestamp),
        "message": first_value(source, fields.message),
        "project": first_value(source, fields.project),
        "node_ip": first_scalar_value(source, fields.node_ip),
        "trace_id": first_value(source, fields.trace_exact + fields.trace_text),
        "level": first_value(source, fields.level_exact + fields.level_text),
        "error": error,
        "attributes": attributes,
    }


class LogSearchService:
    def __init__(
        self,
        settings: Settings,
        es: ElasticsearchClient,
        cursors: MemoryCursorStore,
        profiles: LogProfileRegistry,
    ) -> None:
        self.settings = settings
        self.es = es
        self.cursors = cursors
        self.profiles = profiles

    async def list_indices(
        self,
        *,
        index: str | None = None,
        target_type: str = "all",
        limit: int = 200,
        include_system: bool = False,
    ) -> dict[str, Any]:
        if target_type not in {"all", "index", "alias", "data_stream"}:
            raise ToolError("target_type must be all, index, alias, or data_stream")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ToolError("limit must be an integer from 1 to 1000")
        expression = (
            resolve_index(index, self.settings)
            if index is not None
            else ",".join(self.settings.es_allowed_index_patterns)
        )
        payload = await self.es.resolve_indices(expression)
        targets: list[dict[str, str]] = []
        system_indices_excluded = 0
        for response_key, resolved_type in (
            ("indices", "index"),
            ("aliases", "alias"),
            ("data_streams", "data_stream"),
        ):
            if target_type != "all" and resolved_type != target_type:
                continue
            items = payload.get(response_key) or []
            if not isinstance(items, list):
                raise ToolError("Elasticsearch returned an invalid index resolution response")
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                if not isinstance(name, str) or not _is_allowed_resolved_name(
                    name, self.settings
                ):
                    continue
                if not include_system and _is_system_target(item, name):
                    system_indices_excluded += 1
                    continue
                targets.append({"name": name, "type": resolved_type})

        targets.sort(key=lambda item: (item["name"], item["type"]))
        matched = len(targets)
        return {
            "index_expression": expression,
            "target_type": target_type,
            "matched": matched,
            "count": min(matched, limit),
            "truncated": matched > limit,
            "system_indices_excluded": system_indices_excluded,
            "targets": targets[:limit],
        }

    async def search(
        self,
        *,
        trace_id: str | None = None,
        node_ip: str | None = None,
        keyword: str | None = None,
        level: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        index: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        supplied = any(
            value is not None
            for value in (trace_id, node_ip, keyword, level, start_time, end_time, index)
        )
        if cursor:
            if supplied or limit != 50:
                raise ToolError(
                    "When cursor is provided, do not repeat filters, index, or limit"
                )
            state = await self.cursors.take(cursor)
            selected_profile = self.profiles.get(state.profile_name)
            body = build_search_body(
                state.search_args, selected_profile, self.settings.es_max_limit
            )
            try:
                response = await self.es.search_pit(state.pit_id, body, state.search_after)
            except Exception:
                await self.cursors.save(state, token=cursor)
                raise
            return await self._format_page(response, state, selected_profile)

        resolved_index = resolve_index(index, self.settings)
        selected_profile = self.profiles.select(resolved_index)
        args = {
            "trace_id": trace_id,
            "node_ip": node_ip,
            "keyword": keyword,
            "level": level,
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
        }
        body = build_search_body(args, selected_profile, self.settings.es_max_limit)
        normalized_args = {key: value for key, value in args.items() if value is not None}
        normalized_args["limit"] = limit
        pit_id = await self.es.open_pit(resolved_index)
        try:
            response = await self.es.search_pit(pit_id, body)
        except Exception:
            await self.es.close_pit(pit_id)
            raise
        state = LogCursorState(
            pit_id=pit_id,
            index_expression=resolved_index,
            search_after=[],
            search_args=normalized_args,
            profile_name=selected_profile.name,
            limit=limit,
            seen=0,
        )
        return await self._format_page(response, state, selected_profile)

    async def _format_page(
        self,
        response: dict[str, Any],
        state: LogCursorState,
        profile: LogProfile,
    ) -> dict[str, Any]:
        hits_section = response.get("hits") or {}
        total_value = hits_section.get("total", 0)
        if isinstance(total_value, dict):
            total = int(total_value.get("value", 0))
            relation = str(total_value.get("relation", "eq"))
        else:
            total, relation = int(total_value or 0), "eq"
        raw_hits = hits_section.get("hits") or []
        logs = [
            normalize_hit(hit, profile, self.settings.log_max_stack_trace_length)
            for hit in raw_hits
        ]
        seen = state.seen + len(raw_hits)
        has_more = bool(raw_hits) and len(raw_hits) == state.limit and (
            relation != "eq" or seen < total
        )
        pit_id = response.get("pit_id") or state.pit_id
        next_cursor: str | None = None
        if has_more:
            sort_values = raw_hits[-1].get("sort")
            expected = len(profile.sort) + 1
            if not isinstance(sort_values, list) or len(sort_values) != expected:
                await self.es.close_pit(pit_id)
                raise ToolError("Elasticsearch response is missing a valid PIT sort cursor")
            next_cursor = await self.cursors.save(LogCursorState(
                pit_id=pit_id,
                index_expression=state.index_expression,
                search_after=sort_values,
                search_args=state.search_args,
                profile_name=profile.name,
                limit=state.limit,
                seen=seen,
            ))
        else:
            await self.es.close_pit(pit_id)
        return {
            "profile": profile.name,
            "index_expression": state.index_expression,
            "total": total,
            "total_relation": relation,
            "count": len(logs),
            "took_ms": response.get("took"),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "logs": logs,
        }
