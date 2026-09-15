"""Load, validate, and refresh the PvP rotation rules snapshot.

The snapshot lives outside the code so that a published build never needs a code
change to follow the game: a new map roster is a data update, and an outdated
snapshot is reported as drift rather than caused by an expiry date somebody had
to guess when the build was cut.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION: Final[int] = 1
BUNDLED_RULES_NAME: Final[str] = "pvp-rules.json"
BUNDLED_RULES: Final[str] = "bundled"
RotationMode = Literal["frontline", "cc"]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)


class RotationRulesError(RuntimeError):
    """A stable, user-actionable rotation-rule failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RotationModeRules:
    reference: datetime
    interval: timedelta
    order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RotationRulesBundle:
    schema_version: int
    source_url: str | None
    verified_at: str | None
    rotation_digest: str
    modes: Mapping[str, RotationModeRules]
    names: Mapping[str, str]

    def rules_for(self, mode: str) -> RotationModeRules:
        try:
            return self.modes[mode]
        except KeyError as exc:
            raise RotationRulesError(
                "pvp_rules_invalid",
                f"轮换规则快照缺少模式 {mode!r}",
            ) from exc

    def name_for(self, map_id: str) -> str:
        return self.names.get(map_id, map_id)


def bundled_rules_path() -> Path:
    return Path(__file__).resolve().parent / "data" / BUNDLED_RULES_NAME


def canonical_rotation_payload(
    modes: Mapping[str, RotationModeRules],
) -> dict[str, object]:
    """Return the rotation-relevant projection used for digests.

    Display metadata (map names, verification dates) is deliberately excluded so
    that a wording or translation tweak upstream is not mistaken for a schedule
    change.
    """
    return {
        mode: {
            "reference": rules.reference.astimezone(UTC).isoformat(),
            "interval": rules.interval.total_seconds(),
            "order": list(rules.order),
        }
        for mode, rules in sorted(modes.items())
    }


