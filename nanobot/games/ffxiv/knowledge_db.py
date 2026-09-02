"""SQLite schema and explicit FTS maintenance for the Chinese guide index."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1
_PROBE_TABLE = "temp.__nanobot_fts5_trigram_probe"


class KnowledgeBuildError(RuntimeError):
    """Raised when the local knowledge index cannot be built safely."""


def require_fts5_trigram(connection: sqlite3.Connection) -> None:
    """Fail clearly when the runtime lacks the required Chinese tokenizer."""
    try:
        connection.execute(
            f"CREATE VIRTUAL TABLE {_PROBE_TABLE} "
            "USING fts5(text, tokenize='trigram')"
        )
    except sqlite3.Error as exc:
        raise KnowledgeBuildError(
            "当前 Python 的 SQLite 运行时缺少必需的 FTS5 trigram 支持，"
            "无法安全建立中文知识库索引。"
        ) from exc
    else:
        connection.execute(f"DROP TABLE {_PROBE_TABLE}")


def create_schema(connection: sqlite3.Connection) -> None:
    """Create schema version 1 on a new database connection."""
    connection.execute("PRAGMA foreign_keys = ON")
    require_fts5_trigram(connection)
    connection.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );

        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL
                REFERENCES documents(id) ON DELETE CASCADE,
            chunk_key TEXT NOT NULL UNIQUE,
            ordinal INTEGER NOT NULL,
            heading_path TEXT NOT NULL,
            source_line_start INTEGER NOT NULL,
            source_line_end INTEGER NOT NULL,
            text TEXT NOT NULL
        );

        CREATE TABLE document_links (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL
                REFERENCES documents(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            destination TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            UNIQUE(document_id, label, destination, source_line)
        );

        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text,
            heading_path,
            content='chunks',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            UNIQUE(kind, normalized_name)
        );

        CREATE TABLE aliases (
            entity_id INTEGER NOT NULL
                REFERENCES entities(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            UNIQUE(entity_id, normalized_alias)
        );

        CREATE TABLE entity_chunks (
            entity_id INTEGER NOT NULL
                REFERENCES entities(id) ON DELETE CASCADE,
            chunk_id INTEGER NOT NULL
                REFERENCES chunks(id) ON DELETE CASCADE,
            PRIMARY KEY(entity_id, chunk_id)
        );

        CREATE TABLE build_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX idx_documents_sha256 ON documents(sha256);
        CREATE INDEX idx_chunks_document_ordinal
            ON chunks(document_id, ordinal);
        CREATE INDEX idx_document_links_document_line
            ON document_links(document_id, source_line);
        CREATE INDEX idx_aliases_normalized ON aliases(normalized_alias);
        """
    )
    connection.execute(
        "INSERT INTO build_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def insert_fts_chunk(
    connection: sqlite3.Connection,
    chunk_id: int,
    text: str,
    heading_path: str,
) -> None:
    """Insert one already-persisted chunk into the external-content index."""
    connection.execute(
        "INSERT INTO chunks_fts(rowid, text, heading_path) VALUES (?, ?, ?)",
        (chunk_id, text, heading_path),
    )


def delete_fts_chunk(
    connection: sqlite3.Connection,
    chunk_id: int,
    text: str,
    heading_path: str,
) -> None:
    """Delete one FTS row using the old external-content column values."""
    connection.execute(
        """
        INSERT INTO chunks_fts(chunks_fts, rowid, text, heading_path)
        VALUES ('delete', ?, ?, ?)
        """,
        (chunk_id, text, heading_path),
    )


def rebuild_fts(connection: sqlite3.Connection) -> None:
    """Rebuild the entire FTS index from the authoritative chunks table."""
    connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
