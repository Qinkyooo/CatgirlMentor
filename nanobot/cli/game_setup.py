"""Wizard-only setup for the source-bundled FF14 knowledge database."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nanobot.config.schema import Config
from nanobot.games.ffxiv.knowledge_assets import (
    BUNDLED_GUIDE_DATABASE,
    GuideDatabaseMode,
    resolve_guide_database,
    validate_bundled_database,
    validate_custom_database,
)


@dataclass(frozen=True, slots=True)
class GameKnowledgeSetup:
    database: Path
    mode: GuideDatabaseMode
    message: str


def prepare_game_knowledge(config: Config) -> GameKnowledgeSetup:
    configured = config.tools.games.guide_database
    resolved = resolve_guide_database(
        configured,
        data_root=Path(config.tools.games.data_dir).expanduser(),
    )
    if resolved.mode == "custom":
        info = validate_custom_database(resolved.path, full=True)
        return GameKnowledgeSetup(
            info.path,
            "custom",
            "保留并验证了自定义 FF14 中文知识库",
        )
    info = validate_bundled_database(full=True)
    if resolved.mode == "legacy":
        config.tools.games.guide_database = BUNDLED_GUIDE_DATABASE
    return GameKnowledgeSetup(
        info.path,
        "bundled",
        "已验证并绑定源码内置 FF14 中文知识库",
    )
