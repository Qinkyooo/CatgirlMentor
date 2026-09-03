"""Tests for the persona skill's structural (marker-free) apply/clear logic.

SOUL.md / USER.md carry no `<!-- persona:... -->` comments. Layout:

    <# Title>
    <identity section — default persona/relationship, or generated persona>
    ## 默认部分…
    <stable default content, never rewritten>

The identity section is everything between the title and the first
"## 默认部分" heading; apply/clear replace it structurally. Content Dream adds
inside that region is preserved below the stable part.
"""

from __future__ import annotations

import json
from importlib.resources import files as pkg_files
from pathlib import Path

import pytest

from nanobot.skills.persona.scripts import persona_tools as pt


def bundled(name: str) -> str:
    return (pkg_files("nanobot") / "templates" / name).read_text(encoding="utf-8")


def make_profile(name: str = "测试角色") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "character": {"name": name, "work": "测试作品", "sourceNote": "unit test"},
        "traits": ["特质一", "特质二"],
        "speech": {
            "catchphrases": ["口头禅"],
            "addressUser": "你",
            "tone": "平稳耐心",
            "taboos": ["脏话"],
        },
        "background": "一个用于测试的背景设定。",
        "relationship": f"与使用者的关系：{name} 是你的伙伴。",
        "knowledgeBoundary": "只依据测试作品设定作答。",
        "responseRules": ["先给结论", "不编造"],
    }


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def assert_no_markers(*paths: Path) -> None:
    for path in paths:
        assert "<!--" not in path.read_text(encoding="utf-8"), f"{path.name} has markers"


