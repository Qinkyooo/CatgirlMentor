"""Heading-aware chunks sized for Chinese retrieval."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from .knowledge_clean import CleanBlock, CleanDocument

TARGET_CHUNK_CHARS = 800
MAX_BLOCK_CHARS = 1200
_SENTENCE = re.compile(r".*?(?:[。！？；\n]+|$)", flags=re.DOTALL)


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_key: str
    source_path: str
    heading_path: tuple[str, ...]
    source_line_start: int
    source_line_end: int
    ordinal: int
    text: str


@dataclass(frozen=True, slots=True)
class _PendingChunk:
    heading_path: tuple[str, ...]
    source_line_start: int
    source_line_end: int
    text: str


def _split_huge_sentence(value: str) -> tuple[str, ...]:
    return tuple(
        value[index : index + MAX_BLOCK_CHARS]
        for index in range(0, len(value), MAX_BLOCK_CHARS)
    )


def _split_long_block(text: str) -> tuple[str, ...]:
    sentences = tuple(
        match.group(0)
        for match in _SENTENCE.finditer(text)
        if match.group(0)
    )
    result: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > MAX_BLOCK_CHARS:
            if current:
                result.append(current)
                current = ""
            result.extend(_split_huge_sentence(sentence))
            continue
        if current and len(current) + len(sentence) > TARGET_CHUNK_CHARS:
            result.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        result.append(current)
    return tuple(result)


def _same_top_level(
    first: tuple[str, ...], second: tuple[str, ...]
) -> bool:
    return (first[:1] or ("",)) == (second[:1] or ("",))


def _chunk_key(
    *,
    source_path: str,
    heading_path: tuple[str, ...],
    source_line_start: int,
    source_line_end: int,
    text: str,
) -> str:
    normalized_path = unicodedata.normalize(
        "NFKC", source_path.replace("\\", "/")
    )
    material = json.dumps(
        [
            normalized_path,
            list(heading_path),
            source_line_start,
            source_line_end,
            text,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def chunk_document(
    source_path: str, document: CleanDocument
) -> tuple[KnowledgeChunk, ...]:
    pending: list[_PendingChunk] = []
    current_blocks: list[CleanBlock] = []

    def flush() -> None:
        if not current_blocks:
            return
        pending.append(
            _PendingChunk(
                heading_path=current_blocks[0].heading_path,
                source_line_start=current_blocks[0].source_line_start,
                source_line_end=current_blocks[-1].source_line_end,
                text="\n\n".join(block.text for block in current_blocks),
            )
        )
        current_blocks.clear()

    for block in document.blocks:
        if not block.text:
            continue
        if block.kind == "heading" and current_blocks:
            flush()
        if len(block.text) > MAX_BLOCK_CHARS:
            flush()
            pending.extend(
                _PendingChunk(
                    heading_path=block.heading_path,
                    source_line_start=block.source_line_start,
                    source_line_end=block.source_line_end,
                    text=part,
                )
                for part in _split_long_block(block.text)
            )
            continue
        if current_blocks:
            combined_length = (
                sum(len(item.text) for item in current_blocks)
                + 2 * len(current_blocks)
                + len(block.text)
            )
            if (
                not _same_top_level(
                    current_blocks[0].heading_path, block.heading_path
                )
                or combined_length > TARGET_CHUNK_CHARS
            ):
                flush()
        current_blocks.append(block)
    flush()

    return tuple(
        KnowledgeChunk(
            chunk_key=_chunk_key(
                source_path=source_path,
                heading_path=item.heading_path,
                source_line_start=item.source_line_start,
                source_line_end=item.source_line_end,
                text=item.text,
            ),
            source_path=source_path.replace("\\", "/"),
            heading_path=item.heading_path,
            source_line_start=item.source_line_start,
            source_line_end=item.source_line_end,
            ordinal=ordinal,
            text=item.text,
        )
        for ordinal, item in enumerate(pending)
    )
