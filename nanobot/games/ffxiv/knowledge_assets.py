"""Resolve source-bundled and user-configured FF14 guide databases."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from .knowledge_db import SCHEMA_VERSION

BUNDLED_GUIDE_DATABASE: Final[str] = "bundled"
BUNDLED_DATABASE_NAME: Final[str] = "guide.sqlite3"
BUNDLED_MANIFEST_NAME: Final[str] = "guide.manifest.json"
GuideDatabaseMode = Literal["legacy", "custom", "bundled"]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REQUIRED_TABLES = frozenset(
    {
        "documents",
        "chunks",
        "document_links",
        "chunks_fts",
        "entities",
        "aliases",
        "entity_chunks",
        "build_meta",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedGuideDatabase:
    path: Path
    mode: GuideDatabaseMode


@dataclass(frozen=True, slots=True)
class KnowledgeManifest:
    schema_version: int
    database_file: str
    database_sha256: str
    source_digest: str
    document_count: int
    chunk_count: int
    built_at: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeDatabaseInfo:
    path: Path
    source_digest: str
    document_count: int
    chunk_count: int


class KnowledgeDatabaseError(RuntimeError):
    """A stable, user-actionable database validation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def bundled_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def bundled_database_path() -> Path:
    return bundled_data_dir() / BUNDLED_DATABASE_NAME


def _manifest_path() -> Path:
    return bundled_data_dir() / BUNDLED_MANIFEST_NAME


def _manifest_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    return value


def _manifest_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} 必须是正整数")
    return value


