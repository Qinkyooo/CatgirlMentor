"""Local-first knowledge service exposed by the FF14 knowledge tool."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .knowledge_assets import (
    GuideDatabaseMode,
    KnowledgeDatabaseError,
    validate_bundled_database,
    validate_custom_database,
)
from .knowledge_search import KnowledgeSearchHit, search_knowledge
from .result import error_result, success_result
from .tool_directory import DirectoryLookupResult
from .types import Evidence, Freshness
from .wiki import GameFact, ItemCandidate, WikiLookupResult, WikiSourceError

DEFAULT_SOURCE_ROOT = Path(r"D:\gamebot\知识库素材--中文")
_ITEM_SUFFIX = re.compile(
    r"(?:是什[么麼]|有什么用|用途|(?:怎么|如何|在哪(?:里|儿))?(?:获得|获取|取得|入手)(?:方式|方法)?)?[?？。]*$"
)
_GUIDE_INTENT_WORDS = (
    "怎么练",
    "如何练",
    "怎么打",
    "怎么玩",
    "如何玩",
    "升级",
    "循环",
    "攻略",
    "指南",
    "玩法",
)
_GAME_NAME_NOISE = re.compile(r"(?:ff\s*14|最终幻想\s*14)", re.IGNORECASE)


class _FFCafe(Protocol):
    async def search_items(self, name: str) -> tuple[ItemCandidate, ...]: ...

    async def game_fact(self, item_id: int) -> GameFact: ...


class _Wiki(Protocol):
    async def lookup(
        self, title: str, *, now: datetime | None = None
    ) -> WikiLookupResult: ...


class _Directory(Protocol):
    async def lookup(
        self, query: str, *, now: datetime | None = None
    ) -> DirectoryLookupResult: ...


def _retrieval_query(query: str) -> str:
    value = _GAME_NAME_NOISE.sub(" ", query)
    return " ".join(value.split()).strip(" ?？。")


def _item_name(query: str) -> str:
    original = " ".join(query.split()).strip()
    value = _retrieval_query(query) or original
    stripped = _ITEM_SUFFIX.sub("", value).strip()
    return stripped or value


def _guide_subject(query: str) -> str:
    value = _retrieval_query(query)
    for word in _GUIDE_INTENT_WORDS:
        value = value.replace(word, "")
    return value.strip(" ?？。")


def _local_evidence(hits: tuple[KnowledgeSearchHit, ...]) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(
            source="local_chinese_knowledge",
            source_path=hit.source_path,
            heading=" / ".join(hit.heading_path) or None,
            excerpt=hit.text[:500],
        )
        for hit in hits
    )


def _local_data(hits: tuple[KnowledgeSearchHit, ...]) -> dict[str, object]:
    return {
        "results": [
            {
                "text": hit.text,
                "sourcePath": hit.source_path,
                "headingPath": hit.heading_path,
                "sourceLineStart": hit.source_line_start,
                "sourceLineEnd": hit.source_line_end,
                "rankReason": hit.rank_reason,
                "score": hit.score,
                "entityName": hit.entity_name,
            }
            for hit in hits
        ]
    }


def _local_freshness() -> Freshness:
    return Freshness(
        source="local_chinese_knowledge",
        source_updated_at=None,
        cache_status="fresh",
        stale=False,
    )


class KnowledgeService:
    def __init__(
        self,
        *,
        source_root: Path,
        guide_database: Path,
        ffcafe: _FFCafe,
        wiki: _Wiki,
        directory: _Directory,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        wiki_cache_mb: int = 0,
        guide_database_mode: GuideDatabaseMode = "legacy",
    ) -> None:
        self.source_root = Path(source_root).expanduser().resolve(strict=False)
        self.guide_database = Path(guide_database).expanduser().resolve(strict=False)
        self.wiki_cache_mb = wiki_cache_mb
        self.guide_database_mode = guide_database_mode
        self._ffcafe = ffcafe
        self._wiki = wiki
        self._directory = directory
        self._clock = clock

    def _database_error(self) -> Any | None:
        if self.guide_database_mode == "bundled":
            try:
                validate_bundled_database(full=False)
            except KnowledgeDatabaseError as exc:
                return error_result(
                    exc.code,
                    str(exc),
                    suggestions=(
                        "重新获取完整源码，确保 guide.sqlite3 与 guide.manifest.json 配套",
                    ),
                )
            return None
        if self.guide_database_mode == "custom":
            try:
                validate_custom_database(self.guide_database, full=False)
            except KnowledgeDatabaseError as exc:
                return error_result(exc.code, str(exc))
            return None
        if self.guide_database.is_file():
            return None
        return error_result(
            "knowledge_build_required",
            (
                f"中文知识库尚未构建。素材目录: {self.source_root}; "
                f"目标数据库: {self.guide_database}"
            ),
            suggestions=(
                "运行 python -m nanobot.games.ffxiv.knowledge_build "
                f"--source \"{self.source_root}\" --database \"{self.guide_database}\"",
            ),
        )

    def _search(self, query: str, limit: int) -> tuple[KnowledgeSearchHit, ...]:
        return search_knowledge(
            self.guide_database,
            query,
            limit=limit,
            max_chars=2400,
        )

    async def _wiki_result(self, query: str) -> Any:
        result = await self._wiki.lookup(query, now=self._clock())
        if result.page is None:
            return error_result(
                "knowledge_not_found",
                "本地知识库和远程百科均未找到可靠结果。",
                suggestions=("换用更完整的正式名称重试",),
            )
        page = result.page
        warnings = list(result.warnings)
        if page.language == "en":
            warnings.append("该兜底内容来自英文来源，需明确标注后再用中文概括。")
        return success_result(
            kind="knowledge_wiki",
            data={
                "title": page.canonical_title,
                "language": page.language,
                "englishSource": page.language == "en",
                "text": page.normalized_text[:2400],
                "unavailable": False,
            },
            evidence=(
                Evidence(
                    source=page.source,
                    source_url=page.source_url,
                    excerpt=page.normalized_text[:500],
                ),
            ),
            freshness=Freshness(
                source=page.source,
                source_updated_at=page.fetched_at,
                cache_status="stale" if result.stale else "fresh",
                stale=result.stale,
            ),
            warnings=warnings,
        )

    async def guide(self, query: str, *, limit: int) -> Any:
        hits = self._search(query, limit)
        subject = _guide_subject(query)
        if not hits and subject and subject != query:
            hits = self._search(subject, limit)
        if hits:
            return success_result(
                kind="knowledge_guide",
                data=_local_data(hits),
                evidence=_local_evidence(hits),
                freshness=_local_freshness(),
            )
        return await self._wiki_result(query)

    async def search(self, query: str, *, limit: int) -> Any:
        hits = self._search(query, limit)
        cleaned = _retrieval_query(query)
        if not hits and cleaned and cleaned != query:
            hits = self._search(cleaned, limit)
        if hits:
            return success_result(
                kind="knowledge_search",
                data=_local_data(hits),
                evidence=_local_evidence(hits),
                freshness=_local_freshness(),
            )
        return await self._wiki_result(query)

    def _qualified_local_tools(
        self,
        hits: tuple[KnowledgeSearchHit, ...],
    ) -> tuple[dict[str, object], ...]:
        entries: list[dict[str, object]] = []
        connection = sqlite3.connect(
            f"{self.guide_database.as_uri()}?mode=ro", uri=True
        )
        try:
            for hit in hits:
                links = connection.execute(
                    """
                    SELECT l.label, l.destination, l.source_line
                    FROM document_links AS l
                    JOIN documents AS d ON d.id = l.document_id
                    WHERE d.source_path = ?
                      AND l.source_line BETWEEN ? AND ?
                    ORDER BY l.source_line, l.destination
                    """,
                    (hit.source_path, hit.source_line_start, hit.source_line_end),
                )
                for label, destination, line in links:
                    if (
                        not isinstance(label, str)
                        or not isinstance(destination, str)
                        or not isinstance(line, int)
                    ):
                        continue
                    parsed = urlsplit(destination)
                    description = hit.text.strip()
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.netloc
                        or not description
                        or description == label
                    ):
                        continue
                    entries.append(
                        {
                            "name": label,
                            "url": destination,
                            "category": "本地中文知识库",
                            "description": description[:500],
                            "sourcePath": hit.source_path,
                            "heading": " / ".join(hit.heading_path) or None,
                            "sourceLine": line,
                        }
                    )
        finally:
            connection.close()
        return tuple(entries)

    async def tool_directory(self, query: str, *, limit: int) -> Any:
        hits = self._search(query, limit)
        local = self._qualified_local_tools(hits)
        if local:
            return success_result(
                kind="knowledge_tool_directory",
                data={"entries": local[:limit], "unavailable": False},
                evidence=tuple(
                    Evidence(
                        source="local_chinese_knowledge",
                        source_url=str(item["url"]),
                        source_path=str(item["sourcePath"]),
                        heading=(str(item["heading"]) if item["heading"] else None),
                        excerpt=str(item["description"]),
                    )
                    for item in local[:limit]
                ),
                freshness=_local_freshness(),
            )

        result = await self._directory.lookup(query, now=self._clock())
        if result.unavailable:
            return error_result(
                "tool_directory_unavailable",
                "本地知识库没有合格网址说明，水晶驿站当前也不可用。",
            )
        entries = result.entries[:limit]
        if not entries:
            return error_result(
                "tool_directory_not_found",
                "未找到匹配的 FF14 工具站条目。",
                suggestions=("改用 action=guide 且 tool_site=false 查询普通攻略",),
            )
        return success_result(
            kind="knowledge_tool_directory",
            data={"entries": entries, "unavailable": False},
            evidence=tuple(
                Evidence(
                    source="水晶驿站工具名录",
                    source_url=item.url,
                    excerpt=item.description,
                )
                for item in entries
            ),
            freshness=Freshness(
                source="水晶驿站工具名录",
                source_updated_at=result.fetched_at,
                cache_status="stale" if result.stale else "fresh",
                stale=result.stale,
            ),
            warnings=result.warnings,
        )

    async def item(self, query: str, *, limit: int) -> Any:
        name = _item_name(query)
        local_hits = self._search(name, limit)
        try:
            candidates = await self._ffcafe.search_items(name)
            if len(candidates) > 1:
                return error_result(
                    "ambiguous_item",
                    "物品名称不唯一，请从候选中指定一个。",
                    suggestions=tuple(
                        f"{item.name_zh}（ID {item.item_id}）" for item in candidates
                    ),
                )
            if len(candidates) == 1:
                fact = await self._ffcafe.game_fact(candidates[0].item_id)
                evidence = [*_local_evidence(local_hits)]
                evidence.append(
                    Evidence(
                        source="FFCafe Item",
                        source_url=(
                            "https://xivapi-v2.xivcdn.com/api/sheet/Item/"
                            f"{fact.item_id}?language=chs"
                        ),
                        excerpt=f"{fact.name_zh}；{fact.item_type}；{fact.description}",
                    )
                )
                evidence.extend(
                    Evidence(
                        source=method.source,
                        source_url=method.source_url,
                        excerpt=method.summary,
                    )
                    for method in fact.acquisition_methods
                )
                warnings = (
                    ("获取途径覆盖范围为 partial，不能据此声称已穷尽所有来源。",)
                    if fact.acquisition_coverage == "partial"
                    else ()
                )
                return success_result(
                    kind="knowledge_item",
                    data=fact,
                    evidence=evidence,
                    freshness=Freshness(
                        source="FFCafe XIVAPI v2",
                        source_updated_at=None,
                        cache_status="fresh",
                        stale=False,
                    ),
                    warnings=warnings,
                )
        except WikiSourceError:
            pass
        return await self._wiki_result(name)

    async def execute(self, **kwargs: Any) -> Any:
        action = kwargs.get("action")
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            return error_result(
                "invalid_query",
                "请提供非空 query（中文主题、物品名或工具名）。",
                suggestions=("龙骑士循环", "铁矿怎么获得", "采集时钟"),
            )
        limit_value = kwargs.get("limit", 8)
        if not isinstance(limit_value, int) or isinstance(limit_value, bool):
            return error_result("invalid_limit", "limit 必须是整数。")
        limit = max(1, min(limit_value, 20))
        database_error = self._database_error()
        if database_error is not None:
            return database_error
        if action == "guide":
            if kwargs.get("tool_site") is True:
                return await self.tool_directory(query, limit=limit)
            return await self.guide(query, limit=limit)
        if action == "search":
            return await self.search(query, limit=limit)
        if action == "item":
            return await self.item(query, limit=limit)
        return error_result("invalid_action", f"不支持的知识库 action: {action}")
