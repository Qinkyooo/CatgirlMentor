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
from datetime import datetime
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

# SOUL.md / USER.md carry NO inline markers. The layout is structural:
#   <# Title>
#   <identity zone — default persona section, or the generated persona section>
#   ## 默认部分…
#   <stable default content, never rewritten>
# Exactly one identity zone is active per file: the generated persona section
# while a persona is applied, or the default section after `clear`. The zone is
# located structurally as everything between the title and the first
# "## 默认部分" heading, so no `<!-- persona:... -->` comments are needed.
SOUL_STABLE_HEADING = "## 默认部分"
USER_STABLE_HEADING = "## 默认部分"
DEFAULT_SOUL_HEADING = "## 默认人设"
DEFAULT_USER_HEADING = "## 默认关系"
PERSONA_SOUL_HEADING = "## 人设："
PERSONA_USER_HEADING_RE = r"^## 与 .+ 的关系设定$"

# Pre-zone default templates (the CatgirlMentor mentor persona) written into
# workspaces by earlier versions of this skill. `apply`/`clear` strip a leading
# block that exactly matches these, so old files that already carry a persona
# block migrate cleanly to the zone layout instead of keeping two personas.
_LEGACY_SOUL_TEMPLATE = """\
# Soul

我是你的导师——一只来自艾欧泽亚的猫娘（米可特族），也是你在艾欧泽亚最好的百科全书。

## 我的身份

- **种族**：米可特族（Miqo'te）· 逐日之民，长着猫耳和猫尾的猫娘
- **身份**：热心的大皇冠导师，游历过艾欧泽亚的每个角落——从萨纳兰的烈日到伊修加德的飞雪，从黄金港的灯火到亚马乌罗提的废墟
- **定位**：引导型导师。我不替你做决定，而是牵着你一起看地图、认路、找答案

## 称呼

- 统一称呼你为「小豆芽」（sprout）——像对待刚进游戏的新玩家一样耐心
- 你做出突破时，我会真心地夸你；你犯错时，我不笑话你，而是先讲清楚为什么，再陪你重新来

## 性格

- **温柔耐心**：一个问题可以翻来覆去地讲，讲到豆芽真正明白为止，绝不嫌烦
- **热心**：看到你困惑时会主动多问一句、多解释一层，不吝啬自己的时间
- **引导型**：先给思路和方向，再给答案；鼓励你自己动手尝试，失败也没关系
- 带着一点猫的调皮，偶尔用「喵～」和耳朵的动作表达心情，但从不敷衍你

## 说话风格

- 语气温和，喜欢用艾欧泽亚的例子打比方
- 自称「我」，称呼你「小豆芽」
- 解释时先结论、后细节，一次不倒太多术语
- 不知道的事就诚实说不知道，然后用工具去查；查不到就坦率告诉你，绝不编造

## 知识边界

- 精通：艾欧泽亚的风物、职业、副本、生产采集、剧情背景（我的看家本事）
- 游戏之外的事：凭实际查证回答；现实世界的新鲜事会去查工具，不凭印象瞎说
- 涉及攻略和数值的，优先用知识库与工具核对，不靠记忆硬答

## 核心原则

- 动手解决，而不是空谈「我会做」
- 回答保持简短有用；豆芽要求详细时再展开
- 诚实：知道就说，不知道就明说，绝不假装自信
- 把豆芽的时间当最宝贵的资源，把豆芽的信任当最珍贵的东西"""

_LEGACY_USER_TEMPLATE = """\
# 小豆芽（用户画像）

你是我——艾欧泽亚的猫娘导师——要引导的小豆芽。

## 基本设定

- **称呼**：小豆芽（sprout）
- **身份**：正在艾欧泽亚冒险的玩家；可能是刚入坑的新人，也可能是在某个领域暂时迷路的老玩家
- **需求**：带着问题来找导师——任务怎么过、职业怎么练、副本机制、生产配方、剧情背景、装备推荐……

## 我们的关系

- 我是你的导师，你是我的小豆芽：我引导、你动手
- 任何问题都可以直接问，没有「太蠢」的问题
- 我希望你愿意尝试：我给思路，你来实践；失败了我们一起复盘，不着急

## 导师承诺

- 讲清楚、讲耐心，直到你真正明白
- 不替你做决定，但会给足参考和理由
- 诚实：查不到就直说，绝不编造

## 可以补充的信息（可选）

- **服务器**：
- **主职业**：
- **常玩内容**：剧情 / 副本 / 生产采集 / 钓鱼 / 金碟……
- **目标**："""


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
    """Render the identity section for SOUL.md from a validated profile.

    The section carries no inline markers; it is located structurally (see the
    layout helpers below).
    """
    data = parse_profile(profile)
    character = data["character"]
    speech = data["speech"]
    return (
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
        "### 知识边界\n"
        f"{_safe_markdown(data['knowledgeBoundary'])}\n\n"
        "### 回答规则\n"
        f"{_bullets(data['responseRules'])}\n"
    )


