"""PersonaTool: manage the active roleplay persona inside the agent workspace.

The whole persona lifecycle (status / verify / preview / apply / clear /
search) runs in-process here instead of shelling out to
``python -m nanobot.skills.persona.scripts.persona_tools ...``, so it never
depends on which ``python`` the exec tool happens to resolve, and every path
is resolved against the agent workspace given by ``ToolContext.workspace`` —
never against the exec current directory / project root. This is what makes
the persona skill deterministic across environments.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.skills.persona.scripts import persona_tools as pt

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext

_SAFE_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@tool_parameters({
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["status", "verify", "preview", "apply", "clear", "search"],
            "description": "status=查看状态; verify=自检; preview=渲染预览(不写入); "
            "apply=应用人设(覆盖冲突、写入 .active/.defaults); clear=退出人设恢复默认; "
            "search=收集 FFXIV 剧情台词素材",
        },
        "profile": {
            "type": "string",
            "description": "相对 personas/ 的 profile 路径，如 honey-b-lovely/profile.json（apply/preview 用）",
        },
        "name": {
            "type": "string",
            "description": "search 用：角色名（优先中文，失败可换英文）",
        },
        "dir": {
            "type": "string",
            "description": "search 用：personas/ 下的安全目录名，如 honey-b-lovely",
        },
        "cap": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "search 用：素材条数上限，默认 500",
        },
    },
    "required": ["action"],
})
class PersonaTool(Tool):
    """Manage the active roleplay persona in the agent workspace (in-process)."""

    @property
    def name(self) -> str:
        return "persona"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(workspace=Path(ctx.workspace))

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace
        self._soul = workspace / "SOUL.md"
        self._user = workspace / "USER.md"
        self._personas = workspace / "personas"

    @property
    def description(self) -> str:
        return (
            "管理 agent workspace 里生效的角色人设（只作用于该目录的 SOUL.md/USER.md，"
            "不会写错位置）：status 查看状态、verify 自检、preview 渲染预览(不写入)、"
            "apply 应用人设(冲突整体覆盖)、clear 退出恢复默认、search 收集 FFXIV 剧情台词素材。"
            "创建人设的完整研究流程见 persona 技能。"
        )

    def _resolve_profile(self, profile: str | None) -> Path:
        if not profile:
            raise ValueError("缺少 profile 参数（相对 personas/ 的路径，如 honey-b-lovely/profile.json）")
        ref = Path(profile)
        workspace = self._workspace.resolve()
        candidates: list[Path] = []
        if not ref.is_absolute():
            candidates.append(self._workspace / "personas" / ref)
            candidates.append(self._workspace / ref)
        else:
            candidates.append(ref)
        for candidate in candidates:
            try:
                candidate.resolve().relative_to(workspace)
            except (OSError, ValueError):
                continue
            if candidate.exists():
                return candidate
        raise ValueError(
            f"找不到 profile：{profile!r}（应位于 agent workspace 的 personas/ 下，实际根目录：{workspace}）"
        )

    async def execute(self, **kwargs: Any) -> Any:
        action = kwargs.get("action")
        try:
            if action == "status":
                return "\n".join(
                    pt.describe_status(self._soul, self._user, self._personas)
                )
            if action == "verify":
                problems = pt.verify_files(self._soul, self._user)
                if problems:
                    return ToolResult.error(
                        "verify 发现问题：\n" + "\n".join(f"- {p}" for p in problems)
                    )
                return "verify: OK（单套人设布局，无内联注释）"
            if action == "preview":
                path = self._resolve_profile(kwargs.get("profile"))
                data = pt._read_profile(path)
                return (
                    pt.render_persona_section(data)
                    + "\n\n"
                    + pt.render_user_section(data)
                )
            if action == "apply":
                path = self._resolve_profile(kwargs.get("profile"))
                data = pt._read_profile(path)
                pt.apply_profile(data, self._soul, self._user)
                pt.write_active(path, data, self._workspace)
                problems = pt.verify_files(self._soul, self._user)
                if problems:
                    return ToolResult.error(
                        "apply 后校验失败：\n" + "\n".join(f"- {p}" for p in problems)
                    )
                name = (data.get("character") or {}).get("name", "?")
                return (
                    f"已应用人设：{name}（写入 agent workspace：{self._workspace}）。"
                    "新会话开始生效；本会话若缓存了旧上下文请开新对话。"
                )
            if action == "clear":
                pt.clear_profile(self._soul, self._user)
                active = pt._active_file(self._workspace)
                if active.exists():
                    active.unlink()
                problems = pt.verify_files(self._soul, self._user)
                if problems:
                    return ToolResult.error(
                        "clear 后校验失败：\n" + "\n".join(f"- {p}" for p in problems)
                    )
                return "已退出人设，恢复默认（personas/.active 已删除，默认区备份/恢复由工具维护）。"
            if action == "search":
                name = (kwargs.get("name") or "").strip()
                if not name:
                    return ToolResult.error("search 需要 name（角色名）参数")
                safe_dir = (kwargs.get("dir") or "").strip()
                if not _SAFE_DIR_RE.fullmatch(safe_dir):
                    return ToolResult.error(
                        f"dir 需要是安全目录名（字母/数字/._-，64 字符内）：{safe_dir!r}"
                    )
                cap = int(kwargs.get("cap") or 500)
                items, truncated = pt.collect_story_context(
                    pt._urlopen_fetch, name, cap=cap
                )
                out_dir = self._personas / safe_dir
                out_dir.mkdir(parents=True, exist_ok=True)
                out = out_dir / "source-dialogue.md"
                out.write_text(
                    pt.render_dialogue_source(name, items, truncated=truncated),
                    encoding="utf-8",
                )
                summary = (
                    f"剧情台词素材已写入 personas/{safe_dir}/source-dialogue.md"
                    f"（{len(items)} 条{'，已截断' if truncated else ''}，角色名：{name}）。"
                    "如需全文请用文件工具读取该文件。"
                )
                return summary
            return ToolResult.error(f"未知 action：{action!r}")
        except Exception as exc:
            return ToolResult.error(f"persona {action}: {exc}")