@pytest.fixture
def fresh_ws(tmp_path: Path) -> Path:
    """A workspace whose SOUL.md/USER.md are the untouched bundled defaults."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").write_text(bundled("SOUL.md"), encoding="utf-8")
    (ws / "USER.md").write_text(bundled("USER.md"), encoding="utf-8")
    return ws


def write_profile(tmp_path: Path, profile: dict[str, object]) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    return path


def dream_section(name: str) -> str:
    return f"\n\n## {name}\n- 内容来自 Dream 测试\n"


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_replaces_default_section_and_keeps_stable(fresh_ws: Path) -> None:
    profile = make_profile()
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    pt.apply_profile(profile, soul_path, user_path)

    soul = soul_path.read_text(encoding="utf-8")
    user = user_path.read_text(encoding="utf-8")
    assert soul.count("## 人设：测试角色（测试作品）") == 1
    assert "## 默认人设" not in soul
    assert "默认部分（不属于人设" in soul  # stable part preserved
    assert "## 与 测试角色 的关系设定" in user
    assert "## 默认关系" not in user
    assert pt.verify_files(soul_path, user_path) == []
    assert_no_markers(soul_path, user_path)


def test_relationship_rendered_once_in_user_only(fresh_ws: Path) -> None:
    profile = make_profile(name="唯一关系角色")
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    pt.apply_profile(profile, soul_path, user_path)

    soul = soul_path.read_text(encoding="utf-8")
    user = user_path.read_text(encoding="utf-8")
    assert profile["relationship"] in user
    assert profile["relationship"] not in soul


def test_reapply_overwrites_previous_persona(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    pt.apply_profile(make_profile(name="旧角色"), soul_path, user_path)
    pt.apply_profile(make_profile(name="新角色"), soul_path, user_path)

    soul = soul_path.read_text(encoding="utf-8")
    assert soul.count("## 人设：新角色（测试作品）") == 1
    assert "旧角色" not in soul
    assert pt.verify_files(soul_path, user_path) == []
    assert_no_markers(soul_path, user_path)


def test_apply_is_idempotent(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    profile = make_profile()
    pt.apply_profile(profile, soul_path, user_path)
    first_soul = soul_path.read_text(encoding="utf-8")
    first_user = user_path.read_text(encoding="utf-8")
    pt.apply_profile(profile, soul_path, user_path)
    assert soul_path.read_text(encoding="utf-8") == first_soul
    assert user_path.read_text(encoding="utf-8") == first_user


def test_apply_cleans_old_marker_comments(tmp_path: Path) -> None:
    """A file from the previous marker-based version is cleaned on apply."""
    profile = make_profile()
    soul_path = tmp_path / "SOUL.md"
    user_path = tmp_path / "USER.md"
    stable = bundled("SOUL.md").split("## 默认部分", 1)[1]
    old_active = (
        "# Soul\n\n"
        "<!-- persona:managed -->\n"
        "## 人设：旧角色（残留版本）\n"
        "旧版人设内容\n"
        "<!-- /persona:managed -->\n\n"
        f"## 默认部分{stable}"
    )
    soul_path.write_text(old_active, encoding="utf-8")
    user_path.write_text(bundled("USER.md"), encoding="utf-8")

    pt.apply_profile(profile, soul_path, user_path)
    assert_no_markers(soul_path, user_path)
    soul = soul_path.read_text(encoding="utf-8")
    assert "## 人设：测试角色（测试作品）" in soul
    assert "残留版本" not in soul
    assert pt.verify_files(soul_path, user_path) == []


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_restores_bundled_defaults(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    pt.apply_profile(make_profile(), soul_path, user_path)
    pt.clear_profile(soul_path, user_path)

    assert normalized(soul_path.read_text(encoding="utf-8")) == normalized(bundled("SOUL.md"))
    assert normalized(user_path.read_text(encoding="utf-8")) == normalized(bundled("USER.md"))
    assert pt.verify_files(soul_path, user_path) == []
    assert_no_markers(soul_path, user_path)


def test_clear_is_idempotent(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    pt.apply_profile(make_profile(), soul_path, user_path)
    pt.clear_profile(soul_path, user_path)
    after_first = soul_path.read_text(encoding="utf-8")
    pt.clear_profile(soul_path, user_path)
    assert soul_path.read_text(encoding="utf-8") == after_first


# ---------------------------------------------------------------------------
# legacy migration
# ---------------------------------------------------------------------------


def test_legacy_soul_migrates(tmp_path: Path) -> None:
    profile = make_profile()
    soul_path = tmp_path / "SOUL.md"
    legacy = pt._LEGACY_SOUL_TEMPLATE + "\n\n" + pt.render_persona_section(profile)
    soul_path.write_text(legacy, encoding="utf-8")

    pt.apply_profile(profile, soul_path)

    soul = soul_path.read_text(encoding="utf-8")
    assert "我是你的导师——一只来自艾欧泽亚的猫娘" not in soul
    assert "## 人设：测试角色（测试作品）" in soul
    assert "默认部分（不属于人设" in soul
    assert pt.verify_files(soul_path, None) == []
    assert_no_markers(soul_path)


def test_legacy_user_migrates_and_keeps_fields(tmp_path: Path) -> None:
    profile = make_profile()
    soul_path = tmp_path / "SOUL.md"
    user_path = tmp_path / "USER.md"
    soul_path.write_text(bundled("SOUL.md"), encoding="utf-8")

    legacy_user = pt._LEGACY_USER_TEMPLATE.replace(
        "- **服务器**：", "- **服务器**：国服「红玉海」"
    )
    user_path.write_text(
        legacy_user + "\n\n" + pt.render_user_section(profile), encoding="utf-8"
    )

    pt.apply_profile(profile, soul_path, user_path)

    user = user_path.read_text(encoding="utf-8")
    assert "导师承诺" not in user  # mentor scaffold dropped
    assert "国服「红玉海」" in user  # user-filled field preserved
    assert "## 与 测试角色 的关系设定" in user
    assert pt.verify_files(soul_path, user_path) == []
    assert_no_markers(soul_path, user_path)


# ---------------------------------------------------------------------------
# default-persona backup
# ---------------------------------------------------------------------------


def test_customized_default_is_backed_up_and_restored(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    custom = "（自定义默认：回答更详细一些）"
    soul_path.write_text(
        soul_path.read_text(encoding="utf-8").replace(
            "## 默认人设（未启用角色人设时生效）",
            f"## 默认人设（未启用角色人设时生效）\n\n{custom}",
            1,
        ),
        encoding="utf-8",
    )

    pt.apply_profile(make_profile(), soul_path, user_path)

    backup = fresh_ws / "personas" / ".defaults" / "SOUL.md"
    assert backup.exists()
    assert custom in backup.read_text(encoding="utf-8")

    pt.clear_profile(soul_path, user_path)
    assert custom in soul_path.read_text(encoding="utf-8")
    assert pt.verify_files(soul_path, user_path) == []


# ---------------------------------------------------------------------------
# Dream interference: content inserted around the identity section survives
# ---------------------------------------------------------------------------


def test_dream_section_between_title_and_identity_is_preserved(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    profile = make_profile()
    pt.apply_profile(profile, soul_path, user_path)
    soul_path.write_text(
        soul_path.read_text(encoding="utf-8").replace(
            "\n## 人设：", dream_section("DreamA") + "\n## 人设：", 1
        ),
        encoding="utf-8",
    )

    pt.apply_profile(make_profile(name="新角色"), soul_path, user_path)
    soul = soul_path.read_text(encoding="utf-8")
    assert "DreamA" in soul
    assert soul.index("DreamA") > soul.index("## 默认部分")  # relocated below stable
    assert soul.count("## 人设：新角色（测试作品）") == 1
    assert pt.verify_files(soul_path, user_path) == []


def test_dream_section_between_identity_and_stable_is_preserved(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    profile = make_profile()
    pt.apply_profile(profile, soul_path, user_path)
    soul_path.write_text(
        soul_path.read_text(encoding="utf-8").replace(
            "\n## 默认部分", dream_section("DreamB") + "\n## 默认部分", 1
        ),
        encoding="utf-8",
    )

    pt.apply_profile(make_profile(name="新角色"), soul_path, user_path)
    soul = soul_path.read_text(encoding="utf-8")
    assert "DreamB" in soul
    assert soul.index("DreamB") > soul.index("## 默认部分")
    assert soul.count("## 人设：新角色（测试作品）") == 1
    assert pt.verify_files(soul_path, user_path) == []


def test_dream_content_survives_clear(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    profile = make_profile()
    pt.apply_profile(profile, soul_path, user_path)
    soul_path.write_text(
        soul_path.read_text(encoding="utf-8").rstrip("\n") + dream_section("DreamC"),
        encoding="utf-8",
    )

    pt.clear_profile(soul_path, user_path)
    soul = soul_path.read_text(encoding="utf-8")
    assert "DreamC" in soul
    assert "## 默认人设" in soul
    assert "## 人设：" not in soul
    assert pt.verify_files(soul_path, user_path) == []


# ---------------------------------------------------------------------------
# verify / status / active record
# ---------------------------------------------------------------------------


def test_verify_flags_two_identity_sections(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    # Insert a second identity section inside the zone region (above the
    # default heading) -> two-persona state.
    soul_path.write_text(
        soul_path.read_text(encoding="utf-8").replace(
            "\n## 默认人设",
            "\n## 人设：多余角色（残留）\n内容\n\n## 默认人设",
            1,
        ),
        encoding="utf-8",
    )
    problems = pt.verify_files(soul_path, user_path)
    assert any("多个人设小节" in problem for problem in problems)


def test_verify_flags_mode_mismatch(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    # Apply to SOUL only: SOUL is persona mode, USER stays default -> mismatch.
    pt.apply_profile(make_profile(), soul_path)
    problems = pt.verify_files(soul_path, user_path)
    assert any("状态不一致" in problem for problem in problems)


def test_active_record_lifecycle(tmp_path: Path, fresh_ws: Path) -> None:
    profile_path = write_profile(tmp_path, make_profile(name="生效角色"))
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"

    assert pt.main(["apply", str(profile_path), str(soul_path), "--user", str(user_path)]) == 0
    active = pt.read_active(fresh_ws)
    assert active is not None
    assert active["character"]["name"] == "生效角色"

    assert pt.main(["clear", str(soul_path), "--user", str(user_path)]) == 0
    assert pt.read_active(fresh_ws) is None


def test_status_describes_persona(fresh_ws: Path) -> None:
    soul_path = fresh_ws / "SOUL.md"
    user_path = fresh_ws / "USER.md"
    assert "默认人设" in "\n".join(pt.describe_status(soul_path, user_path))
    pt.apply_profile(make_profile(), soul_path, user_path)
    lines = "\n".join(pt.describe_status(soul_path, user_path))
    assert "角色人设已生效" in lines
    assert "## 人设：测试角色" in lines


# ---------------------------------------------------------------------------
# context injection (relationship rendered once per turn)
# ---------------------------------------------------------------------------


def test_context_builder_single_relationship(tmp_path: Path) -> None:
    from nanobot.agent.context import ContextBuilder

    ws = tmp_path / "ws"
    ws.mkdir()
    profile = make_profile(name="上下文角色")
    pt.apply_profile(
        profile,
        ws / "SOUL.md",
        ws / "USER.md",
    )
    prompt = ContextBuilder(ws).build_system_prompt()
    assert prompt.count(str(profile["relationship"])) == 1
    assert "<!-- persona" not in prompt