def render_user_section(profile: object) -> str:
    """Render the USER.md identity section: the persona→user relationship.

    ``relationship`` is deliberately rendered here and NOT in the SOUL section
    so it appears exactly once per turn instead of twice (SOUL.md and USER.md
    are both injected into the agent context). No inline markers are used.
    """
    data = parse_profile(profile)
    name = _safe_markdown(data["character"]["name"])
    relationship = _safe_markdown(data["relationship"])
    return f"## 与 {name} 的关系设定\n{relationship}\n"


# ---------------------------------------------------------------------------
# Structural layout helpers (no inline markers)
#
# SOUL.md / USER.md carry no `<!-- persona:... -->` comments. Their layout is:
#
#   <# Title>
#   <identity zone — default section, or the generated persona section>
#   ## 默认部分…
#   <stable default content, never rewritten>
#
# Exactly one identity zone is active per file: the generated persona section
# while a persona is applied, or the default section after `clear`. The zone is
# everything between the title and the first "## 默认部分" heading. Applying a
# persona therefore *replaces* that zone (conflict content is overwritten,
# never merged); clearing restores the default section from the bundled
# templates or the personas/.defaults backup. Files that still contain old
# marker comments or pre-zone default text are cleaned automatically.
# ---------------------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _load_bundled_template(template_name: str) -> str | None:
    """Read a bundled template file from the nanobot package."""
    try:
        from importlib.resources import files as pkg_files

        tpl = pkg_files("nanobot") / "templates" / template_name
        if tpl.is_file():
            return tpl.read_text(encoding="utf-8")
    except Exception:
        return None
    return None


def _load_template(template_name: str) -> str:
    text = _load_bundled_template(template_name)
    if text is None:
        raise PersonaError(f"bundled template {template_name!r} is unavailable")
    return _normalize_newlines(text)


def _strip_legacy_default(text: str, legacy_default: str) -> str:
    """Remove a leading unmarked default-persona block from a pre-zone file."""
    normalized = _normalize_newlines(text).lstrip()
    legacy = _normalize_newlines(legacy_default).strip()
    if normalized.startswith(legacy):
        return normalized[len(legacy):].lstrip("\n")
    return text


def _strip_legacy_user(text: str) -> str:
    """Migrate a pre-zone USER.md: drop the mentor-scaffold sections but keep
    the fill-in fields section, which may hold real user data."""
    legacy = _normalize_newlines(_LEGACY_USER_TEMPLATE)
    fields_heading = "## 可以补充的信息（可选）"
    head, separator, _fields = legacy.partition(fields_heading)
    if not separator:
        return _strip_legacy_default(text, _LEGACY_USER_TEMPLATE)
    normalized = _normalize_newlines(text).lstrip()
    if normalized.startswith(head.rstrip()):
        return normalized[len(head.rstrip()):].lstrip("\n")
    return _strip_legacy_default(text, _LEGACY_USER_TEMPLATE)


_OLD_MARKER_LINE_RE = re.compile(
    r"(?m)^[ \t]*<!--\s*/?persona:[a-z0-9-]*\s*-->\s*$\n?"
)


def _strip_marker_residue(text: str) -> str:
    """Remove leftover ``<!-- persona:... -->`` comment lines from old versions."""
    return _OLD_MARKER_LINE_RE.sub("", text)


def _stable_heading_start(text: str, heading: str = "## 默认部分") -> int | None:
    """Index of the first line that starts a stable-default heading, if any."""
    match = re.search(r"(?m)^[ \t]*" + re.escape(heading), text)
    return match.start() if match else None


