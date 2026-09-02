#!/usr/bin/env python3
"""Validate, render, apply, clear, and collect bounded persona source data."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

API_URL = "https://strings.ffcafe.cn/api/search"
ITEMS_URL = "https://strings.ffcafe.cn/api/items"
SHEET_WHITELIST = ("Balloon", "AkatsukiNoteString")
STORY_SHEET_PREFIXES = ("cut_scene/", "quest/")
MAX_FIELD_CHARS = 500
MAX_TRAITS = 5
MAX_RESULTS = 500
MAX_PAGE_SIZE = 100


class PersonaError(ValueError):
    """Raised when persona input or a remote source violates the contract."""


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PersonaError(f"{path}: expected object")
    return cast(Mapping[str, Any], value)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonaError(f"{path}: required non-empty text")
    if len(value) > MAX_FIELD_CHARS:
        raise PersonaError(f"{path}: exceeds {MAX_FIELD_CHARS} characters")
    return value.strip()


def _text_list(value: object, path: str, *, maximum: int | None = None) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PersonaError(f"{path}: required non-empty list")
    values = cast(list[object], value)
    if maximum is not None and len(values) > maximum:
        raise PersonaError(f"{path}: exceeds {maximum} items")
    return [_text(item, f"{path}[{index}]") for index, item in enumerate(values)]


def parse_profile(data: object) -> dict[str, Any]:
    """Validate and normalize a schemaVersion=1 persona profile."""
    root = _mapping(data, "profile")
    if root.get("schemaVersion") != 1:
        raise PersonaError("schemaVersion: expected 1")

    character = _mapping(root.get("character"), "character")
    normalized_character = {
        "name": _text(character.get("name"), "character.name"),
        "work": _text(character.get("work"), "character.work"),
        "sourceNote": _text(character.get("sourceNote"), "character.sourceNote"),
    }
    speech = _mapping(root.get("speech"), "speech")
    normalized_speech = {
        "catchphrases": _text_list(speech.get("catchphrases"), "speech.catchphrases"),
        "addressUser": _text(speech.get("addressUser"), "speech.addressUser"),
        "tone": _text(speech.get("tone"), "speech.tone"),
        "taboos": _text_list(speech.get("taboos"), "speech.taboos"),
    }
    return {
        "schemaVersion": 1,
        "character": normalized_character,
        "traits": _text_list(root.get("traits"), "traits", maximum=MAX_TRAITS),
        "speech": normalized_speech,
        "background": _text(root.get("background"), "background"),
        "relationship": _text(root.get("relationship"), "relationship"),
        "knowledgeBoundary": _text(root.get("knowledgeBoundary"), "knowledgeBoundary"),
        "responseRules": _text_list(root.get("responseRules"), "responseRules"),
    }


def _safe_markdown(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return re.sub(r"(?m)^([ \t]*)#", r"\1&#35;", escaped)


def _bullets(values: Sequence[str]) -> str:
    return "\n".join(f"- {_safe_markdown(value)}" for value in values)


def render_persona_section(profile: object) -> str:
    """Render the managed SOUL.md block from a validated profile."""
    data = parse_profile(profile)
    character = data["character"]
    speech = data["speech"]
    return (
        "<!-- persona:managed -->\n"
        f"## 人设：{_safe_markdown(character['name'])}（{_safe_markdown(character['work'])}）\n\n"
        "### 基础设定\n"
        f"{_safe_markdown(data['background'])}\n\n"
        f"素材依据：{_safe_markdown(character['sourceNote'])}\n\n"
        "### 性格特质\n"
        f"{_bullets(data['traits'])}\n\n"
        "### 说话风格\n"
        f"- 口癖 / 习惯用语：{_safe_markdown('；'.join(speech['catchphrases']))}\n"
        f"- 对玩家的称呼：{_safe_markdown(speech['addressUser'])}\n"
        f"- 语气与句式：{_safe_markdown(speech['tone'])}\n"
        f"- 禁忌措辞：{_safe_markdown('；'.join(speech['taboos']))}\n\n"
        "### 与玩家的关系\n"
        f"{_safe_markdown(data['relationship'])}\n\n"
        "### 知识边界\n"
        f"{_safe_markdown(data['knowledgeBoundary'])}\n\n"
        "### 回答规则\n"
        f"{_bullets(data['responseRules'])}\n"
        "<!-- /persona:managed -->"
    )


def render_user_section(profile: object) -> str:
    """Render the managed USER.md relationship block."""
    data = parse_profile(profile)
    name = _safe_markdown(data["character"]["name"])
    relationship = _safe_markdown(data["relationship"])
    return (
        "<!-- persona:user -->\n"
        f"## 与 {name} 的关系设定\n"
        f"{relationship}\n"
        "<!-- /persona:user -->"
    )


def _markers(marker: str) -> tuple[str, str]:
    if not re.fullmatch(r"[a-z][a-z0-9:-]*", marker):
        raise PersonaError("marker: invalid marker name")
    return f"<!-- {marker} -->", f"<!-- /{marker} -->"


def _bounds(text: str, marker: str) -> tuple[int, int] | None:
    start_marker, end_marker = _markers(marker)
    starts = [match.start() for match in re.finditer(re.escape(start_marker), text)]
    ends = [match.end() for match in re.finditer(re.escape(end_marker), text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise PersonaError(f"{marker}: malformed or duplicate managed block")
    return starts[0], ends[0]


def insert_block(existing_text: str, marker: str, block: str) -> str:
    """Append or replace exactly one managed block."""
    bounds = _bounds(existing_text, marker)
    normalized = block.strip()
    if bounds is not None:
        start, end = bounds
        return existing_text[:start] + normalized + existing_text[end:]
    prefix = existing_text.rstrip()
    return f"{prefix}\n\n{normalized}\n" if prefix else f"{normalized}\n"


def remove_block(text: str, marker: str) -> str:
    """Remove one managed block while preserving all unmanaged text."""
    bounds = _bounds(text, marker)
    if bounds is None:
        return text
    start, end = bounds
    before, after = text[:start], text[end:]
    if before.strip() and after.strip():
        return before.rstrip() + "\n\n" + after.lstrip()
    if before.strip():
        return before.rstrip() + "\n"
    return after.lstrip() if after.strip() else ""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.persona.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonaError(f"profile JSON: {exc.msg}") from exc
    return parse_profile(raw)


def apply_profile(profile: object, soul_path: Path, user_path: Path | None = None) -> None:
    data = parse_profile(profile)
    soul = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    _write(soul_path, insert_block(soul, "persona:managed", render_persona_section(data)))
    if user_path is not None:
        user = user_path.read_text(encoding="utf-8") if user_path.exists() else ""
        _write(user_path, insert_block(user, "persona:user", render_user_section(data)))


def clear_profile(soul_path: Path, user_path: Path | None = None) -> None:
    if soul_path.exists():
        _write(soul_path, remove_block(soul_path.read_text(encoding="utf-8"), "persona:managed"))
    if user_path is not None and user_path.exists():
        _write(user_path, remove_block(user_path.read_text(encoding="utf-8"), "persona:user"))


def search_url(
    name: str,
    *,
    lang: str = "chs",
    sheet: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
) -> str:
    if not name.strip():
        raise PersonaError("name: required")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise PersonaError(f"limit: expected 1..{MAX_PAGE_SIZE}")
    if offset < 0:
        raise PersonaError("offset: expected non-negative integer")
    if sheet is not None and sheet not in SHEET_WHITELIST:
        raise PersonaError(f"sheet: unsupported value {sheet!r}")
    params: dict[str, object] = {
        "lang": lang,
        "q": name.strip(),
        "limit": limit,
        "offset": offset,
    }
    if sheet:
        params["sheet"] = sheet
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def items_url(sheet: str, *, offset: int, limit: int) -> str:
    if not sheet.strip():
        raise PersonaError("sheet: required")
    if offset < 0:
        raise PersonaError("offset: expected non-negative integer")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise PersonaError(f"limit: expected 1..{MAX_PAGE_SIZE}")
    params = {"sheet": sheet, "offset": offset, "limit": limit, "fields": "chs"}
    return f"{ITEMS_URL}?{urllib.parse.urlencode(params)}"


def parse_ffcafe_response(payload: object) -> tuple[list[dict[str, Any]], int]:
    root = _mapping(payload, "response")
    if "meta" not in root:
        raise PersonaError("meta.total: required")
    meta = _mapping(root.get("meta"), "meta")
    total = meta.get("total")
    if not isinstance(total, int) or total < 0:
        raise PersonaError("meta.total: expected non-negative integer")
    rows = root.get("data")
    if not isinstance(rows, list):
        raise PersonaError("data: expected list")
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(cast(list[object], rows)):
        row = _mapping(raw, f"data[{index}]")
        values = _mapping(row.get("values"), f"data[{index}].values")
        text = values.get("chs")
        sheet = row.get("sheet")
        row_id = row.get("rowId")
        if isinstance(sheet, str) and isinstance(text, str) and text.strip() and row_id is not None:
            items.append(
                {
                    "sheet": sheet,
                    "rowId": row_id,
                    "text": text.strip(),
                    "index": row.get("index"),
                }
            )
    return items, total


Fetch = Callable[[str], bytes | tuple[int, bytes]]


def search_ffcafe(
    fetch: Fetch,
    name: str,
    *,
    lang: str = "chs",
    sheet: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    response = fetch(search_url(name, lang=lang, sheet=sheet, limit=limit, offset=offset))
    status, body = response if isinstance(response, tuple) else (200, response)
    if status != 200:
        raise PersonaError(f"HTTP {status} from strings.ffcafe.cn")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersonaError("JSON response from strings.ffcafe.cn is invalid") from exc
    return parse_ffcafe_response(payload)


def fetch_ffcafe_items(
    fetch: Fetch,
    sheet: str,
    *,
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    response = fetch(items_url(sheet, offset=offset, limit=limit))
    status, body = response if isinstance(response, tuple) else (200, response)
    if status != 200:
        raise PersonaError(f"HTTP {status} from strings.ffcafe.cn")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersonaError("JSON response from strings.ffcafe.cn is invalid") from exc
    return parse_ffcafe_response(payload)[0]


def merge_and_filter(
    items: Sequence[Mapping[str, Any]],
    sheet_whitelist: Sequence[str] = SHEET_WHITELIST,
    *,
    cap: int = MAX_RESULTS,
) -> tuple[list[dict[str, Any]], bool]:
    if not 1 <= cap <= MAX_RESULTS:
        raise PersonaError(f"cap: expected 1..{MAX_RESULTS}")
    allowed = set(sheet_whitelist)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        sheet = item.get("sheet")
        row_id = item.get("rowId")
        text = item.get("text")
        if sheet not in allowed or row_id is None or not isinstance(text, str) or not text.strip():
            continue
        key = str(sheet), str(row_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append({"sheet": str(sheet), "rowId": row_id, "text": text.strip()})
    return unique[:cap], len(unique) > cap


def plan_pages(total: int, limit: int = MAX_PAGE_SIZE, cap: int = MAX_RESULTS) -> list[int]:
    if total < 0:
        raise PersonaError("total: expected non-negative integer")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise PersonaError(f"limit: expected 1..{MAX_PAGE_SIZE}")
    if not 1 <= cap <= MAX_RESULTS:
        raise PersonaError(f"cap: expected 1..{MAX_RESULTS}")
    return list(range(0, min(total, cap), limit))


def render_dialogue_source(name: str, items: Sequence[Mapping[str, Any]], *, truncated: bool) -> str:
    lines = [
        f"# {_safe_markdown(name)} 剧情台词素材",
        "",
        f"来源：{API_URL}",
        f"状态：{'已截断（达到输入上限）' if truncated else '完整（在本次检索上限内）'}",
        "说明：以下是角色名命中位置及其后续剧情上下文，并非说话人标注；不得把每一行都归因于目标角色。",
        "",
    ]
    if not items:
        lines.append("未命中符合白名单的台词。")
    else:
        for item in items:
            label = f"{item['sheet']}#{item['rowId']}"
            lines.append(f"- [{_safe_markdown(label)}] {_safe_markdown(str(item['text']))}")
    return "\n".join(lines).rstrip() + "\n"


def _urlopen_fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "nanobot-persona/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise PersonaError(f"strings.ffcafe.cn request failed: {exc.reason}") from exc


def collect_dialogue(
    name: str,
    sheets: Sequence[str],
    *,
    cap: int,
    limit: int,
    fetch: Fetch = _urlopen_fetch,
) -> tuple[list[dict[str, Any]], bool]:
    collected: list[dict[str, Any]] = []
    source_exceeded_cap = False
    for sheet in sheets:
        first, total = search_ffcafe(fetch, name, sheet=sheet, limit=limit, offset=0)
        collected.extend(first)
        offsets = plan_pages(total, limit, cap)
        source_exceeded_cap = source_exceeded_cap or total > cap
        for offset in offsets[1:]:
            page, _ = search_ffcafe(fetch, name, sheet=sheet, limit=limit, offset=offset)
            collected.extend(page)
    merged, truncated = merge_and_filter(collected, sheets, cap=cap)
    return merged, truncated or source_exceeded_cap


def collect_story_context(
    fetch: Fetch,
    name: str,
    *,
    cap: int = MAX_RESULTS,
    context_after: int = 8,
    anchor_cap: int = 20,
) -> tuple[list[dict[str, Any]], bool]:
    """Collect bounded story context after character-name hits in quest/cutscene sheets."""
    if not 1 <= cap <= MAX_RESULTS:
        raise PersonaError(f"cap: expected 1..{MAX_RESULTS}")
    if not 1 <= context_after < MAX_PAGE_SIZE:
        raise PersonaError(f"context_after: expected 1..{MAX_PAGE_SIZE - 1}")
    if not 1 <= anchor_cap <= 50:
        raise PersonaError("anchor_cap: expected 1..50")

    hits, total = search_ffcafe(fetch, name, limit=MAX_PAGE_SIZE, offset=0)
    for offset in plan_pages(total, MAX_PAGE_SIZE, MAX_RESULTS)[1:]:
        page, _ = search_ffcafe(fetch, name, limit=MAX_PAGE_SIZE, offset=offset)
        hits.extend(page)

    anchors: list[tuple[str, int]] = []
    seen_anchors: set[tuple[str, int]] = set()
    for hit in hits:
        sheet, index = hit.get("sheet"), hit.get("index")
        if (
            not isinstance(sheet, str)
            or not sheet.startswith(STORY_SHEET_PREFIXES)
            or not isinstance(index, int)
        ):
            continue
        key = sheet, index
        if key not in seen_anchors:
            seen_anchors.add(key)
            anchors.append(key)

    contexts: list[dict[str, Any]] = []
    seen_rows: set[tuple[str, str]] = set()
    for sheet, index in anchors[:anchor_cap]:
        rows = fetch_ffcafe_items(
            fetch,
            sheet,
            offset=index,
            limit=context_after + 1,
        )
        for row in rows:
            key = str(row["sheet"]), str(row["rowId"])
            if key in seen_rows:
                continue
            seen_rows.add(key)
            contexts.append({**row, "context": f"{sheet}@{index}"})
            if len(contexts) >= cap:
                return contexts, True

    if contexts:
        truncated = total > MAX_RESULTS or len(anchors) > anchor_cap
        return contexts, truncated

    fallback, truncated = merge_and_filter(hits, SHEET_WHITELIST, cap=cap)
    return [{**item, "context": "search-hit"} for item in fallback], truncated or total > MAX_RESULTS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render", help="render a profile preview")
    render.add_argument("profile", type=Path)
    render.add_argument("--out", type=Path, required=True)

    apply_cmd = commands.add_parser("apply", help="apply profile blocks")
    apply_cmd.add_argument("profile", type=Path)
    apply_cmd.add_argument("soul", type=Path)
    apply_cmd.add_argument("--user", type=Path)

    clear = commands.add_parser("clear", help="remove managed profile blocks")
    clear.add_argument("soul", type=Path)
    clear.add_argument("--user", type=Path)

    search = commands.add_parser("search", help="collect bounded FFCafe dialogue")
    search.add_argument("name")
    search.add_argument(
        "--sheet",
        action="append",
        choices=SHEET_WHITELIST,
        help="exact sheet search; omit to collect quest/cutscene context",
    )
    search.add_argument("--cap", type=int, default=MAX_RESULTS)
    search.add_argument("--limit", type=int, default=MAX_PAGE_SIZE)
    search.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render":
            profile = _read_profile(args.profile)
            preview = render_persona_section(profile) + "\n\n" + render_user_section(profile) + "\n"
            _write(args.out, preview)
        elif args.command == "apply":
            apply_profile(_read_profile(args.profile), args.soul, args.user)
        elif args.command == "clear":
            clear_profile(args.soul, args.user)
        elif args.command == "search":
            if args.sheet:
                items, truncated = collect_dialogue(
                    args.name,
                    args.sheet,
                    cap=args.cap,
                    limit=args.limit,
                )
            else:
                items, truncated = collect_story_context(
                    _urlopen_fetch,
                    args.name,
                    cap=args.cap,
                )
            _write(args.out, render_dialogue_source(args.name, items, truncated=truncated))
        return 0
    except (OSError, PersonaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
