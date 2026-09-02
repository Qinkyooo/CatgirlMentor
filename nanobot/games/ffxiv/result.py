"""Stable JSON envelopes returned by FF14 tools."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias, cast

from nanobot.agent.tools.base import ToolResult

from .types import Evidence, Freshness

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


def _camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def to_jsonable(value: object) -> JsonValue:
    """Convert supported first-party values into the public JSON shape."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        instance = cast(object, value)
        return {
            _camel_case(field.name): to_jsonable(getattr(instance, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in mapping):
            raise TypeError("JSON object keys must be strings")
        return {
            _camel_case(cast(str, key)): to_jsonable(item)
            for key, item in mapping.items()
        }
    if isinstance(value, list | tuple):
        items = cast(list[object] | tuple[object, ...], value)
        return [to_jsonable(item) for item in items]
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def success_result(
    *,
    kind: str,
    data: object,
    evidence: Sequence[Evidence],
    freshness: Freshness,
    warnings: Sequence[str] = (),
) -> str:
    payload = {
        "ok": True,
        "kind": kind,
        "data": data,
        "evidence": list(evidence),
        "freshness": freshness,
        "warnings": list(warnings),
    }
    return json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)


def error_result(
    code: str,
    message: str,
    *,
    suggestions: Sequence[str] = (),
) -> ToolResult:
    payload = {
        "ok": False,
        "error": {"code": code, "message": message},
        "suggestions": list(suggestions),
    }
    return ToolResult.error(
        json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    )
