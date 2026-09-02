"""Small semantic cleaners for Chinese Markdown, VuePress, Vue and HTML."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

BlockKind = Literal["heading", "paragraph", "list", "table", "code", "container"]


@dataclass(frozen=True, slots=True)
class CleanBlock:
    kind: BlockKind
    text: str
    source_line_start: int
    source_line_end: int
    heading_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DocumentLink:
    label: str
    destination: str
    source_line: int


@dataclass(frozen=True, slots=True)
class CleanWarning:
    code: str
    source_line: int
    message: str


@dataclass(frozen=True, slots=True)
class CleanDocument:
    title: str
    blocks: tuple[CleanBlock, ...]
    links: tuple[DocumentLink, ...]
    warnings: tuple[CleanWarning, ...]


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_CONTAINER = re.compile(r"^:{3,}\s*(?P<kind>[\w-]+)?\s*(?P<label>.*)$")
_INCLUDE = re.compile(r"^\s*<!--\s*include:\s*(?P<path>.+?)\s*-->\s*$")
_LINK = re.compile(
    r"""(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)"""
)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_TAG = re.compile(r"<[^>]+>")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
_IGNORED_HTML = frozenset(
    {"script", "style", "nav", "footer", "header", "aside", "noscript"}
)
_CONTAINER_LABELS = {
    "tip": "提示",
    "warning": "警告",
    "danger": "危险",
    "collapse": "折叠说明",
    "segment": "说明",
    "info": "说明",
    "note": "说明",
}


def _normalize(value: str) -> str:
    punctuation = "，。：！？；"
    protected = {
        character: chr(0xE000 + index)
        for index, character in enumerate(punctuation)
    }
    restored = {value: key for key, value in protected.items()}
    value = value.translate(str.maketrans(protected))
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans(restored))
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _heading_tuple(levels: list[str], level: int, text: str) -> tuple[str, ...]:
    del levels[level - 1 :]
    while len(levels) < level - 1:
        levels.append("")
    levels.append(text)
    return tuple(item for item in levels if item)


def _inline(
    text: str, *, line: int, links: list[DocumentLink]
) -> str:
    text = _IMAGE.sub("", text)

    def replace_link(match: re.Match[str]) -> str:
        label, destination = match.groups()
        links.append(DocumentLink(label.strip(), destination.strip(), line))
        return label

    text = _LINK.sub(replace_link, text)
    text = _TAG.sub("", text)
    return " ".join(text.split())


def _front_matter(lines: list[str]) -> tuple[str | None, int]:
    if not lines or lines[0].strip() != "---":
        return None, 0
    for index in range(1, min(len(lines), 200)):
        if lines[index].strip() != "---":
            continue
        title = next(
            (
                line.split(":", 1)[1].strip().strip(chr(34) + chr(39))
                for line in lines[1:index]
                if line.casefold().startswith("title:") and ":" in line
            ),
            None,
        )
        return title, index + 1
    return None, 0


def _expand_includes(
    *,
    source_path: str,
    text: str,
    source_root: Path | None,
    depth: int,
    max_depth: int,
    stack: tuple[Path, ...],
    warnings: list[CleanWarning],
) -> str:
    if source_root is None:
        return text
    root = source_root.expanduser().resolve(strict=True)
    base = (root / source_path).resolve(strict=False).parent
    output: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _INCLUDE.match(line)
        if match is None:
            output.append(line)
            continue
        if depth >= max_depth:
            warnings.append(
                CleanWarning("include_depth", line_number, "include depth exceeded")
            )
            continue
        candidate = (base / match.group("path")).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            warnings.append(
                CleanWarning("include_escape", line_number, "include leaves source root")
            )
            continue
        if candidate in stack:
            warnings.append(
                CleanWarning("include_cycle", line_number, "include cycle detected")
            )
            continue
        try:
            included = candidate.read_text(encoding="utf-8-sig")
        except OSError as exc:
            warnings.append(
                CleanWarning(
                    "include_unreadable",
                    line_number,
                    f"include could not be read: {type(exc).__name__}",
                )
            )
            continue
        output.append(
            _expand_includes(
                source_path=candidate.relative_to(root).as_posix(),
                text=included,
                source_root=root,
                depth=depth + 1,
                max_depth=max_depth,
                stack=(*stack, candidate),
                warnings=warnings,
            )
        )
    return "\n".join(output)


class _SemanticHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[CleanBlock] = []
        self.links: list[DocumentLink] = []
        self._ignored_depth = 0
        self._buffer: list[str] = []
        self._block_kind: BlockKind = "paragraph"
        self._block_start = 1
        self._headings: list[str] = []
        self._heading_level: int | None = None
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def _flush(self, end_line: int) -> None:
        text = " ".join("".join(self._buffer).split())
        self._buffer.clear()
        if not text:
            return
        if self._heading_level is not None:
            heading_path = _heading_tuple(
                self._headings, self._heading_level, text
            )
            kind: BlockKind = "heading"
        else:
            heading_path = tuple(item for item in self._headings if item)
            kind = self._block_kind
        self.blocks.append(
            CleanBlock(kind, text, self._block_start, end_line, heading_path)
        )
        self._heading_level = None
        self._block_kind = "paragraph"

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lowered = tag.casefold()
        if lowered in _IGNORED_HTML:
            self._flush(self.getpos()[0])
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        line = self.getpos()[0]
        if lowered in {"p", "li", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "template"}:
            self._flush(line)
            self._block_start = line
        if lowered == "li":
            self._block_kind = "list"
        elif lowered == "pre":
            self._block_kind = "code"
        elif lowered == "tr":
            self._block_kind = "table"
        elif lowered.startswith("h") and lowered[1:].isdigit():
            self._heading_level = int(lowered[1:])
        elif lowered == "a":
            self._link_href = dict(attrs).get("href")
            self._link_text = []

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() == "br" and not self._ignored_depth:
            self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in _IGNORED_HTML:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if lowered == "a" and self._link_href:
            self.links.append(
                DocumentLink(
                    " ".join("".join(self._link_text).split()),
                    self._link_href,
                    self.getpos()[0],
                )
            )
            self._link_href = None
            self._link_text = []
        if lowered in {"p", "li", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "template"}:
            self._flush(self.getpos()[0])

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self._buffer.append(data)
        if self._link_href is not None:
            self._link_text.append(data)

    def finish(self) -> None:
        self._flush(self.getpos()[0])


def _clean_html(source_path: str, text: str) -> CleanDocument:
    parser = _SemanticHTMLParser()
    parser.feed(text)
    parser.finish()
    title = next(
        (block.text for block in parser.blocks if block.kind == "heading"),
        Path(source_path).stem,
    )
    return CleanDocument(title, tuple(parser.blocks), tuple(parser.links), ())


def _clean_markdown(
    source_path: str,
    text: str,
    warnings: list[CleanWarning],
) -> CleanDocument:
    lines = text.splitlines()
    front_title, cursor = _front_matter(lines)
    blocks: list[CleanBlock] = []
    links: list[DocumentLink] = []
    headings: list[str] = []
    paragraph: list[tuple[int, str]] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        cleaned = [
            _inline(value, line=line_number, links=links)
            for line_number, value in paragraph
        ]
        value = " ".join(item for item in cleaned if item)
        if value:
            blocks.append(
                CleanBlock(
                    "paragraph",
                    value,
                    paragraph[0][0],
                    paragraph[-1][0],
                    tuple(item for item in headings if item),
                )
            )
        paragraph.clear()

    in_code = False
    code_start = 0
    code_lines: list[str] = []
    ignored_tag: str | None = None
    while cursor < len(lines):
        raw = lines[cursor]
        line_number = cursor + 1
        stripped = raw.strip()
        cursor += 1
        if ignored_tag is not None:
            if re.search(fr"</\s*{ignored_tag}\s*>", stripped, flags=re.I):
                ignored_tag = None
            continue
        ignored = re.match(r"^<\s*(script|style|nav|footer|header|aside)\b", stripped, re.I)
        if ignored is not None:
            flush_paragraph()
            tag = ignored.group(1).casefold()
            if re.search(fr"</\s*{tag}\s*>", stripped, flags=re.I) is None:
                ignored_tag = tag
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_paragraph()
            if in_code:
                blocks.append(
                    CleanBlock(
                        "code",
                        "\n".join(code_lines),
                        code_start,
                        line_number - 1,
                        tuple(item for item in headings if item),
                    )
                )
                code_lines = []
                in_code = False
            else:
                in_code = True
                code_start = line_number + 1
            continue
        if in_code:
            code_lines.append(raw)
            continue
        heading = _HEADING.match(stripped)
        if heading is not None:
            flush_paragraph()
            value = _inline(heading.group(2), line=line_number, links=links)
            heading_path = _heading_tuple(headings, len(heading.group(1)), value)
            blocks.append(
                CleanBlock("heading", value, line_number, line_number, heading_path)
            )
            continue
        container = _CONTAINER.match(stripped)
        if container is not None:
            flush_paragraph()
            container_kind = container.group("kind")
            label = container.group("label").strip()
            if container_kind:
                prefix = _CONTAINER_LABELS.get(
                    container_kind.casefold(), container_kind
                )
                text_value = f"{prefix}：{label}" if label else prefix
                blocks.append(
                    CleanBlock(
                        "container",
                        text_value,
                        line_number,
                        line_number,
                        tuple(item for item in headings if item),
                    )
                )
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            table_rows = [(line_number, raw)]
            while cursor < len(lines) and lines[cursor].strip().startswith("|"):
                table_rows.append((cursor + 1, lines[cursor]))
                cursor += 1
            readable: list[str] = []
            for table_line, row in table_rows:
                if _TABLE_SEPARATOR.match(row):
                    continue
                cells = [
                    _inline(cell.strip(), line=table_line, links=links)
                    for cell in row.strip().strip("|").split("|")
                ]
                readable.append(" | ".join(cell for cell in cells if cell))
            if readable:
                blocks.append(
                    CleanBlock(
                        "table",
                        "\n".join(readable),
                        table_rows[0][0],
                        table_rows[-1][0],
                        tuple(item for item in headings if item),
                    )
                )
            continue
        if not stripped:
            flush_paragraph()
            continue
        kind: BlockKind = "list" if re.match(r"^\s*(?:[-*+] |\d+[.)] )", raw) else "paragraph"
        if kind == "list":
            flush_paragraph()
            value = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", raw)
            value = _inline(value, line=line_number, links=links)
            if value:
                blocks.append(
                    CleanBlock(
                        "list",
                        value,
                        line_number,
                        line_number,
                        tuple(item for item in headings if item),
                    )
                )
        else:
            paragraph.append((line_number, raw))
    flush_paragraph()
    if in_code and code_lines:
        blocks.append(
            CleanBlock(
                "code",
                "\n".join(code_lines),
                code_start,
                len(lines),
                tuple(item for item in headings if item),
            )
        )
    title = front_title or next(
        (block.text for block in blocks if block.kind == "heading"),
        Path(source_path).stem,
    )
    return CleanDocument(title, tuple(blocks), tuple(links), tuple(warnings))


def clean_knowledge_document(
    source_path: str,
    text: str,
    *,
    source_root: Path | None = None,
    max_include_depth: int = 5,
) -> CleanDocument:
    if max_include_depth < 0:
        raise ValueError("max_include_depth must not be negative")
    warnings: list[CleanWarning] = []
    normalized = _normalize(text)
    expanded = _expand_includes(
        source_path=source_path,
        text=normalized,
        source_root=source_root,
        depth=0,
        max_depth=max_include_depth,
        stack=(
            ((source_root.resolve() / source_path).resolve(strict=False),)
            if source_root is not None
            else ()
        ),
        warnings=warnings,
    )
    extension = Path(source_path).suffix.casefold()
    if extension in {".html", ".vue"}:
        document = _clean_html(source_path, expanded)
        return CleanDocument(
            document.title,
            document.blocks,
            document.links,
            tuple(warnings),
        )
    return _clean_markdown(source_path, expanded, warnings)
