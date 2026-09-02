"""Evidence-only retrieval for the local Chinese guide database."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from .knowledge_entities import normalize_entity_name

RankReason = Literal["exact", "alias", "heading", "body"]
_QUERY_TERM = re.compile(r"[0-9a-z_\u3400-\u9fff]+", flags=re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    text: str
    source_path: str
    heading_path: tuple[str, ...]
    source_line_start: int
    source_line_end: int
    rank_reason: RankReason
    score: float | None
    entity_name: str | None
    chunk_id: int
    ordinal_start: int
    ordinal_end: int


def _read_only_connection(database: Path) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve(strict=True)
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _heading_path(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    values = cast(list[object], parsed)
    return tuple(item for item in values if isinstance(item, str))


def _hit(
    row: tuple[int, int, str, str, int, int, str],
    *,
    reason: RankReason,
    score: float | None,
    entity_name: str | None,
) -> KnowledgeSearchHit:
    chunk_id, ordinal, source_path, heading, line_start, line_end, text = row
    return KnowledgeSearchHit(
        text=text,
        source_path=source_path,
        heading_path=_heading_path(heading),
        source_line_start=line_start,
        source_line_end=line_end,
        rank_reason=reason,
        score=score,
        entity_name=entity_name,
        chunk_id=chunk_id,
        ordinal_start=ordinal,
        ordinal_end=ordinal,
    )


def _exact_hits(
    connection: sqlite3.Connection,
    normalized_query: str,
    limit: int,
) -> list[KnowledgeSearchHit]:
    hits: list[KnowledgeSearchHit] = []
    query = """
        SELECT c.id, c.ordinal, d.source_path, c.heading_path,
               c.source_line_start, c.source_line_end, c.text,
               e.canonical_name
        FROM entities AS e
        JOIN entity_chunks AS ec ON ec.entity_id = e.id
        JOIN chunks AS c ON c.id = ec.chunk_id
        JOIN documents AS d ON d.id = c.document_id
        WHERE {condition}
        ORDER BY e.canonical_name, d.source_path, c.ordinal
        LIMIT ?
    """
    lookups: tuple[tuple[str, RankReason], ...] = (
        ("e.normalized_name = ?", "exact"),
        (
            "EXISTS (SELECT 1 FROM aliases AS a "
            "WHERE a.entity_id = e.id AND a.normalized_alias = ?)",
            "alias",
        ),
    )
    for condition, reason in lookups:
        for row in connection.execute(
            query.format(condition=condition),
            (normalized_query, limit),
        ):
            hits.append(
                _hit(
                    cast(tuple[int, int, str, str, int, int, str], row[:7]),
                    reason=reason,
                    score=None,
                    entity_name=str(row[7]),
                )
            )
        if hits:
            break
    return hits


def _fts_expression(normalized_query: str) -> str | None:
    terms = tuple(
        match.group(0)
        for match in _QUERY_TERM.finditer(normalized_query)
        if len(match.group(0)) >= 3
    )
    if not terms:
        return None
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _short_term_hits(
    connection: sqlite3.Connection,
    normalized_query: str,
    limit: int,
) -> list[KnowledgeSearchHit]:
    terms = tuple(
        dict.fromkeys(
            match.group(0)
            for match in _QUERY_TERM.finditer(normalized_query)
            if len(match.group(0)) == 2
        )
    )
    if not terms:
        return []
    condition = " OR ".join("(c.text LIKE ? OR c.heading_path LIKE ?)" for _ in terms)
    parameters = tuple(value for term in terms for value in (f"%{term}%", f"%{term}%"))
    rows = connection.execute(
        f"""
        SELECT c.id, c.ordinal, d.source_path, c.heading_path,
               c.source_line_start, c.source_line_end, c.text, d.title
        FROM chunks AS c
        JOIN documents AS d ON d.id = c.document_id
        WHERE {condition}
        """,
        parameters,
    )
    ranked: list[tuple[int, int, int, KnowledgeSearchHit]] = []
    for row in rows:
        heading = normalize_entity_name(str(row[3]))
        text = normalize_entity_name(str(row[6]))
        title = normalize_entity_name(str(row[7]))
        title_matches = sum(term in title for term in terms)
        heading_matches = sum(term in heading for term in terms)
        matches = sum(term in heading or term in text for term in terms)
        ranked.append(
            (
                title_matches,
                heading_matches,
                matches,
                _hit(
                    cast(tuple[int, int, str, str, int, int, str], row[:7]),
                    reason="heading" if heading_matches else "body",
                    score=-float(matches),
                    entity_name=None,
                ),
            )
        )
    ranked.sort(
        key=lambda item: (
            -bool(item[0]),
            -bool(item[1]),
            -item[2],
            item[3].chunk_id,
        )
    )
    return [item[3] for item in ranked[:limit]]


def _fts_hits(
    connection: sqlite3.Connection,
    normalized_query: str,
    limit: int,
) -> list[KnowledgeSearchHit]:
    expression = _fts_expression(normalized_query)
    if expression is None:
        return _short_term_hits(connection, normalized_query, limit)
    terms = tuple(match.group(0) for match in _QUERY_TERM.finditer(normalized_query))
    rows = connection.execute(
        """
        SELECT c.id, c.ordinal, d.source_path, c.heading_path,
               c.source_line_start, c.source_line_end, c.text,
               bm25(chunks_fts, 1.0, 3.0) AS score
        FROM chunks_fts
        JOIN chunks AS c ON c.id = chunks_fts.rowid
        JOIN documents AS d ON d.id = c.document_id
        WHERE chunks_fts MATCH ?
        ORDER BY score, c.id
        LIMIT ?
        """,
        (expression, limit),
    )
    hits: list[KnowledgeSearchHit] = []
    for row in rows:
        heading = normalize_entity_name(str(row[3]))
        reason: RankReason = (
            "heading"
            if any(term in heading for term in terms if len(term) >= 3)
            else "body"
        )
        hits.append(
            _hit(
                cast(tuple[int, int, str, str, int, int, str], row[:7]),
                reason=reason,
                score=float(row[7]),
                entity_name=None,
            )
        )
    hits.sort(
        key=lambda item: (
            0 if item.rank_reason == "heading" else 1,
            item.score if item.score is not None else 0.0,
            item.chunk_id,
        )
    )
    return hits


def _merge_adjacent(
    hits: list[KnowledgeSearchHit],
    max_chars: int,
) -> tuple[KnowledgeSearchHit, ...]:
    merged: list[KnowledgeSearchHit] = []
    for hit in hits:
        for index, previous in enumerate(merged):
            if (
                previous.source_path == hit.source_path
                and previous.rank_reason == hit.rank_reason
                and previous.entity_name == hit.entity_name
                and hit.ordinal_start == previous.ordinal_end + 1
            ):
                combined = f"{previous.text}\n\n{hit.text}"[:max_chars]
                merged[index] = replace(
                    previous,
                    text=combined,
                    source_line_end=max(previous.source_line_end, hit.source_line_end),
                    ordinal_end=hit.ordinal_end,
                    score=min(
                        value
                        for value in (previous.score, hit.score)
                        if value is not None
                    )
                    if previous.score is not None or hit.score is not None
                    else None,
                )
                break
            if (
                previous.source_path == hit.source_path
                and previous.rank_reason == hit.rank_reason
                and previous.entity_name == hit.entity_name
                and hit.ordinal_end + 1 == previous.ordinal_start
            ):
                combined = f"{hit.text}\n\n{previous.text}"[:max_chars]
                merged[index] = replace(
                    previous,
                    text=combined,
                    source_line_start=min(previous.source_line_start, hit.source_line_start),
                    ordinal_start=hit.ordinal_start,
                    score=min(
                        value
                        for value in (previous.score, hit.score)
                        if value is not None
                    )
                    if previous.score is not None or hit.score is not None
                    else None,
                )
                break
        else:
            merged.append(replace(hit, text=hit.text[:max_chars]))
    return tuple(merged)


def search_knowledge(
    database: Path,
    query: str,
    *,
    limit: int = 8,
    max_chars: int = 4000,
) -> tuple[KnowledgeSearchHit, ...]:
    """Return ranked source evidence without synthesizing an answer."""
    if limit < 1 or max_chars < 1:
        raise ValueError("limit and max_chars must be positive")
    normalized_query = normalize_entity_name(query)
    if not normalized_query:
        return ()
    connection = _read_only_connection(database)
    try:
        exact = _exact_hits(connection, normalized_query, limit)
        seen_chunks = {item.chunk_id for item in exact}
        remaining = max(0, limit - len(exact))
        fts = [
            item
            for item in _fts_hits(connection, normalized_query, limit)
            if item.chunk_id not in seen_chunks
        ][:remaining]
        return _merge_adjacent([*exact, *fts], max_chars)
    finally:
        connection.close()