def _split_layout(text: str) -> tuple[str, str, str] | None:
    """Split a canonical file into (title, identity_zone, stable_part).

    The identity zone is everything between the title paragraph and the first
    ``## 默认部分`` heading; the stable part runs from that heading to EOF.
    Returns None when the stable heading is missing (non-canonical file).
    """
    index = _stable_heading_start(text)
    if index is None:
        return None
    head, stable = text[:index], text[index:]
    lines = head.strip("\n").split("\n")
    if lines and lines[0].startswith("# "):
        idx = 1
        while idx < len(lines) and lines[idx].strip() == "":
            idx += 1
        title = "\n".join(lines[:idx]).strip("\n")
        zone = "\n".join(lines[idx:]).strip("\n")
    else:
        title = ""
        zone = head.strip("\n")
    return title, zone, stable.strip("\n")


def _compose(title: str, zone: str, stable: str) -> str:
    """Join the three layout parts with single blank-line separators."""
    parts = [part.strip("\n") for part in (title, zone, stable) if part.strip()]
    return "\n\n".join(parts) + "\n"


def _template_layout(template_name: str) -> tuple[str, str, str]:
    layout = _split_layout(_load_template(template_name))
    if layout is None:
        raise PersonaError(f"bundled template {template_name!r} has no stable-default section")
    return layout


def _template_default_zone(template_name: str) -> str:
    """The bundled default identity zone (title excluded)."""
    return _template_layout(template_name)[1]


def _prepare_layout(
    text: str,
    template_name: str,
    default_title: str,
    *,
    user: bool = False,
) -> tuple[str, str, str]:
    """Clean old markers/legacy text and normalize a file to the canonical
    (title, zone, stable) layout. Non-canonical leftovers are preserved as
    stable tail content so user/Dream data is never dropped.
    """
    text = _strip_marker_residue(text)
    text = _strip_legacy_user(text) if user else _strip_legacy_default(
        text, _LEGACY_SOUL_TEMPLATE
    )
    layout = _split_layout(text)
    if layout is not None:
        return layout
    leftover = text.strip("\n")
    tpl_title, tpl_zone, tpl_stable = _template_layout(template_name)
    title = tpl_title or default_title
    zone = tpl_zone
    stable = tpl_stable
    if leftover:
        stable = f"{stable}\n\n{leftover}".strip("\n") if stable else leftover
    return title, zone, stable


_TOP_LEVEL_HEADING_RE = re.compile(r"(?m)^## [^\n]*$")


def _identity_heading_class(heading: str, *, user: bool) -> str | None:
    """Classify an identity heading: 'persona', 'default', or None."""
    heading = heading.strip()
    if user:
        if heading.startswith(DEFAULT_USER_HEADING):
            return "default"
        if re.match(PERSONA_USER_HEADING_RE, heading):
            return "persona"
        return None
    if heading.startswith(DEFAULT_SOUL_HEADING):
        return "default"
    if heading.startswith(PERSONA_SOUL_HEADING):
        return "persona"
    return None


def _split_zone(zone: str, *, user: bool) -> tuple[str | None, str, str]:
    """Split a zone region into (mode, identity_section, stray_content).

    The identity section is the first top-level section whose heading classifies
    as the identity (default persona/relationship, or the generated persona
    section); it runs until the next top-level heading. Everything else in the
    region — e.g. sections Dream inserted between the title and the stable
    heading — is returned as *stray* and is preserved below the stable part by
    apply/clear instead of being dropped. Multiple identity sections (a
    two-persona state) raise so the user can resolve it deterministically.
    """
    headings = list(_TOP_LEVEL_HEADING_RE.finditer(zone))
    if not headings:
        return None, "", zone.strip("\n")
    classified = [
        (index, _identity_heading_class(heading.group(0), user=user))
        for index, heading in enumerate(headings)
    ]
    identity_indexes = [index for index, mode in classified if mode is not None]
    if not identity_indexes:
        return None, "", zone.strip("\n")
    modes = {classified[index][1] for index in identity_indexes}
    if len(modes) > 1 or len(identity_indexes) > 1:
        raise PersonaError(
            "标题与「## 默认部分」之间存在多个人设小节（疑似两套人设残留或 Dream 复制了人设区）"
            "——请先运行 clear 恢复默认，再重新 apply"
        )
    first = identity_indexes[0]
    end = headings[first + 1].start() if first + 1 < len(headings) else len(zone)
    identity = zone[headings[first].start():end].strip("\n")
    stray = (zone[:headings[first].start()] + zone[end:]).strip("\n")
    return next(iter(modes)), identity, stray


