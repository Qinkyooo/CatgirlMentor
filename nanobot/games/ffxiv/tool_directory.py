"""Water Crystal Station tool-directory adapter with local filtering only."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.parse import urlsplit

from .http import FetchError, FetchResponse
from .wiki_cache import RemotePage, WikiCache, WikiCacheHit

DIRECTORY_URL = "https://ff14.bluefissure.com/"
DIRECTORY_HOSTS = frozenset({"ff14.bluefissure.com"})
DIRECTORY_CACHE_TITLE = "index"
DIRECTORY_MAX_BYTES = 2 * 1024 * 1024
_QUERY_TERM = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]{2,}", flags=re.IGNORECASE)


class _HttpClient(Protocol):
    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        etag: str | None = None,
    ) -> FetchResponse: ...


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    url: str
    category: str
    description: str


@dataclass(frozen=True, slots=True)
class DirectoryLookupResult:
    entries: tuple[DirectoryEntry, ...]
    fetched_at: datetime | None
    stale: bool
    warnings: tuple[str, ...]
    unavailable: bool


class _DirectoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[DirectoryEntry] = []
        self._category = ""
        self._category_buffer: list[str] | None = None
        self._card_url: str | None = None
        self._name_buffer: list[str] | None = None
        self._description_buffer: list[str] | None = None
        self._card_name = ""
        self._card_description = ""

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        value = dict(attrs).get("class") or ""
        return set(value.split())

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lowered = tag.casefold()
        if lowered == "h4":
            self._category_buffer = []
        elif lowered == "a" and "item-card-a" in self._classes(attrs):
            self._card_url = dict(attrs).get("href")
            self._card_name = ""
            self._card_description = ""
        elif self._card_url is not None and lowered == "strong":
            self._name_buffer = []
        elif self._card_url is not None and lowered == "p":
            self._description_buffer = []

    def handle_data(self, data: str) -> None:
        if self._category_buffer is not None:
            self._category_buffer.append(data)
        if self._name_buffer is not None:
            self._name_buffer.append(data)
        if self._description_buffer is not None:
            self._description_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "h4" and self._category_buffer is not None:
            self._category = " ".join("".join(self._category_buffer).split())
            self._category_buffer = None
        elif lowered == "strong" and self._name_buffer is not None:
            self._card_name = " ".join("".join(self._name_buffer).split())
            self._name_buffer = None
        elif lowered == "p" and self._description_buffer is not None:
            self._card_description = " ".join(
                "".join(self._description_buffer).split()
            )
            self._description_buffer = None
        elif lowered == "a" and self._card_url is not None:
            parsed = urlsplit(self._card_url)
            if (
                parsed.scheme.casefold() in {"http", "https"}
                and parsed.netloc
                and self._category
                and self._card_name
                and self._card_description
            ):
                self.entries.append(
                    DirectoryEntry(
                        name=self._card_name,
                        url=self._card_url,
                        category=self._category,
                        description=self._card_description,
                    )
                )
            self._card_url = None
            self._name_buffer = None
            self._description_buffer = None


def parse_directory_html(body: bytes) -> tuple[DirectoryEntry, ...]:
    parser = _DirectoryParser()
    parser.feed(body.decode("utf-8-sig"))
    parser.close()
    return tuple(parser.entries)


def _facts(entries: tuple[DirectoryEntry, ...]) -> dict[str, object]:
    return {
        "entries": [
            {
                "name": item.name,
                "url": item.url,
                "category": item.category,
                "description": item.description,
            }
            for item in entries
        ]
    }


def _cached_entries(hit: WikiCacheHit | None) -> tuple[DirectoryEntry, ...]:
    if hit is None:
        return ()
    raw_entries = hit.page.facts.get("entries")
    if not isinstance(raw_entries, list):
        return ()
    entries: list[DirectoryEntry] = []
    for raw in cast(list[object], raw_entries):
        if not isinstance(raw, Mapping):
            return ()
        values = cast(Mapping[object, object], raw)
        fields = tuple(values.get(key) for key in ("name", "url", "category", "description"))
        if not all(isinstance(value, str) and value for value in fields):
            return ()
        name, url, category, description = cast(tuple[str, str, str, str], fields)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ()
        entries.append(DirectoryEntry(name, url, category, description))
    return tuple(entries)


def _filter(
    entries: tuple[DirectoryEntry, ...], query: str
) -> tuple[DirectoryEntry, ...]:
    terms = tuple(match.group(0).casefold() for match in _QUERY_TERM.finditer(query))
    if not terms:
        return ()
    return tuple(
        item
        for item in entries
        if any(
            term
            in " ".join(
                (item.name, item.category, item.description, item.url)
            ).casefold()
            for term in terms
        )
    )


class WaterCrystalDirectory:
    def __init__(self, *, http: _HttpClient, cache: WikiCache) -> None:
        self._http = http
        self._cache = cache

    async def lookup(
        self,
        query: str,
        *,
        now: datetime | None = None,
    ) -> DirectoryLookupResult:
        current = now or datetime.now(UTC)
        cached = await self._cache.get(
            "tool_directory", DIRECTORY_CACHE_TITLE, "zh", now=current
        )
        cached_entries = _cached_entries(cached)
        if cached is not None and not cached.stale and cached_entries:
            return DirectoryLookupResult(
                entries=_filter(cached_entries, query),
                fetched_at=cached.page.fetched_at,
                stale=False,
                warnings=(),
                unavailable=False,
            )

        try:
            response = await self._http.get_bytes(
                DIRECTORY_URL,
                allowed_hosts=DIRECTORY_HOSTS,
                max_bytes=DIRECTORY_MAX_BYTES,
                etag=cached.page.etag if cached is not None else None,
            )
            if response.status_code == 304 and cached is not None and cached_entries:
                page = RemotePage(
                    source=cached.page.source,
                    canonical_title=cached.page.canonical_title,
                    language=cached.page.language,
                    source_url=cached.page.source_url,
                    fetched_at=current,
                    source_revision=cached.page.source_revision,
                    etag=cached.page.etag,
                    normalized_text=cached.page.normalized_text,
                    raw_size_bytes=cached.page.raw_size_bytes,
                    facts=cached.page.facts,
                )
                entries = cached_entries
            else:
                entries = parse_directory_html(response.body)
                if not entries:
                    raise ValueError("directory contains no valid visible entries")
                page = RemotePage(
                    source="tool_directory",
                    canonical_title=DIRECTORY_CACHE_TITLE,
                    language="zh",
                    source_url=response.url,
                    fetched_at=current,
                    source_revision=None,
                    etag=response.headers.get("etag"),
                    normalized_text="\n".join(
                        f"{item.category}\t{item.name}\t{item.url}\t{item.description}"
                        for item in entries
                    ),
                    raw_size_bytes=len(response.body),
                    facts=_facts(entries),
                )
            await self._cache.put(page, now=current)
            return DirectoryLookupResult(
                entries=_filter(entries, query),
                fetched_at=current,
                stale=False,
                warnings=(),
                unavailable=False,
            )
        except (FetchError, UnicodeDecodeError, ValueError) as exc:
            if cached is not None and cached_entries:
                return DirectoryLookupResult(
                    entries=_filter(cached_entries, query),
                    fetched_at=cached.page.fetched_at,
                    stale=True,
                    warnings=(str(exc),),
                    unavailable=False,
                )
            return DirectoryLookupResult(
                entries=(),
                fetched_at=None,
                stale=False,
                warnings=(str(exc),),
                unavailable=True,
            )
