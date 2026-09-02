"""Conservative entity and alias extraction for Chinese guide sources."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from .knowledge_clean import CleanDocument

_SURROUNDING_PAIRS = {
    "《": "》",
    "〈": "〉",
    "【": "】",
    "[": "]",
    "“": "”",
    "‘": "’",
    '"': '"',
    "'": "'",
    "(": ")",
    "（": "）",
}
_PARENTHESIZED_HEADING = re.compile(r"^(.+?)[（(]([^（）()]+)[）)]$")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_ALIAS_SPLIT = re.compile(r"\s*(?:、|/|,|，|;|；)\s*")
_NAME_HEADERS = frozenset({"正式名称", "名称", "标准名称", "词条"})
_ALIAS_HEADERS = frozenset({"别名", "俗称", "简称", "缩写"})


@dataclass(frozen=True, slots=True)
class ExplicitAlias:
    alias: str
    normalized_alias: str
    source: str


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    kind: str
    canonical_name: str
    normalized_name: str
    aliases: tuple[ExplicitAlias, ...]


def normalize_entity_name(value: str) -> str:
    """Normalize lookup text without erasing Chinese characters."""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split()).casefold()
    while len(normalized) >= 2 and _SURROUNDING_PAIRS.get(normalized[0]) == normalized[-1]:
        normalized = normalized[1:-1].strip()
    return normalized


def _front_matter(raw_text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, ()
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, ()

    values: dict[str, str] = {}
    aliases: list[str] = []
    collecting_aliases = False
    for line in lines[1:end]:
        stripped = line.strip()
        if collecting_aliases and stripped.startswith("-"):
            alias = stripped[1:].strip().strip("\"'")
            if alias:
                aliases.append(alias)
            continue
        collecting_aliases = False
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().casefold()
        value = value.strip()
        if key == "aliases":
            if not value:
                collecting_aliases = True
            elif value.startswith("[") and value.endswith("]"):
                aliases.extend(
                    item.strip().strip("\"'")
                    for item in value[1:-1].split(",")
                    if item.strip().strip("\"'")
                )
            else:
                aliases.extend(
                    item for item in _ALIAS_SPLIT.split(value.strip("\"'")) if item
                )
        else:
            values[key] = value.strip("\"'")
    return values, tuple(aliases)


def _table_cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _table_entities(raw_text: str) -> list[tuple[str, tuple[str, ...]]]:
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result: list[tuple[str, tuple[str, ...]]] = []
    index = 0
    while index + 1 < len(lines):
        headers = _table_cells(lines[index])
        separators = _table_cells(lines[index + 1])
        if (
            "|" not in lines[index]
            or len(headers) != len(separators)
            or not all(_TABLE_SEPARATOR.fullmatch(cell) for cell in separators)
        ):
            index += 1
            continue
        name_column = next(
            (position for position, value in enumerate(headers) if value in _NAME_HEADERS),
            None,
        )
        alias_column = next(
            (position for position, value in enumerate(headers) if value in _ALIAS_HEADERS),
            None,
        )
        index += 2
        while index < len(lines) and "|" in lines[index]:
            cells = _table_cells(lines[index])
            if (
                name_column is not None
                and alias_column is not None
                and max(name_column, alias_column) < len(cells)
            ):
                canonical = cells[name_column]
                aliases = tuple(
                    item for item in _ALIAS_SPLIT.split(cells[alias_column]) if item
                )
                if canonical and aliases:
                    result.append((canonical, aliases))
            index += 1
    return result


def extract_entities(
    raw_text: str,
    document: CleanDocument,
) -> tuple[ExtractedEntity, ...]:
    """Extract only entities and aliases backed by explicit source syntax."""
    metadata, metadata_aliases = _front_matter(raw_text)
    default_kind = metadata.get("kind", "guide").strip().casefold() or "guide"
    candidates: list[tuple[str, str, tuple[str, ...], str]] = []
    if document.title.strip():
        candidates.append(
            (default_kind, document.title.strip(), metadata_aliases, "front_matter")
        )
    for block in document.blocks:
        if block.kind != "heading":
            continue
        match = _PARENTHESIZED_HEADING.fullmatch(block.text.strip())
        if match is not None:
            candidates.append(
                (default_kind, match.group(1).strip(), (match.group(2).strip(),), "heading")
            )
    candidates.extend(
        (default_kind, canonical, aliases, "alias_table")
        for canonical, aliases in _table_entities(raw_text)
    )

    merged: dict[tuple[str, str], tuple[str, dict[str, ExplicitAlias]]] = {}
    for kind, canonical, aliases, source in candidates:
        normalized_name = normalize_entity_name(canonical)
        if not normalized_name:
            continue
        key = (kind, normalized_name)
        if key not in merged:
            merged[key] = (canonical, {})
        alias_map = merged[key][1]
        for alias in aliases:
            normalized_alias = normalize_entity_name(alias)
            if not normalized_alias or normalized_alias == normalized_name:
                continue
            alias_map.setdefault(
                normalized_alias,
                ExplicitAlias(alias.strip(), normalized_alias, source),
            )

    return tuple(
        ExtractedEntity(
            kind=kind,
            canonical_name=canonical,
            normalized_name=normalized_name,
            aliases=tuple(alias_map[key] for key in sorted(alias_map)),
        )
        for (kind, normalized_name), (canonical, alias_map) in sorted(merged.items())
    )


def store_entities(
    connection: sqlite3.Connection,
    entities: tuple[ExtractedEntity, ...],
    chunk_ids: tuple[int, ...],
) -> tuple[int, ...]:
    """Merge entities into the index and link every supporting document chunk."""
    entity_ids: list[int] = []
    for entity in entities:
        connection.execute(
            """
            INSERT OR IGNORE INTO entities(kind, canonical_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (entity.kind, entity.canonical_name, entity.normalized_name),
        )
        row = connection.execute(
            "SELECT id FROM entities WHERE kind = ? AND normalized_name = ?",
            (entity.kind, entity.normalized_name),
        ).fetchone()
        if row is None:
            continue
        entity_id = int(row[0])
        entity_ids.append(entity_id)
        connection.executemany(
            """
            INSERT OR IGNORE INTO aliases(entity_id, alias, normalized_alias)
            VALUES (?, ?, ?)
            """,
            (
                (entity_id, alias.alias, alias.normalized_alias)
                for alias in entity.aliases
            ),
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO entity_chunks(entity_id, chunk_id)
            VALUES (?, ?)
            """,
            ((entity_id, chunk_id) for chunk_id in chunk_ids),
        )
    return tuple(entity_ids)