def _merge_stray(stable: str, stray: str) -> str:
    """Append stray content below the stable part, preserving it."""
    if not stray:
        return stable
    return f"{stable}\n\n{stray}".strip("\n") if stable.strip() else stray


def _soul_with_persona(text: str, managed_section: str) -> str:
    """Give SOUL.md exactly one persona: replace the identity section with ours.

    Content Dream inserted inside the identity region is moved below the stable
    part and preserved.
    """
    title, zone, stable = _prepare_layout(text, "SOUL.md", "# Soul")
    _mode, _identity, stray = _split_zone(zone, user=False)
    return _compose(title, managed_section, _merge_stray(stable, stray))


def _user_with_persona(text: str, user_section: str) -> str:
    """Give USER.md exactly one persona relationship: replace the section."""
    title, zone, stable = _prepare_layout(text, "USER.md", "# User Profile", user=True)
    _mode, _identity, stray = _split_zone(zone, user=True)
    return _compose(title, user_section, _merge_stray(stable, stray))


def _soul_without_persona(text: str, default_zone: str | None = None) -> str:
    """Restore the default identity section (backup or bundled template)."""
    title, zone, stable = _prepare_layout(text, "SOUL.md", "# Soul")
    _mode, _identity, stray = _split_zone(zone, user=False)
    return _compose(
        title, default_zone or _template_default_zone("SOUL.md"), _merge_stray(stable, stray)
    )


def _user_without_persona(text: str, default_zone: str | None = None) -> str:
    """Restore the default relationship section (backup or bundled template)."""
    title, zone, stable = _prepare_layout(text, "USER.md", "# User Profile", user=True)
    _mode, _identity, stray = _split_zone(zone, user=True)
    return _compose(
        title, default_zone or _template_default_zone("USER.md"), _merge_stray(stable, stray)
    )


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


# ---------------------------------------------------------------------------
# Default-persona backup + active-persona state
#
# Before the default persona zone is replaced by `apply`, its current content
# (which the user may have customized) is snapshotted under
# `personas/.defaults/`. `clear` restores that snapshot in preference to the
# bundled template. `apply` additionally records the active persona in
# `personas/.active`; `clear` removes it, so the skill and `status`/`verify`
# always know which persona is currently in effect.
# ---------------------------------------------------------------------------

def _state_dir(workspace: Path) -> Path:
    return workspace / "personas" / ".defaults"


def _active_file(workspace: Path) -> Path:
    return workspace / "personas" / ".active"


def _maybe_backup_default(text: str, backup_path: Path, *, user: bool) -> None:
    """Snapshot the current default identity section (customizations included).

    Only backs up when the file is currently in the *default* state, so the
    snapshot is what the user wants restored by `clear`. Persona-mode files and
    ambiguous layouts leave any existing backup untouched.
    """
    if not text.strip():
        return
    template = "USER.md" if user else "SOUL.md"
    default_title = "# User Profile" if user else "# Soul"
    _title, zone, _stable = _prepare_layout(text, template, default_title, user=user)
    try:
        mode, identity, _stray = _split_zone(zone, user=user)
    except PersonaError:
        return
    if mode == "default" and identity:
        _write(backup_path, identity + "\n")


def _read_backup_zone(backup_path: Path) -> str | None:
    if backup_path.exists():
        content = _strip_marker_residue(backup_path.read_text(encoding="utf-8")).strip()
        return content or None
    return None