def load_bundled_manifest() -> KnowledgeManifest:
    path = _manifest_path()
    if not path.is_file():
        raise KnowledgeDatabaseError(
            "bundled_manifest_missing",
            f"源码内置知识库清单不存在: {path}",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("清单顶层必须是对象")
        manifest_values = cast(dict[str, object], value)
        schema_version = _manifest_positive_int(
            manifest_values.get("schemaVersion"), "schemaVersion"
        )
        database_file = _manifest_string(
            manifest_values.get("databaseFile"), "databaseFile"
        )
        database_sha256 = _manifest_string(
            manifest_values.get("databaseSha256"), "databaseSha256"
        )
        source_digest = _manifest_string(
            manifest_values.get("sourceDigest"), "sourceDigest"
        )
        document_count = _manifest_positive_int(
            manifest_values.get("documentCount"), "documentCount"
        )
        chunk_count = _manifest_positive_int(
            manifest_values.get("chunkCount"), "chunkCount"
        )
        built_at_value = manifest_values.get("builtAt")
        if built_at_value is not None and not isinstance(built_at_value, str):
            raise ValueError("builtAt 必须是字符串或 null")
        if database_file != BUNDLED_DATABASE_NAME:
            raise ValueError(f"databaseFile 必须是 {BUNDLED_DATABASE_NAME}")
        if _SHA256.fullmatch(database_sha256) is None:
            raise ValueError("databaseSha256 必须是小写 SHA-256")
        if _SHA256.fullmatch(source_digest) is None:
            raise ValueError("sourceDigest 必须是小写 SHA-256")
    except KnowledgeDatabaseError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise KnowledgeDatabaseError(
            "bundled_manifest_invalid",
            f"源码内置知识库清单无效: {path}: {exc}",
        ) from exc
    return KnowledgeManifest(
        schema_version=schema_version,
        database_file=database_file,
        database_sha256=database_sha256,
        source_digest=source_digest,
        document_count=document_count,
        chunk_count=chunk_count,
        built_at=built_at_value,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _invalid_code(manifest: KnowledgeManifest | None) -> str:
    return "bundled_database_invalid" if manifest is not None else "custom_database_invalid"


def validate_database(
    path: Path,
    *,
    manifest: KnowledgeManifest | None,
    full: bool,
) -> KnowledgeDatabaseInfo:
    path = Path(path).expanduser().resolve(strict=False)
    invalid_code = _invalid_code(manifest)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise OSError("不是普通文件")
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            missing = sorted(_REQUIRED_TABLES - tables)
            if missing:
                raise ValueError(f"缺少表: {', '.join(missing)}")
            metadata = {
                str(key): str(value)
                for key, value in connection.execute("SELECT key, value FROM build_meta")
            }
            database_schema = metadata.get("schema_version")
            if database_schema != str(SCHEMA_VERSION):
                code = (
                    "bundled_database_schema_unsupported"
                    if manifest is not None
                    else invalid_code
                )
                raise KnowledgeDatabaseError(
                    code,
                    f"知识库架构版本不受支持: {resolved}: {database_schema!r}",
                )
            source_digest = metadata.get("source_manifest_digest", "")
            if _SHA256.fullmatch(source_digest) is None:
                raise ValueError("source_manifest_digest 缺失或无效")
            document_count = int(
                connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            )
            chunk_count = int(
                connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            )
            if document_count < 1 or chunk_count < 1:
                raise ValueError("文档数和切片数必须为正数")
            if metadata.get("document_count") != str(document_count):
                raise ValueError("数据库 document_count 元数据漂移")
            connection.execute("SELECT rowid FROM chunks_fts LIMIT 1").fetchall()
            if full and connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("PRAGMA quick_check 未返回 ok")
        finally:
            connection.close()
        if manifest is not None:
            if manifest.schema_version != SCHEMA_VERSION:
                raise KnowledgeDatabaseError(
                    "bundled_database_schema_unsupported",
                    f"源码内置知识库架构版本不受支持: {manifest.schema_version}",
                )
            if source_digest != manifest.source_digest:
                raise ValueError("清单 sourceDigest 与数据库不一致")
            if document_count != manifest.document_count:
                raise ValueError("清单 documentCount 与数据库不一致")
            if chunk_count != manifest.chunk_count:
                raise ValueError("清单 chunkCount 与数据库不一致")
            if full and _sha256(resolved) != manifest.database_sha256:
                raise KnowledgeDatabaseError(
                    "bundled_database_hash_mismatch",
                    f"源码内置知识库与清单散列不匹配: {resolved}",
                )
    except KnowledgeDatabaseError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise KnowledgeDatabaseError(
            invalid_code,
            f"知识库数据库无效: {path}: {exc}",
        ) from exc
    return KnowledgeDatabaseInfo(
        path=resolved,
        source_digest=source_digest,
        document_count=document_count,
        chunk_count=chunk_count,
    )


def validate_bundled_database(*, full: bool) -> KnowledgeDatabaseInfo:
    database = bundled_database_path()
    if not database.is_file():
        raise KnowledgeDatabaseError(
            "bundled_database_missing",
            f"源码内置知识库不存在: {database}",
        )
    manifest = load_bundled_manifest()
    if manifest.schema_version != SCHEMA_VERSION:
        raise KnowledgeDatabaseError(
            "bundled_database_schema_unsupported",
            f"源码内置知识库架构版本不受支持: {manifest.schema_version}",
        )
    return validate_database(database, manifest=manifest, full=full)


def validate_custom_database(path: Path, *, full: bool) -> KnowledgeDatabaseInfo:
    database = Path(path).expanduser().resolve(strict=False)
    if not database.is_file():
        raise KnowledgeDatabaseError(
            "custom_database_missing",
            f"自定义知识库不存在: {database}",
        )
    return validate_database(database, manifest=None, full=full)


def resolve_guide_database(
    configured: str | None,
    *,
    data_root: Path,
) -> ResolvedGuideDatabase:
    if configured == BUNDLED_GUIDE_DATABASE:
        return ResolvedGuideDatabase(bundled_database_path(), "bundled")
    if configured:
        return ResolvedGuideDatabase(
            Path(configured).expanduser().resolve(strict=False),
            "custom",
        )
    path = (
        Path(data_root).expanduser()
        / "ffxiv"
        / "knowledge"
        / BUNDLED_DATABASE_NAME
    ).resolve(strict=False)
    if path.is_file():
        return ResolvedGuideDatabase(path, "legacy")
    return ResolvedGuideDatabase(bundled_database_path(), "bundled")