def rotation_digest(modes: Mapping[str, RotationModeRules]) -> str:
    encoded = json.dumps(
        canonical_rotation_payload(modes),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RotationRulesError("pvp_rules_invalid", f"{label} 必须是对象")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise RotationRulesError("pvp_rules_invalid", f"{label} 的键必须是字符串")
    return cast(dict[str, object], value)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RotationRulesError("pvp_rules_invalid", f"{label} 必须是非空字符串")
    return value


def _parse_interval(value: object, label: str) -> timedelta:
    text = _require_string(value, label)
    match = _DURATION.match(text)
    if match is None:
        raise RotationRulesError(
            "pvp_rules_invalid", f"{label} 不是 ISO-8601 时长: {text!r}"
        )
    parts = {key: int(item or 0) for key, item in match.groupdict().items()}
    interval = timedelta(**parts)
    if interval <= timedelta(0):
        raise RotationRulesError("pvp_rules_invalid", f"{label} 必须为正时长")
    return interval


def _parse_reference(value: object, label: str) -> datetime:
    text = _require_string(value, label)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RotationRulesError(
            "pvp_rules_invalid", f"{label} 不是 ISO-8601 时间: {text!r}"
        ) from exc
    if moment.tzinfo is None:
        raise RotationRulesError("pvp_rules_invalid", f"{label} 必须带时区偏移")
    return moment.astimezone(UTC)


def _parse_modes(raw: object) -> dict[str, RotationModeRules]:
    modes_raw = _require_object(raw, "rotation")
    if not modes_raw:
        raise RotationRulesError("pvp_rules_invalid", "rotation 不能为空")
    modes: dict[str, RotationModeRules] = {}
    for mode, body in sorted(modes_raw.items()):
        label = f"rotation.{mode}"
        entry = _require_object(body, label)
        reference = _parse_reference(entry.get("reference"), f"{label}.reference")
        interval = _parse_interval(entry.get("interval"), f"{label}.interval")
        order_raw = entry.get("order")
        if not isinstance(order_raw, list) or not order_raw:
            raise RotationRulesError(
                "pvp_rules_invalid", f"{label}.order 必须是非空数组"
            )
        order = tuple(
            _require_string(item, f"{label}.order[{index}]")
            for index, item in enumerate(cast(Sequence[object], order_raw))
        )
        modes[mode] = RotationModeRules(
            reference=reference, interval=interval, order=order
        )
    return modes


def _parse_names(raw: object, modes: Mapping[str, RotationModeRules]) -> dict[str, str]:
    maps_raw = _require_object(raw, "maps")
    names: dict[str, str] = {}
    for map_id, body in sorted(maps_raw.items()):
        entry = _require_object(body, f"maps.{map_id}")
        names[map_id] = _require_string(entry.get("name"), f"maps.{map_id}.name")
    missing = sorted(
        {map_id for rules in modes.values() for map_id in rules.order} - set(names)
    )
    if missing:
        raise RotationRulesError(
            "pvp_rules_invalid",
            "maps 缺少轮换中出现的地图: " + ", ".join(missing),
        )
    return names


def parse_rotation_rules(raw: bytes, *, origin: str) -> RotationRulesBundle:
    try:
        decoded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RotationRulesError(
            "pvp_rules_invalid", f"规则快照不是合法 JSON: {origin}"
        ) from exc

    payload = _require_object(decoded, "规则快照")
    schema_version = payload.get("schemaVersion")
    if schema_version != SCHEMA_VERSION:
        raise RotationRulesError(
            "pvp_rules_schema_unsupported",
            f"规则快照 schemaVersion={schema_version!r}，本版本仅支持 {SCHEMA_VERSION}",
        )
    digest_raw = _require_string(payload.get("rotationDigest"), "rotationDigest")
    if _SHA256.fullmatch(digest_raw) is None:
        raise RotationRulesError(
            "pvp_rules_invalid", "rotationDigest 必须是小写 SHA-256"
        )

    modes = _parse_modes(payload.get("rotation"))
    names = _parse_names(payload.get("maps"), modes)
    computed = rotation_digest(modes)
    if computed != digest_raw:
        raise RotationRulesError(
            "pvp_rules_digest_mismatch",
            f"规则快照的 rotationDigest 与实际轮换内容不符，文件可能被手工改动（{origin}）",
        )

    source_url = payload.get("sourceUrl")
    if source_url is not None:
        source_url = _require_string(source_url, "sourceUrl")
    verified_at = payload.get("verifiedAt")
    if verified_at is not None:
        verified_at = _require_string(verified_at, "verifiedAt")

    return RotationRulesBundle(
        schema_version=SCHEMA_VERSION,
        source_url=source_url,
        verified_at=verified_at,
        rotation_digest=computed,
        modes=modes,
        names=names,
    )


def _load_bundle(path: Path, *, code: str) -> RotationRulesBundle:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RotationRulesError(code, f"读取轮换规则失败: {path}") from exc
    try:
        return parse_rotation_rules(raw, origin=str(path))
    except RotationRulesError as exc:
        raise RotationRulesError(code, str(exc)) from exc


def load_configured_rules(path: Path) -> RotationRulesBundle:
    resolved = Path(path).expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise RotationRulesError(
            "custom_pvp_rules_missing", f"自定义轮换规则不存在: {resolved}"
        )
    return _load_bundle(resolved, code="custom_pvp_rules_invalid")


def load_bundled_rules() -> RotationRulesBundle:
    return _load_bundle(bundled_rules_path(), code="bundled_pvp_rules_invalid")


@dataclass(frozen=True, slots=True)
class ResolvedRotationRules:
    bundle: RotationRulesBundle
    mode: Literal["bundled", "custom"]
    configured: bool


def resolve_rotation_rules(configured: str | None) -> ResolvedRotationRules:
    """Resolve the active snapshot: an explicit override wins, else the bundle."""
    if configured and configured.strip():
        if configured.strip() == BUNDLED_RULES:
            return ResolvedRotationRules(load_bundled_rules(), "bundled", False)
        return ResolvedRotationRules(load_configured_rules(Path(configured)), "custom", True)
    return ResolvedRotationRules(load_bundled_rules(), "bundled", False)


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RotationRulesError(
            "invalid_pvp_query",
            f"未知时区 {name!r}；请使用 IANA 名称，例如 Asia/Shanghai。",
        ) from exc


def host_for_url(url: str) -> str | None:
    return urlsplit(url).hostname


__all__ = [
    "BUNDLED_RULES",
    "BUNDLED_RULES_NAME",
    "SCHEMA_VERSION",
    "ResolvedRotationRules",
    "RotationMode",
    "RotationModeRules",
    "RotationRulesBundle",
    "RotationRulesError",
    "bundled_rules_path",
    "canonical_rotation_payload",
    "host_for_url",
    "load_bundled_rules",
    "load_configured_rules",
    "parse_rotation_rules",
    "resolve_rotation_rules",
    "resolve_timezone",
    "rotation_digest",
]
