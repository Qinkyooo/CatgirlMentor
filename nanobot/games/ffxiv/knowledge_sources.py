"""Read-only inventory of prepared Chinese knowledge sources."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

SUPPORTED_SOURCE_EXTENSIONS = frozenset({".md", ".vue", ".html", ".txt"})


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    source_path: str
    extension: str
    size_bytes: int
    mtime_ns: int
    sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class SourceWarning:
    source_path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SourceScanResult:
    documents: tuple[KnowledgeSource, ...]
    warnings: tuple[SourceWarning, ...]


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_link(path: Path) -> bool:
    attributes = cast(int, getattr(path.lstat(), "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & 0x400)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _warning(path: str, code: str, message: str) -> SourceWarning:
    return SourceWarning(source_path=path, code=code, message=message)


def scan_knowledge_sources(source_root: Path) -> SourceScanResult:
    root = Path(source_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("knowledge source root must be a directory")

    candidates: list[Path] = []
    warnings: list[SourceWarning] = []
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        kept_names: list[str] = []
        for name in sorted(names):
            child = current / name
            if not _is_link(child):
                kept_names.append(name)
                continue
            relative = _relative(child, root)
            if not _inside(child, root):
                warnings.append(
                    _warning(relative, "escaped", "linked directory leaves source root")
                )
            else:
                warnings.append(
                    _warning(relative, "linked_directory", "linked directory was not followed")
                )
        names[:] = kept_names
        for name in sorted(filenames):
            path = current / name
            if path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS:
                candidates.append(path)

    documents: list[KnowledgeSource] = []
    for path in sorted(candidates, key=lambda item: _relative(item, root)):
        relative = _relative(path, root)
        if not _inside(path, root):
            warnings.append(_warning(relative, "escaped", "file leaves source root"))
            continue
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            warnings.append(
                _warning(relative, "unreadable", f"could not read source: {type(exc).__name__}")
            )
            continue
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(data) != after.st_size
        ):
            warnings.append(
                _warning(relative, "changed", "source changed while it was being read")
            )
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            warnings.append(
                _warning(relative, "encoding", "source is not valid UTF-8")
            )
            continue
        documents.append(
            KnowledgeSource(
                source_path=relative,
                extension=path.suffix.lower(),
                size_bytes=len(data),
                mtime_ns=after.st_mtime_ns,
                sha256=hashlib.sha256(data).hexdigest(),
                text=text,
            )
        )

    return SourceScanResult(
        documents=tuple(documents),
        warnings=tuple(
            sorted(warnings, key=lambda item: (item.source_path, item.code))
        ),
    )
