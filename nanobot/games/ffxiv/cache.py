"""Atomic JSON cache records for FF14 data sources."""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

_CACHE_KEY = re.compile(r"[A-Za-z0-9._-]+")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except (OSError, NotImplementedError):
        return
    try:
        with suppress(OSError, NotImplementedError):
            os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, value: object) -> None:
    """Serialize *value* as deterministic UTF-8 JSON and atomically replace *path*."""
    content = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class CacheRecord:
    fetched_at: datetime
    source_updated_at: datetime | None
    etag: str | None
    payload: object


@dataclass(frozen=True, slots=True)
class CacheHit:
    record: CacheRecord
    stale: bool


class JsonCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)

    def _path_for(self, key: str) -> Path:
        if (
            not key
            or key in {".", ".."}
            or _CACHE_KEY.fullmatch(key) is None
            or Path(key).name != key
        ):
            raise ValueError("invalid cache key")
        path = (self.root / f"{key}.json").resolve(strict=False)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("invalid cache key") from exc
        return path

    def write(self, key: str, record: CacheRecord) -> None:
        value = {
            "fetchedAt": record.fetched_at.isoformat(),
            "sourceUpdatedAt": (
                record.source_updated_at.isoformat()
                if record.source_updated_at is not None
                else None
            ),
            "etag": record.etag,
            "payload": record.payload,
        }
        atomic_write_json(self._path_for(key), value)

    def read(
        self,
        key: str,
        *,
        ttl: timedelta,
        now: datetime,
        allow_stale: bool,
    ) -> CacheHit | None:
        if ttl < timedelta(0):
            raise ValueError("ttl must not be negative")
        try:
            parsed: object = json.loads(self._path_for(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        data = cast(dict[str, object], parsed)
        if "payload" not in data:
            return None

        fetched_at = self._datetime_value(data.get("fetchedAt"))
        source_updated_at_valid, source_updated_at = self._optional_datetime_value(
            data.get("sourceUpdatedAt")
        )
        etag = data.get("etag")
        if fetched_at is None or not source_updated_at_valid:
            return None
        if etag is not None and not isinstance(etag, str):
            return None
        try:
            stale = now - fetched_at >= ttl
        except TypeError:
            return None
        if stale and not allow_stale:
            return None
        return CacheHit(
            record=CacheRecord(
                fetched_at=fetched_at,
                source_updated_at=source_updated_at,
                etag=etag,
                payload=data["payload"],
            ),
            stale=stale,
        )

    @staticmethod
    def _datetime_value(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    @classmethod
    def _optional_datetime_value(cls, value: object) -> tuple[bool, datetime | None]:
        if value is None:
            return True, None
        parsed = cls._datetime_value(value)
        return parsed is not None, parsed
