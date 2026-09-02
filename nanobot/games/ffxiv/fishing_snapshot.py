"""Refresh, validate, and atomically promote local FishCake snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from .cache import atomic_write_json
from .fishcake import (
    FISHCAKE_HOSTS,
    DiscoveredAsset,
    discover_current_assets,
    normalize_fishcake_assets,
    serialize_fishing_snapshot,
)
from .http import FetchError, FetchResponse

_REVISION = re.compile(r"[A-Za-z0-9_-]+")
_ROLE_FILENAMES = {
    "data_json": "data-json.js",
    "fishing_spot": "fishingSpot.bin",
    "fish_bait_and_mooch": "fishBaitAndMooch.bin",
    "normal_fish": "normalFish.bin",
    "item": "item.bin",
    "item_name_chs": "itemNameCHS.bin",
    "place_name_chs": "placeNameCHS.bin",
}


class FishingSnapshotUnavailableError(RuntimeError):
    """No validated local fishing snapshot is available."""


class _FetchClient(Protocol):
    async def get_bytes(
        self, url: str, *, allowed_hosts: frozenset[str]
    ) -> FetchResponse: ...


@dataclass(frozen=True, slots=True)
class FishingSnapshotStatus:
    source_revision: str
    fetched_at: datetime
    source_updated_at: datetime | None
    state: Literal["fresh", "stale"]
    warnings: tuple[str, ...]
    data_path: Path


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot clock must return a timezone-aware datetime")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _source_updated_at(headers: Mapping[str, str]) -> datetime | None:
    value = next(
        (item for key, item in headers.items() if key.casefold() == "last-modified"),
        None,
    )
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _warning_text(metric: str, baseline: int, current: int) -> str:
    return f"{metric}: baseline={baseline}, current={current}"


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


class FishingSnapshotManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        client: _FetchClient,
        ttl: timedelta,
        clock: Callable[[], datetime],
        baseline: object,
    ) -> None:
        if ttl < timedelta(0):
            raise ValueError("snapshot TTL must not be negative")
        self._root = Path(data_dir).expanduser().resolve(strict=False) / "ffxiv" / "fishing"
        self._client = client
        self._ttl = ttl
        self._clock = clock
        self._baseline = baseline
        self._lock = asyncio.Lock()

    @property
    def current_path(self) -> Path:
        return self._root / "current.json"

    def _data_path(self, revision: str) -> Path:
        if _REVISION.fullmatch(revision) is None or revision in {".", ".."}:
            raise FishingSnapshotUnavailableError("invalid fishing snapshot revision")
        return self._root / "snapshots" / revision / "data.json"

    def _read_current(self) -> FishingSnapshotStatus | None:
        try:
            parsed: object = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        data = cast(dict[str, object], parsed)
        revision = data.get("sourceRevision")
        fetched_at = _optional_datetime(data.get("fetchedAt"))
        source_updated_raw = data.get("sourceUpdatedAt")
        source_updated_at = _optional_datetime(source_updated_raw)
        warnings_raw = data.get("warnings")
        if (
            not isinstance(revision, str)
            or fetched_at is None
            or (source_updated_raw is not None and source_updated_at is None)
            or not isinstance(warnings_raw, list)
        ):
            return None
        warning_values = cast(list[object], warnings_raw)
        if not all(isinstance(item, str) for item in warning_values):
            return None
        try:
            data_path = self._data_path(revision)
        except FishingSnapshotUnavailableError:
            return None
        if not data_path.is_file():
            return None
        return FishingSnapshotStatus(
            source_revision=revision,
            fetched_at=fetched_at,
            source_updated_at=source_updated_at,
            state="fresh",
            warnings=tuple(cast(list[str], warning_values)),
            data_path=data_path,
        )

    def _is_fresh(self, status: FishingSnapshotStatus, now: datetime) -> bool:
        try:
            return now - status.fetched_at < self._ttl
        except TypeError:
            return False

    def _promote(self, status: FishingSnapshotStatus) -> None:
        atomic_write_json(
            self.current_path,
            {
                "schemaVersion": 1,
                "sourceRevision": status.source_revision,
                "fetchedAt": status.fetched_at.isoformat(),
                "sourceUpdatedAt": (
                    status.source_updated_at.isoformat()
                    if status.source_updated_at is not None
                    else None
                ),
                "warnings": list(status.warnings),
            },
        )

    async def _download_assets(
        self, assets: tuple[DiscoveredAsset, ...]
    ) -> dict[str, FetchResponse]:
        result: dict[str, FetchResponse] = {}
        for asset in assets:
            result[asset.role] = await self._client.get_bytes(
                asset.url, allowed_hosts=FISHCAKE_HOSTS
            )
        return result

    async def refresh(self, *, force: bool = False) -> FishingSnapshotStatus:
        now = _aware_now(self._clock)
        current = self._read_current()
        if not force and current is not None and self._is_fresh(current, now):
            return current

        async with self._lock:
            now = _aware_now(self._clock)
            current = self._read_current()
            if not force and current is not None and self._is_fresh(current, now):
                return current
            try:
                discovery = await discover_current_assets(self._client)
                data_path = self._data_path(discovery.source_revision)
                if current is not None and current.source_revision == discovery.source_revision:
                    refreshed = replace(
                        current,
                        fetched_at=now,
                        source_updated_at=(
                            _source_updated_at(discovery.home.headers)
                            or current.source_updated_at
                        ),
                        state="fresh",
                    )
                    self._promote(refreshed)
                    return refreshed
                responses = await self._download_assets(discovery.assets)
            except FetchError as exc:
                if current is not None:
                    return replace(current, state="stale")
                raise FishingSnapshotUnavailableError(str(exc)) from exc

            revision_root = data_path.parent
            raw_root = revision_root / "raw"
            _atomic_write_bytes(raw_root / "index.html", discovery.home.body)
            _atomic_write_bytes(raw_root / "app.js", discovery.script.body)
            for role, response in responses.items():
                filename = _ROLE_FILENAMES.get(role)
                if filename is None:
                    raise FishingSnapshotUnavailableError(
                        f"unknown FishCake asset role {role}"
                    )
                _atomic_write_bytes(raw_root / filename, response.body)
                if hashlib.sha256((raw_root / filename).read_bytes()).digest() != hashlib.sha256(
                    response.body
                ).digest():
                    raise OSError(f"FishCake asset hash verification failed for {filename}")

            normal_response = responses["normal_fish"]
            normalized = normalize_fishcake_assets(
                normal_fish=normal_response.body,
                data_json=responses["data_json"].body,
                fishing_spot=responses["fishing_spot"].body,
                fish_bait_and_mooch=responses["fish_bait_and_mooch"].body,
                item_name_chs=responses["item_name_chs"].body,
                place_name_chs=responses["place_name_chs"].body,
                source_revision=discovery.source_revision,
                source_sha256=hashlib.sha256(normal_response.body).hexdigest(),
                fetched_at=now,
                baseline=self._baseline,
            )
            normalized_payload: object = json.loads(serialize_fishing_snapshot(normalized))
            atomic_write_json(data_path, normalized_payload)
            warnings = tuple(
                _warning_text(item.metric, item.baseline, item.current)
                for item in normalized.drift_report.warnings
            )
            status = FishingSnapshotStatus(
                source_revision=discovery.source_revision,
                fetched_at=now,
                source_updated_at=_source_updated_at(discovery.home.headers),
                state="fresh",
                warnings=warnings,
                data_path=data_path,
            )
            self._promote(status)
            return status
