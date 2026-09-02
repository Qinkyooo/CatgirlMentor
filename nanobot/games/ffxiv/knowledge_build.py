"""Incremental, atomic builder for the local Chinese guide database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .cache import atomic_write_json
from .knowledge_assets import BUNDLED_DATABASE_NAME
from .knowledge_chunk import KnowledgeChunk, chunk_document
from .knowledge_clean import DocumentLink, clean_knowledge_document
from .knowledge_db import (
    SCHEMA_VERSION,
    KnowledgeBuildError,
    create_schema,
    insert_fts_chunk,
)
from .knowledge_entities import (
    ExplicitAlias,
    ExtractedEntity,
    extract_entities,
    store_entities,
)
from .knowledge_sources import KnowledgeSource, scan_knowledge_sources

DEFAULT_DATABASE_NAME = "guide.sqlite3"


@dataclass(frozen=True, slots=True)
class KnowledgeBuildReport:
    database: str
    source_digest: str
    reused: int
    added: int
    updated: int
    deleted: int
    skipped: int
    warnings: int


@dataclass(frozen=True, slots=True)
class _StoredDocument:
    title: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    chunks: tuple[KnowledgeChunk, ...]
    entities: tuple[ExtractedEntity, ...]
    links: tuple[DocumentLink, ...]


def _manifest_digest(documents: tuple[KnowledgeSource, ...]) -> str:
    digest = hashlib.sha256()
    for document in documents:
        digest.update(
            json.dumps(
                [document.source_path, document.sha256, document.size_bytes],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _database_path(
    database: Path | None,
    data_dir: Path | None,
) -> Path:
    if database is not None:
        return Path(database).expanduser().resolve(strict=False)
    root = (
        Path(data_dir).expanduser()
        if data_dir is not None
        else Path.home() / ".nanobot" / "data"
    )
    return (root / "ffxiv" / "knowledge" / DEFAULT_DATABASE_NAME).resolve(
        strict=False
    )


def _read_existing(database: Path) -> dict[str, _StoredDocument]:
    if not database.is_file():
        return {}
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        version = connection.execute(
            "SELECT value FROM build_meta WHERE key = 'schema_version'"
        ).fetchone()
        if version != ("1",):
            raise KnowledgeBuildError(
                f"现有知识库架构版本不受支持: {database}"
            )
        documents: dict[str, _StoredDocument] = {}
        for row in connection.execute(
            """
            SELECT id, source_path, title, sha256, size_bytes, mtime_ns
            FROM documents ORDER BY source_path
            """
        ):
            document_id, source_path, title, sha256, size_bytes, mtime_ns = row
            chunks = tuple(
                KnowledgeChunk(
                    chunk_key=chunk[0],
                    source_path=source_path,
                    heading_path=tuple(json.loads(chunk[2])),
                    source_line_start=chunk[3],
                    source_line_end=chunk[4],
                    ordinal=chunk[1],
                    text=chunk[5],
                )
                for chunk in connection.execute(
                    """
                    SELECT chunk_key, ordinal, heading_path,
                           source_line_start, source_line_end, text
                    FROM chunks WHERE document_id = ? ORDER BY ordinal
                    """,
                    (document_id,),
                )
            )
            entities = tuple(
                ExtractedEntity(
                    kind=entity[1],
                    canonical_name=entity[2],
                    normalized_name=entity[3],
                    aliases=tuple(
                        ExplicitAlias(alias[0], alias[1], "existing_index")
                        for alias in connection.execute(
                            """
                            SELECT alias, normalized_alias FROM aliases
                            WHERE entity_id = ? ORDER BY normalized_alias
                            """,
                            (entity[0],),
                        )
                    ),
                )
                for entity in connection.execute(
                    """
                    SELECT DISTINCT e.id, e.kind, e.canonical_name, e.normalized_name
                    FROM entities AS e
                    JOIN entity_chunks AS ec ON ec.entity_id = e.id
                    JOIN chunks AS c ON c.id = ec.chunk_id
                    WHERE c.document_id = ?
                    ORDER BY e.kind, e.normalized_name
                    """,
                    (document_id,),
                )
            )
            links = tuple(
                DocumentLink(label=row[0], destination=row[1], source_line=row[2])
                for row in connection.execute(
                    """
                    SELECT label, destination, source_line
                    FROM document_links WHERE document_id = ?
                    ORDER BY source_line, destination
                    """,
                    (document_id,),
                )
            )
            documents[source_path] = _StoredDocument(
                title=title,
                sha256=sha256,
                size_bytes=size_bytes,
                mtime_ns=mtime_ns,
                chunks=chunks,
                entities=entities,
                links=links,
            )
        return documents
    except (json.JSONDecodeError, sqlite3.Error) as exc:
        raise KnowledgeBuildError(
            f"无法读取现有知识库，已保留原文件: {database}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _insert_document(
    connection: sqlite3.Connection,
    source: KnowledgeSource,
    title: str,
    chunks: tuple[KnowledgeChunk, ...],
    links: tuple[DocumentLink, ...],
) -> tuple[int, tuple[int, ...]]:
    document_id = connection.execute(
        """
        INSERT INTO documents(
            source_path, title, sha256, size_bytes, mtime_ns
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            source.source_path,
            title,
            source.sha256,
            source.size_bytes,
            source.mtime_ns,
        ),
    ).lastrowid
    if document_id is None:
        raise KnowledgeBuildError(f"写入文档失败: {source.source_path}")
    connection.executemany(
        """
        INSERT INTO document_links(document_id, label, destination, source_line)
        VALUES (?, ?, ?, ?)
        """,
        (
            (document_id, link.label, link.destination, link.source_line)
            for link in links
        ),
    )
    chunk_ids: list[int] = []
    for chunk in chunks:
        heading_path = json.dumps(
            chunk.heading_path, ensure_ascii=False, separators=(",", ":")
        )
        chunk_id = connection.execute(
            """
            INSERT INTO chunks(
                document_id, chunk_key, ordinal, heading_path,
                source_line_start, source_line_end, text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                chunk.chunk_key,
                chunk.ordinal,
                heading_path,
                chunk.source_line_start,
                chunk.source_line_end,
                chunk.text,
            ),
        ).lastrowid
        if chunk_id is None:
            raise KnowledgeBuildError(f"写入切片失败: {source.source_path}")
        chunk_ids.append(chunk_id)
        insert_fts_chunk(connection, chunk_id, chunk.text, heading_path)
    return document_id, tuple(chunk_ids)


def _validate_database(connection: sqlite3.Connection) -> None:
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise KnowledgeBuildError("知识库外键校验失败")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        detail = integrity[0] if integrity else "no result"
        raise KnowledgeBuildError(f"知识库完整性校验失败: {detail}")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except (OSError, NotImplementedError):
        return
    try:
        with suppress(OSError, NotImplementedError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_value(database: Path, source_digest: str) -> dict[str, object]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        metadata = {
            str(key): str(value)
            for key, value in connection.execute("SELECT key, value FROM build_meta")
        }
        document_count = int(
            connection.execute("SELECT count(*) FROM documents").fetchone()[0]
        )
        chunk_count = int(
            connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        )
    finally:
        connection.close()
    if metadata.get("schema_version") != str(SCHEMA_VERSION):
        raise KnowledgeBuildError("无法发布清单: 知识库架构版本无效")
    if metadata.get("source_manifest_digest") != source_digest:
        raise KnowledgeBuildError("无法发布清单: 素材摘要与知识库不一致")
    if metadata.get("document_count") != str(document_count):
        raise KnowledgeBuildError("无法发布清单: 文档计数与知识库不一致")
    built_at = metadata.get("built_at")
    if not built_at:
        raise KnowledgeBuildError("无法发布清单: 缺少 built_at 元数据")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "databaseFile": BUNDLED_DATABASE_NAME,
        "databaseSha256": _sha256_file(database),
        "sourceDigest": source_digest,
        "documentCount": document_count,
        "chunkCount": chunk_count,
        "builtAt": built_at,
    }


def _replace_manifest(staged: Path, target: Path) -> None:
    os.replace(staged, target)


def _promote_database_and_manifest(
    staged_database: Path,
    database: Path,
    staged_manifest: Path,
    manifest: Path,
) -> None:
    backup = database.with_name(f".{database.name}.bak")
    had_database = database.is_file()
    backup.unlink(missing_ok=True)
    if had_database:
        os.replace(database, backup)
    try:
        os.replace(staged_database, database)
        _replace_manifest(staged_manifest, manifest)
    except BaseException:
        with suppress(OSError):
            database.unlink(missing_ok=True)
        if had_database and backup.is_file():
            os.replace(backup, database)
        raise
    finally:
        with suppress(OSError):
            backup.unlink(missing_ok=True)


def _report(
    *,
    database: Path,
    source_digest: str,
    reused: int,
    added: int,
    updated: int,
    deleted: int,
    skipped: int,
    warnings: int,
) -> KnowledgeBuildReport:
    return KnowledgeBuildReport(
        database=str(database),
        source_digest=source_digest,
        reused=reused,
        added=added,
        updated=updated,
        deleted=deleted,
        skipped=skipped,
        warnings=warnings,
    )


def build_knowledge_database(
    source_root: Path,
    *,
    database: Path | None = None,
    data_dir: Path | None = None,
    manifest: Path | None = None,
) -> KnowledgeBuildReport:
    """Build a validated temporary database and atomically promote it."""
    source_root = Path(source_root).expanduser().resolve(strict=False)
    target = _database_path(database, data_dir)
    manifest_target = (
        Path(manifest).expanduser().resolve(strict=False)
        if manifest is not None
        else None
    )
    if manifest_target is not None and target.name != BUNDLED_DATABASE_NAME:
        raise KnowledgeBuildError(
            f"发布清单时数据库文件名必须是 {BUNDLED_DATABASE_NAME}: {target}"
        )
    try:
        scan = scan_knowledge_sources(source_root)
    except (OSError, ValueError) as exc:
        raise KnowledgeBuildError(f"无法扫描知识库素材: {source_root}") from exc
    documents = scan.documents
    source_digest = _manifest_digest(documents)
    old = _read_existing(target)
    current_by_path = {item.source_path: item for item in documents}
    skipped = len(
        {warning.source_path for warning in scan.warnings}
        - current_by_path.keys()
    )
    deleted = len(old.keys() - current_by_path.keys())
    unchanged = {
        path
        for path, item in current_by_path.items()
        if path in old and old[path].sha256 == item.sha256
    }
    added = len(current_by_path.keys() - old.keys())
    updated_paths = (current_by_path.keys() & old.keys()) - unchanged

    if target.is_file() and len(unchanged) == len(documents) and deleted == 0:
        report = _report(
            database=target,
            source_digest=source_digest,
            reused=len(unchanged),
            added=0,
            updated=0,
            deleted=0,
            skipped=skipped,
            warnings=len(scan.warnings),
        )
        if manifest_target is not None:
            atomic_write_json(
                manifest_target,
                _manifest_value(target, source_digest),
            )
        return report

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f"guide-v1-{source_digest}.sqlite3.tmp"
    staged_manifest = (
        manifest_target.with_name(f".{manifest_target.name}.{source_digest}.tmp")
        if manifest_target is not None
        else None
    )
    if temporary.exists():
        temporary.unlink()
    if staged_manifest is not None:
        staged_manifest.unlink(missing_ok=True)

    clean_warning_count = 0
    reused = 0
    updated = len(updated_paths)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode = DELETE")
        create_schema(connection)
        for source in documents:
            stored = old.get(source.source_path)
            has_include = "<!-- include:" in source.text.casefold()
            if stored is not None and stored.sha256 == source.sha256 and not has_include:
                title = stored.title
                chunks = stored.chunks
                entities = stored.entities
                links = stored.links
                reused += 1
            else:
                if stored is not None and stored.sha256 == source.sha256:
                    updated += 1
                try:
                    cleaned = clean_knowledge_document(
                        source.source_path,
                        source.text,
                        source_root=source_root,
                    )
                except Exception as exc:
                    raise KnowledgeBuildError(
                        f"清洗知识库文档失败: {source.source_path}"
                    ) from exc
                clean_warning_count += len(cleaned.warnings)
                title = cleaned.title
                chunks = chunk_document(source.source_path, cleaned)
                entities = extract_entities(source.text, cleaned)
                links = cleaned.links
            _document_id, chunk_ids = _insert_document(
                connection, source, title, chunks, links
            )
            store_entities(connection, entities, chunk_ids)

        metadata = {
            "source_manifest_digest": source_digest,
            "source_root": str(source_root),
            "built_at": datetime.now(UTC).isoformat(),
            "document_count": str(len(documents)),
        }
        connection.executemany(
            "INSERT OR REPLACE INTO build_meta(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        connection.commit()
        _validate_database(connection)
        connection.close()
        connection = None
        _fsync_file(temporary)

        after = scan_knowledge_sources(source_root)
        if _manifest_digest(after.documents) != source_digest:
            raise KnowledgeBuildError("知识库素材在构建期间发生变化，未发布新索引")

        if manifest_target is None or staged_manifest is None:
            os.replace(temporary, target)
        else:
            atomic_write_json(
                staged_manifest,
                _manifest_value(temporary, source_digest),
            )
            _promote_database_and_manifest(
                temporary,
                target,
                staged_manifest,
                manifest_target,
            )
        _fsync_directory(target.parent)
    except KnowledgeBuildError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise KnowledgeBuildError(f"构建知识库失败，已保留原文件: {target}") from exc
    finally:
        if connection is not None:
            connection.close()
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        if staged_manifest is not None:
            with suppress(OSError):
                staged_manifest.unlink(missing_ok=True)

    return _report(
        database=target,
        source_digest=source_digest,
        reused=reused,
        added=added,
        updated=updated,
        deleted=deleted,
        skipped=skipped,
        warnings=len(scan.warnings) + clean_warning_count,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 nanobot FF14 中文知识库")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_knowledge_database(
            args.source,
            database=args.database,
            data_dir=args.data_dir,
            manifest=args.manifest,
        )
    except KnowledgeBuildError as exc:
        print(f"knowledge build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
