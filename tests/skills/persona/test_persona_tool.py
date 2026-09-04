"""Tests for the in-process persona tool (environment/path-proof lifecycle)."""

from __future__ import annotations

import json
from importlib.resources import files as pkg_files
from pathlib import Path

import pytest

from nanobot.agent.tools.persona import PersonaTool


def bundled(name: str) -> str:
    return (pkg_files("nanobot") / "templates" / name).read_text(encoding="utf-8")


def make_profile(name: str = "工具测试角色") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "character": {"name": name, "work": "测试作品", "sourceNote": "unit test"},
        "traits": ["特质一"],
        "speech": {
            "catchphrases": ["口头禅"],
            "addressUser": "你",
            "tone": "平稳",
            "taboos": ["脏话"],
        },
        "background": "用于测试工具的背景。",
        "relationship": f"与使用者的关系：{name} 是你的伙伴。",
        "knowledgeBoundary": "仅测试设定。",
        "responseRules": ["先给结论"],
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").write_text(bundled("SOUL.md"), encoding="utf-8")
    (ws / "USER.md").write_text(bundled("USER.md"), encoding="utf-8")
    return ws


def write_profile(ws: Path, profile: dict[str, object], name: str = "t1") -> Path:
    out = ws / "personas" / name / "profile.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    return out


async def test_tool_lifecycle_apply_clear(workspace: Path) -> None:
    profile = make_profile(name="蜂蜂测试")
    write_profile(workspace, profile)
    tool = PersonaTool(workspace=workspace)

    result = await tool.execute(action="status")
    assert "默认人设" in result

    result = await tool.execute(action="apply", profile="t1/profile.json")
    assert "已应用人设" in result
    soul = (workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "## 人设：蜂蜂测试（测试作品）" in soul
    assert "## 默认人设" not in soul
    assert (workspace / "personas" / ".active").exists()

    result = await tool.execute(action="status")
    assert "角色人设已生效" in result
    result = await tool.execute(action="verify")
    assert "OK" in result

    result = await tool.execute(action="clear")
    assert "已退出人设" in result
    assert not (workspace / "personas" / ".active").exists()
    assert "## 默认人设" in (workspace / "SOUL.md").read_text(encoding="utf-8")


async def test_tool_preview_does_not_write(workspace: Path) -> None:
    profile = make_profile(name="预览角色")
    write_profile(workspace, profile)
    tool = PersonaTool(workspace=workspace)

    result = await tool.execute(action="preview", profile="t1/profile.json")
    assert "## 人设：预览角色（测试作品）" in result
    # preview must not touch SOUL.md / USER.md / .active
    assert "## 默认人设" in (workspace / "SOUL.md").read_text(encoding="utf-8")
    assert not (workspace / "personas" / ".active").exists()


async def test_tool_rejects_bad_inputs(workspace: Path) -> None:
    tool = PersonaTool(workspace=workspace)

    result = await tool.execute(action="nope")
    assert "未知 action" in result

    result = await tool.execute(action="apply")  # missing profile
    assert "profile" in result

    result = await tool.execute(action="search", name="测试", dir="bad/../dir")
    assert "安全目录名" in result


async def test_tool_never_escapes_workspace(workspace: Path) -> None:
    write_profile(workspace, make_profile(), name="t1")
    tool = PersonaTool(workspace=workspace)
    result = await tool.execute(action="apply", profile="../../outside/profile.json")
    assert "找不到 profile" in result
    assert not (workspace.parent / "outside").exists()