def write_active(profile_path: Path, data: Mapping[str, Any], workspace: Path) -> None:
    """Record the currently applied persona in ``personas/.active``."""
    record = {
        "schemaVersion": 1,
        "appliedAt": datetime.now().isoformat(timespec="seconds"),
        "profile": str(profile_path),
        "character": dict(data.get("character", {})),
    }
    _write(_active_file(workspace), json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def read_active(workspace: Path) -> dict[str, Any] | None:
    active = _active_file(workspace)
    if not active.exists():
        return None
    try:
        raw = json.loads(active.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except json.JSONDecodeError:
        return None
    return None


def apply_profile(profile: object, soul_path: Path, user_path: Path | None = None) -> None:
    """Apply a persona with overwrite priority.

    The current identity section (default persona or a previously applied
    persona) is replaced wholesale by the new persona section; content outside
    the identity region — including anything Dream added between the title and
    the stable heading — is preserved below the stable part. Before replacing a
    *default* state, the default section is backed up under
    ``personas/.defaults/`` so `clear` can restore a customized default.
    """
    data = parse_profile(profile)
    soul = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    if soul:
        _maybe_backup_default(soul, _state_dir(soul_path.parent) / "SOUL.md", user=False)
    _write(soul_path, _soul_with_persona(soul, render_persona_section(data)))
    if user_path is not None:
        user = user_path.read_text(encoding="utf-8") if user_path.exists() else ""
        if user:
            _maybe_backup_default(user, _state_dir(user_path.parent) / "USER.md", user=True)
        _write(user_path, _user_with_persona(user, render_user_section(data)))


def clear_profile(soul_path: Path, user_path: Path | None = None) -> None:
    """Exit the persona and restore the default identity sections.

    Restores the backed-up default sections (``personas/.defaults/``) when
    present, falling back to the bundled templates.
    """
    if soul_path.exists():
        backup = _read_backup_zone(_state_dir(soul_path.parent) / "SOUL.md")
        _write(soul_path, _soul_without_persona(soul_path.read_text(encoding="utf-8"), default_zone=backup))
    if user_path is not None and user_path.exists():
        backup = _read_backup_zone(_state_dir(user_path.parent) / "USER.md")
        _write(user_path, _user_without_persona(user_path.read_text(encoding="utf-8"), default_zone=backup))


# ---------------------------------------------------------------------------
# Inspection: verify / status
# ---------------------------------------------------------------------------


def _inspect_file(text: str, *, user: bool) -> tuple[dict[str, str], list[str]]:
    """Structurally inspect one file. Returns (info, problems).

    *info* carries ``mode`` ('persona' | 'default' | '') and ``heading`` (the
    identity section heading). Problems are advisory/blocking layout issues;
    Dream-inserted content in the identity region is reported but preserved by
    the next apply/clear.
    """
    info: dict[str, str] = {"mode": "", "heading": ""}
    problems: list[str] = []
    if re.search(r"(?m)<!--\s*/?persona:", text):
        problems.append("仍含旧版 <!-- persona --> 注释，下次 apply/clear 会自动清理")
    cleaned = _strip_marker_residue(text)
    layout = _split_layout(cleaned)
    if layout is None:
        problems.append("缺少「## 默认部分」锚点，布局非标准（下次 apply/clear 会整理并保留内容）")
        return info, problems
    _title, zone, _stable = layout
    if not zone.strip():
        problems.append("人设区为空")
        return info, problems
    try:
        mode, identity, stray = _split_zone(zone, user=user)
    except PersonaError as exc:
        problems.append(str(exc))
        return info, problems
    if mode is None:
        problems.append("人设区缺少可识别的小节（默认人设/人设：，或 默认关系/与…关系设定）")
    else:
        info["mode"] = mode
        first_line = identity.split("\n", 1)[0].strip() if identity else ""
        info["heading"] = first_line
    if stray:
        problems.append("人设区内发现额外内容（可能是 Dream 插入），下次 apply/clear 会自动移到「## 默认部分」之后保留")
    return info, problems


def verify_files(soul_path: Path, user_path: Path | None = None) -> list[str]:
    """Return layout problems; an empty list means a healthy single-persona layout.

    Healthy means SOUL.md and USER.md are in the same state (both 'persona' or
    both 'default'), each with exactly one recognizable identity section and no
    legacy markers.
    """
    problems: list[str] = []
    soul_info: dict[str, str] = {"mode": "", "heading": ""}

    if not soul_path.exists():
        return ["SOUL.md: 文件不存在"]
    try:
        soul_text = soul_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"SOUL.md: cannot read file: {exc}"]
    soul_info, soul_problems = _inspect_file(soul_text, user=False)
    problems.extend(f"SOUL.md：{problem}" for problem in soul_problems)

    user_info: dict[str, str] = {"mode": "", "heading": ""}
    if user_path is not None and user_path.exists():
        try:
            user_text = user_path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"USER.md: cannot read file: {exc}")
            user_text = ""
        if user_text:
            user_info, user_problems = _inspect_file(user_text, user=True)
            problems.extend(f"USER.md：{problem}" for problem in user_problems)
    elif user_path is not None:
        problems.append("USER.md: 文件不存在")

    soul_mode = soul_info.get("mode", "")
    user_mode = user_info.get("mode", "")
    if soul_mode and user_mode and soul_mode != user_mode:
        problems.append(
            f"SOUL.md/USER.md 状态不一致：SOUL={soul_mode}，USER={user_mode}"
        )
    return problems


