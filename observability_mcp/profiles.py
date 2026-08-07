from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ToolError

FIELD_NAME = re.compile(r"^[A-Za-z0-9_.@-]+$")
OUTPUT_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
UNMAPPED_TYPES = {
    "boolean", "byte", "date", "date_nanos", "double", "float", "half_float",
    "integer", "keyword", "long", "short", "unsigned_long",
}


def _strings(value: Any, location: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        values: tuple[str, ...] = ()
    elif isinstance(value, str):
        values = (value.strip(),) if value.strip() else ()
    elif isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        values = tuple(item.strip() for item in value)
    else:
        raise ValueError(f"{location} must be a string or a list of non-empty strings")
    if required and not values:
        raise ValueError(f"{location} must contain at least one value")
    return values


def _field_names(value: Any, location: str, *, required: bool = False) -> tuple[str, ...]:
    values = _strings(value, location, required=required)
    for field in values:
        if not FIELD_NAME.fullmatch(field):
            raise ValueError(f"{location} contains an invalid field name: {field!r}")
    return values


@dataclass(frozen=True)
class ErrorFields:
    type: tuple[str, ...]
    message: tuple[str, ...]
    stack_trace: tuple[str, ...]


@dataclass(frozen=True)
class SearchFields:
    timestamp: tuple[str, ...]
    message: tuple[str, ...]
    project: tuple[str, ...]
    node_ip: tuple[str, ...]
    trace_exact: tuple[str, ...]
    trace_text: tuple[str, ...]
    trace_message_fallback: bool
    level_exact: tuple[str, ...]
    level_text: tuple[str, ...]
    level_message_fallback: bool
    error: ErrorFields
    attributes: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def source_fields(self) -> tuple[str, ...]:
        attribute_fields = tuple(
            field
            for _, candidates in self.attributes
            for field in candidates
        )
        return tuple(dict.fromkeys(
            self.timestamp + self.message + self.project + self.node_ip + self.trace_exact
            + self.trace_text + self.level_exact + self.level_text + self.error.type
            + self.error.message + self.error.stack_trace + attribute_fields
        ))


@dataclass(frozen=True)
class SortField:
    field: str
    order: str
    unmapped_type: str


@dataclass(frozen=True)
class LogProfile:
    name: str
    index_patterns: tuple[str, ...]
    fields: SearchFields
    sort: tuple[SortField, ...]


class LogProfileRegistry:
    def __init__(self, profiles: tuple[LogProfile, ...]) -> None:
        if not profiles:
            raise ValueError("At least one log profile is required")
        names = [profile.name for profile in profiles]
        if len(names) != len(set(names)):
            raise ValueError("Log profile names must be unique")
        self.profiles = profiles
        self._by_name = {profile.name: profile for profile in profiles}

    @classmethod
    def from_file(cls, filename: str) -> LogProfileRegistry:
        path = Path(filename)
        if not path.is_file():
            raise ValueError(f"Log profiles file does not exist: {filename}")
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {filename}: {exc}") from exc
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise ValueError("log profiles file must contain a 'profiles' list")
        return cls(tuple(_parse_profile(item, index) for index, item in enumerate(raw_profiles)))

    def get(self, name: str) -> LogProfile:
        try:
            return self._by_name[name]
        except KeyError as exc:
            choices = ", ".join(self._by_name)
            raise ToolError(f"Unknown log profile {name!r}; available profiles: {choices}") from exc

    def select(self, index_expression: str) -> LogProfile:
        expressions = [part.strip() for part in index_expression.split(",") if part.strip()]
        for profile in self.profiles:
            if all(
                _patterns_overlap(expression, profile.index_patterns)
                for expression in expressions
            ):
                return profile
        raise ToolError(
            f"No log profile matches index expression {index_expression!r}; "
            "narrow the index expression or configure its index_patterns"
        )


def _patterns_overlap(expression: str, profile_patterns: tuple[str, ...]) -> bool:
    """Return whether one profile pattern fully covers the query expression.

    A broad query such as ``*`` overlaps every specialized profile, but it is
    not fully described by any of them and must therefore use the generic
    profile.  Matching only in this direction prevents first-profile wins.
    """
    return any(
        fnmatch.fnmatchcase(expression, pattern)
        for pattern in profile_patterns
    )


def _parse_profile(raw: Any, index: int) -> LogProfile:
    location = f"profiles[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{location} must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"{location}.name must contain only letters, numbers, '_' or '-'")
    patterns = _strings(raw.get("index_patterns"), f"{location}.index_patterns", required=True)
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise ValueError(f"{location}.fields must be an object")

    trace = fields.get("trace_id") or {}
    level = fields.get("level") or {}
    error = fields.get("error") or {}
    attributes = fields.get("attributes") or {}
    if not all(isinstance(item, dict) for item in (trace, level, error, attributes)):
        raise ValueError(
            f"{location}.fields.trace_id, level, error, and attributes must be objects"
        )

    for field_name, field_config in (("trace_id", trace), ("level", level)):
        message_fallback = field_config.get("message_fallback", False)
        if not isinstance(message_fallback, bool):
            raise ValueError(
                f"{location}.fields.{field_name}.message_fallback must be a boolean"
            )

    parsed_attributes: list[tuple[str, tuple[str, ...]]] = []
    for output_name, candidates in attributes.items():
        if not isinstance(output_name, str) or not OUTPUT_FIELD_NAME.fullmatch(output_name):
            raise ValueError(
                f"{location}.fields.attributes contains an invalid output name: "
                f"{output_name!r}"
            )
        parsed_attributes.append((
            output_name,
            _field_names(
                candidates,
                f"{location}.fields.attributes.{output_name}",
                required=True,
            ),
        ))

    search_fields = SearchFields(
        timestamp=_field_names(
            fields.get("timestamp"), f"{location}.fields.timestamp", required=True
        ),
        message=_field_names(fields.get("message"), f"{location}.fields.message", required=True),
        project=_field_names(fields.get("project"), f"{location}.fields.project"),
        node_ip=_field_names(fields.get("node_ip"), f"{location}.fields.node_ip"),
        trace_exact=_field_names(trace.get("exact"), f"{location}.fields.trace_id.exact"),
        trace_text=_field_names(trace.get("text"), f"{location}.fields.trace_id.text"),
        trace_message_fallback=trace.get("message_fallback", False),
        level_exact=_field_names(level.get("exact"), f"{location}.fields.level.exact"),
        level_text=_field_names(level.get("text"), f"{location}.fields.level.text"),
        level_message_fallback=level.get("message_fallback", False),
        error=ErrorFields(
            type=_field_names(error.get("type"), f"{location}.fields.error.type"),
            message=_field_names(error.get("message"), f"{location}.fields.error.message"),
            stack_trace=_field_names(
                error.get("stack_trace"),
                f"{location}.fields.error.stack_trace",
            ),
        ),
        attributes=tuple(parsed_attributes),
    )

    raw_sort = raw.get("sort")
    if not isinstance(raw_sort, list) or not raw_sort:
        raise ValueError(f"{location}.sort must be a non-empty list")
    sort_fields: list[SortField] = []
    for sort_index, item in enumerate(raw_sort):
        sort_location = f"{location}.sort[{sort_index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{sort_location} must be an object")
        field = item.get("field")
        order = item.get("order", "asc")
        unmapped_type = item.get("unmapped_type", "keyword")
        if not isinstance(field, str) or not FIELD_NAME.fullmatch(field):
            raise ValueError(f"{sort_location}.field is invalid")
        if order not in {"asc", "desc"}:
            raise ValueError(f"{sort_location}.order must be asc or desc")
        if unmapped_type not in UNMAPPED_TYPES:
            raise ValueError(f"{sort_location}.unmapped_type is not supported")
        sort_fields.append(SortField(field, order, unmapped_type))
    return LogProfile(name, patterns, search_fields, tuple(sort_fields))
