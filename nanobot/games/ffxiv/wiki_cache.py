"""Bounded SQLite cache for normalized remote Wiki and tool-directory pages."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias, cast

WIKI_TTL = timedelta(days=1)
TOOL_DIRECTORY_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class RemotePage:
    source: str
    canonical_title: str
    language: str
    source_url: str
    fetched_at: datetime
    source_revision: str | None
    etag: str | None
    normalized_text: str
    raw_size_bytes: int
    facts: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WikiCacheHit:
    page: RemotePage
    stale: bool


_CacheKey: TypeAlias = tuple[str, str, str]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("cache timestamps must include a timezone")
    return value.astimezone(UTC)


class WikiCache:
    def __init__(self, database: Path, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.database = Path(database).expanduser().resolve(strict=False)
        self.max_bytes = max_bytes
        self._lock = asyncio.Lock()
        self._pins: set[_CacheKey] = set()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    source TEXT NOT NULL,
                    canonical_title TEXT NOT NULL,
                    language TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    source_revision TEXT,
                    etag TEXT,
                    normalized_text TEXT NOT NULL,
                    raw_size_bytes INTEGER NOT NULL,
                    facts_json TEXT NOT NULL,
                    stored_bytes INTEGER NOT NULL,
                    PRIMARY KEY(source, canonical_title, language)
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_pages_accessed
                    ON wiki_pages(accessed_at);
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _key(source: str, canonical_title: str, language: str) -> _CacheKey:
        return source, canonical_title, language

    @staticmethod
    def _ttl(source: str) -> timedelta:
        return TOOL_DIRECTORY_TTL if source == "tool_directory" else WIKI_TTL

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> RemotePage | None:
        try:
            facts = json.loads(row[10])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(facts, dict):
            return None
        object_facts = cast(dict[object, object], facts)
        if not all(isinstance(key, str) for key in object_facts):
            return None
        typed_facts = cast(dict[str, object], object_facts)
        return RemotePage(
            source=row[0],
            canonical_title=row[1],
            language=row[2],
            source_url=row[3],
            fetched_at=datetime.fromtimestamp(row[4], UTC),
            source_revision=row[6],
            etag=row[7],
            normalized_text=row[8],
            raw_size_bytes=row[9],
            facts=typed_facts,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _get_locked(
        self,
        key: _CacheKey,
        *,
        now: datetime,
        refresh_access: bool,
    ) -> WikiCacheHit | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT source, canonical_title, language, source_url,
                       fetched_at, accessed_at, source_revision, etag,
                       normalized_text, raw_size_bytes, facts_json, stored_bytes
                FROM wiki_pages
                WHERE source = ? AND canonical_title = ? AND language = ?
                """,
                key,
            ).fetchone()
            if row is None:
                return None
            page = self._deserialize(row)
            if page is None:
                connection.execute(
                    """
                    DELETE FROM wiki_pages
                    WHERE source = ? AND canonical_title = ? AND language = ?
                    """,
                    key,
                )
                connection.commit()
                return None
            if refresh_access:
                connection.execute(
                    """
                    UPDATE wiki_pages SET accessed_at = ?
                    WHERE source = ? AND canonical_title = ? AND language = ?
                    """,
                    (_utc(now).timestamp(), *key),
                )
                connection.commit()
            stale = _utc(now) - page.fetched_at >= self._ttl(page.source)
            return WikiCacheHit(page=page, stale=stale)
        finally:
            connection.close()

    async def get(
        self,
        source: str,
        canonical_title: str,
        language: str,
        *,
        now: datetime | None = None,
    ) -> WikiCacheHit | None:
        async with self._lock:
            return self._get_locked(
                self._key(source, canonical_title, language),
                now=now or datetime.now(UTC),
                refresh_access=True,
            )

    async def peek(
        self,
        source: str,
        canonical_title: str,
        language: str,
    ) -> WikiCacheHit | None:
        async with self._lock:
            return self._get_locked(
                self._key(source, canonical_title, language),
                now=datetime.now(UTC),
                refresh_access=False,
            )

    def _evict_locked(self, connection: sqlite3.Connection) -> None:
        while True:
            total = connection.execute(
                "SELECT COALESCE(sum(stored_bytes), 0) FROM wiki_pages"
            ).fetchone()[0]
            if int(total) <= self.max_bytes:
                return
            candidates = connection.execute(
                """
                SELECT source, canonical_title, language
                FROM wiki_pages ORDER BY accessed_at, source, canonical_title, language
                """
            ).fetchall()
            victim = next(
                (tuple(row) for row in candidates if tuple(row) not in self._pins),
                None,
            )
            if victim is None:
                return
            connection.execute(
                """
                DELETE FROM wiki_pages
                WHERE source = ? AND canonical_title = ? AND language = ?
                """,
                victim,
            )

    async def put(
        self,
        page: RemotePage,
        *,
        now: datetime | None = None,
    ) -> None:
        if page.raw_size_bytes < 0:
            raise ValueError("raw_size_bytes must not be negative")
        fetched_at = _utc(page.fetched_at)
        accessed_at = _utc(now or datetime.now(UTC))
        facts_json = json.dumps(
            dict(page.facts),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        stored_bytes = len(page.normalized_text.encode("utf-8")) + len(
            facts_json.encode("utf-8")
        )
        async with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        source, canonical_title, language, source_url,
                        fetched_at, accessed_at, source_revision, etag,
                        normalized_text, raw_size_bytes, facts_json, stored_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, canonical_title, language) DO UPDATE SET
                        source_url = excluded.source_url,
                        fetched_at = excluded.fetched_at,
                        accessed_at = excluded.accessed_at,
                        source_revision = excluded.source_revision,
                        etag = excluded.etag,
                        normalized_text = excluded.normalized_text,
                        raw_size_bytes = excluded.raw_size_bytes,
                        facts_json = excluded.facts_json,
                        stored_bytes = excluded.stored_bytes
                    """,
                    (
                        page.source,
                        page.canonical_title,
                        page.language,
                        page.source_url,
                        fetched_at.timestamp(),
                        accessed_at.timestamp(),
                        page.source_revision,
                        page.etag,
                        page.normalized_text,
                        page.raw_size_bytes,
                        facts_json,
                        stored_bytes,
                    ),
                )
                self._evict_locked(connection)
                connection.commit()
            finally:
                connection.close()

    async def total_bytes(self) -> int:
        async with self._lock:
            connection = self._connect()
            try:
                return int(
                    connection.execute(
                        "SELECT COALESCE(sum(stored_bytes), 0) FROM wiki_pages"
                    ).fetchone()[0]
                )
            finally:
                connection.close()

    async def accessed_at(
        self,
        source: str,
        canonical_title: str,
        language: str,
    ) -> datetime | None:
        async with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT accessed_at FROM wiki_pages
                    WHERE source = ? AND canonical_title = ? AND language = ?
                    """,
                    self._key(source, canonical_title, language),
                ).fetchone()
                return datetime.fromtimestamp(row[0], UTC) if row is not None else None
            finally:
                connection.close()

    @asynccontextmanager
    async def pinned(
        self,
        source: str,
        canonical_title: str,
        language: str,
        *,
        now: datetime | None = None,
    ) -> AsyncGenerator[WikiCacheHit | None, None]:
        key = self._key(source, canonical_title, language)
        async with self._lock:
            hit = self._get_locked(
                key,
                now=now or datetime.now(UTC),
                refresh_access=True,
            )
            if hit is not None:
                self._pins.add(key)
        try:
            yield hit
        finally:
            if hit is not None:
                async with self._lock:
                    self._pins.discard(key)