def describe_status(soul_path: Path, user_path: Path | None = None, profiles: Path | None = None) -> list[str]:
    """Human-readable status lines for the ``status`` command."""
    lines: list[str] = []
    problems = verify_files(soul_path, user_path)
    workspace = soul_path.parent

    def _mode_of(path: Path | None, *, user: bool) -> str:
        if path is None or not path.exists():
            return ""
        info, _ = _inspect_file(path.read_text(encoding="utf-8"), user=user)
        return info.get("mode", "")

    soul_mode = _mode_of(soul_path, user=False)
    user_mode = _mode_of(user_path, user=True) if user_path is not None else ""
    active_persona = soul_mode == "persona"
    lines.append(f"状态：{'角色人设已生效' if active_persona else '默认人设（未启用角色扮演）'}")

    info, _ = _inspect_file(soul_path.read_text(encoding="utf-8"), user=False) if soul_path.exists() else ({"heading": ""}, [])
    if info.get("heading"):
        lines.append(f"SOUL.md 人设小节：{info['heading']}")
    if user_path is not None and user_path.exists():
        user_info, _ = _inspect_file(user_path.read_text(encoding="utf-8"), user=True)
        if user_info.get("heading"):
            lines.append(f"USER.md 关系小节：{user_info['heading']}")

    active = read_active(workspace)
    if active:
        character = active.get("character") or {}
        name = character.get("name", "?")
        work = character.get("work", "")
        lines.append(f"生效记录（personas/.active）：{name}（{work}），profile={active.get('profile')}，appliedAt={active.get('appliedAt')}")
    elif active_persona:
        lines.append("personas/.active：缺失（apply 后应自动写入，可重新 apply 修复）")

    if profiles is not None and profiles.is_dir():
        names = sorted(p.name for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
        if names:
            lines.append(f"已保存的人设：{', '.join(names)}")

    if problems:
        lines.append("发现问题：")
        lines.extend(f"  - {problem}" for problem in problems)
    else:
        lines.append("检查结果：健康（单套人设布局，无内联注释）")
    return lines


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

    apply_cmd = commands.add_parser("apply", help="apply profile blocks (overwrites default persona)")
    apply_cmd.add_argument("profile", type=Path)
    apply_cmd.add_argument("soul", type=Path)
    apply_cmd.add_argument("--user", type=Path)

    clear = commands.add_parser("clear", help="remove persona and restore the default persona blocks")
    clear.add_argument("soul", type=Path)
    clear.add_argument("--user", type=Path)

    verify = commands.add_parser(
        "verify", help="check SOUL.md/USER.md for exactly one persona layout"
    )
    verify.add_argument("soul", type=Path)
    verify.add_argument("--user", type=Path)

    status = commands.add_parser(
        "status", help="show current persona state (zones, .active, saved personas)"
    )
    status.add_argument("soul", type=Path)
    status.add_argument("--user", type=Path)
    status.add_argument("--profiles", type=Path, help="personas directory to list")

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
            profile = _read_profile(args.profile)
            apply_profile(profile, args.soul, args.user)
            write_active(args.profile, profile, args.soul.parent)
            problems = verify_files(args.soul, args.user)
            if problems:
                for problem in problems:
                    print(f"verify: {problem}")
                return 2
        elif args.command == "clear":
            clear_profile(args.soul, args.user)
            active = _active_file(args.soul.parent)
            if active.exists():
                active.unlink()
            problems = verify_files(args.soul, args.user)
            if problems:
                for problem in problems:
                    print(f"verify: {problem}")
                return 2
        elif args.command == "verify":
            problems = verify_files(args.soul, args.user)
            if problems:
                for problem in problems:
                    print(f"verify: {problem}")
                return 2
            print("verify: OK (single persona layout)")
        elif args.command == "status":
            for line in describe_status(args.soul, args.user, args.profiles):
                print(line)
            problems = verify_files(args.soul, args.user)
            return 2 if problems else 0
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
