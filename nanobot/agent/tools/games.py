"""Built-in FF14 game-assistant tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from pydantic import Field

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
)
from nanobot.config_base import Base
from nanobot.games.ffxiv.http import SafeHttpClient
from nanobot.games.ffxiv.knowledge_assets import (
    GuideDatabaseMode,
    resolve_guide_database,
)

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext


class GamesToolConfig(Base):
    """Configuration shared by the built-in FF14 tools."""

    enable: bool = True
    data_dir: str = "~/.nanobot/games"
    update_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    guide_database: str | None = None
    wiki_cache_mb: int = Field(default=1024, ge=16, le=16_384)


class _GameService(Protocol):
    async def execute(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class _ServiceSettings:
    data_root: Path
    update_timeout_seconds: float
    guide_database: Path
    wiki_cache_mb: int
    guide_database_mode: GuideDatabaseMode = "legacy"
    timezone_name: str = "UTC"


@dataclass(frozen=True, slots=True)
class _GameServices:
    fishing: _GameService
    knowledge: _GameService
    housing: _GameService
    market: _GameService


def _build_services(config: _ServiceSettings) -> _GameServices:
    from nanobot.games.ffxiv.fishcake import FishCakeDetailSource
    from nanobot.games.ffxiv.fishing import FishingService
    from nanobot.games.ffxiv.fishing_snapshot import FishingSnapshotManager
    from nanobot.games.ffxiv.housing import HousingMetadataSource, HousingService
    from nanobot.games.ffxiv.knowledge import DEFAULT_SOURCE_ROOT, KnowledgeService
    from nanobot.games.ffxiv.market import MarketService
    from nanobot.games.ffxiv.tool_directory import WaterCrystalDirectory
    from nanobot.games.ffxiv.wiki import FFCafeClient, WikiLookup
    from nanobot.games.ffxiv.wiki_cache import WikiCache

    http_client = SafeHttpClient(timeout_seconds=config.update_timeout_seconds)
    fishing_manager = FishingSnapshotManager(
        data_dir=config.data_root,
        client=http_client,
        ttl=timedelta(minutes=30),
        clock=lambda: datetime.now(timezone.utc),
        baseline={
            "fishCount": 1517,
            "fishWithPredators": 59,
            "predatorRelationCount": 74,
            "rarityDistribution": {
                "normal": 923,
                "bigFish": 299,
                "livingLegend": 36,
                "ikdNormalFish": 104,
                "ikdBigFish": 13,
                "ikdSpectralFish": 13,
                "ikdBlueFish": 13,
                "ikdGreenFish": 116,
            },
            "requiredFieldCompleteness": {
                "fishParameterId": 1517,
                "itemId": 1517,
                "type": 1517,
                "catchType": 1517,
                "tierType": 1517,
                "chineseItemName": 1517,
            },
            "relationshipSentinels": {
                "8775": [{"fishId": 8774, "count": 1}],
                "24994": [
                    {"fishId": 24203, "count": 3},
                    {"fishId": 23056, "count": 3},
                    {"fishId": 24204, "count": 5},
                ],
            },
        },
    )
    wiki_cache = WikiCache(
        config.data_root / "ffxiv" / "knowledge" / "wiki-cache.sqlite3",
        max_bytes=config.wiki_cache_mb * 1024 * 1024,
    )
    ffcafe = FFCafeClient(http=http_client)
    return _GameServices(
        fishing=FishingService(
            snapshot_manager=fishing_manager,
            detail_source=FishCakeDetailSource(http_client),
            timezone_name=config.timezone_name,
            clock=lambda: datetime.now(timezone.utc),
        ),
        knowledge=KnowledgeService(
            source_root=DEFAULT_SOURCE_ROOT,
            guide_database=config.guide_database,
            guide_database_mode=config.guide_database_mode,
            ffcafe=ffcafe,
            wiki=WikiLookup(http=http_client, cache=wiki_cache),
            directory=WaterCrystalDirectory(http=http_client, cache=wiki_cache),
            clock=lambda: datetime.now(timezone.utc),
            wiki_cache_mb=config.wiki_cache_mb,
        ),
        housing=HousingService(
            http=http_client,
            metadata_source=HousingMetadataSource(http_client),
            timezone_name=config.timezone_name,
            clock=lambda: datetime.now(timezone.utc),
        ),
        market=MarketService(http=http_client, items=ffcafe),
    )


class _FFXIVTool(Tool):
    """Shared discovery and configuration adapter for FF14 tools."""

    config_key = "games"
    _tool_name: ClassVar[str]
    _description: ClassVar[str]
    _actions: ClassVar[tuple[str, ...]]
    _fields: ClassVar[dict[str, object]] = {}
    _required: ClassVar[list[str]] = ["action"]
    _action_required: ClassVar[dict[str, tuple[str, ...]]] = {}
    _service_name: ClassVar[str]

    def __init__(self, *, settings: _ServiceSettings) -> None:
        self.data_root = settings.data_root
        self.guide_database = settings.guide_database
        self._settings = settings
        self._services: _GameServices | None = None
        self._services_lock = asyncio.Lock()

    @classmethod
    def config_cls(cls) -> type[GamesToolConfig]:
        return GamesToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.config.games.enable

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        config = ctx.config.games
        data_root = Path(config.data_dir).expanduser()
        guide_database = resolve_guide_database(
            config.guide_database,
            data_root=data_root,
        )
        return cls(
            settings=_ServiceSettings(
                data_root=data_root,
                update_timeout_seconds=config.update_timeout_seconds,
                guide_database=guide_database.path,
                guide_database_mode=guide_database.mode,
                wiki_cache_mb=config.wiki_cache_mb,
                timezone_name=ctx.timezone,
            )
        )

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return ObjectSchema(
            {
                "action": StringSchema("Operation to perform.", enum=self._actions),
                **self._fields,
            },
            required=self._required,
            additional_properties=False,
        ).to_json_schema()

    @property
    def read_only(self) -> bool:
        return True

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        for field in self._required:
            value = params.get(field)
            if isinstance(value, str) and not value.strip():
                errors.append(f"{field} must not be blank")
        action = params.get("action")
        if not isinstance(action, str):
            return errors
        for field in self._action_required.get(action, ()):
            value = params.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"{field} is required when action='{action}'")
        return errors

    async def execute(self, **kwargs: Any) -> Any:
        services = self._services
        if services is None:
            async with self._services_lock:
                services = self._services
                if services is None:
                    services = _build_services(self._settings)
                    self._services = services
        service = cast(_GameService, getattr(services, self._service_name))
        return await service.execute(**kwargs)


_EVIDENCE_CONTRACT = (
    "最终回答必须使用工具返回的关键事实、免责与过期 warning；默认不展示来源清单，"
    "只有用户明确询问来源时才展示 source/evidence 明细。"
    "工具返回结构化 error 时，禁止改用 web_search 或 web_fetch 或 read_file 兜底；"
    "有 suggestions 时按 suggestions 修正参数重试一次；否则按工具专属恢复流程处理，"
    "没有专属流程才如实告知并索要缺失项。"
    "不得编造数值、时间、钓点或价格，不得把推测写成事实；"
    "不得在回答文本中输出伪 tool_call 标记。"
)


class FFXIVFishingTool(_FFXIVTool):
    _service_name = "fishing"
    _tool_name = "ffxiv_fishing"
    _actions = ("fish_info", "fish_windows", "weather")
    _action_required = {"fish_info": ("fish_name",), "weather": ("zone",)}
    _description = (
        "FF14 钓鱼工具：fish_info 查询鱼类资料，fish_name 必填；即使未来 48 小时无"
        "窗口也返回钓点、鱼饵、前置鱼等静态资料。fish_windows 查询鱼窗/筛选鱼王鱼皇；"
        "weather 查询地区天气，接受官方城区名及森都、海都、沙都、伊修加德别名。"
        "查询指定鱼时，用户未明确时间范围就不要传 duration_minutes；省略后工具会返回"
        "最近 5 个窗口。只有用户明确给出时间范围时才传 duration_minutes。"
        "提醒必须先调用 ffxiv_fishing action=fish_windows include_reminder=true，"
        "再把 reminderAt/reminderMessage 传给 cron action=add 的 at/message；"
        "不得询问用户 CD 时长。"
        + _EVIDENCE_CONTRACT
        + " Delivery is best-effort: nanobot and the LLM must be available at trigger time; "
        "a reminder missed during downtime is not replayed, and a failed execution is not "
        "retried."
    )
    _fields = {
        "fish_name": StringSchema("Chinese fish name.", nullable=True),
        "region": StringSchema("Official region or reviewed nickname.", nullable=True),
        "rarity": StringSchema("Optional rarity filter.", nullable=True),
        "duration_minutes": IntegerSchema(
            description=(
                "Window search duration in minutes. Set only when the user explicitly "
                "provides a time range; omit for the next five named-fish windows."
            ),
            minimum=1,
        ),
        "zone": StringSchema("Weather zone or reviewed nickname.", nullable=True),
        "start_at": StringSchema("Optional aware ISO-8601 range start.", nullable=True),
        "include_reminder": BooleanSchema(description="Include reminder hand-off fields."),
    }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if (
            params.get("action") == "fish_windows"
            and params.get("include_reminder") is True
            and not str(params.get("fish_name") or "").strip()
        ):
            errors.append("fish_name is required when include_reminder=true")
        return errors


class FFXIVKnowledgeTool(_FFXIVTool):
    _service_name = "knowledge"
    _tool_name = "ffxiv_knowledge"
    _actions = ("search", "item", "guide")
    _description = (
        "FF14 中文知识工具：普通知识用 search，物品用途/获取用 item，任务、职业、机制和"
        "攻略用 guide。logs、采集时钟等工具站问题必须 guide + tool_site=true。"
        "检索 query 使用用户原词，不拼接 FF14 等噪声。item/search 可用于把用户简称解析为"
        "最多 3 个物品完整名称候选。sourcePath 是证据标注而非工作区文件，heading/sourceUrl"
        "同为证据字段；不要用文件工具读取它。英文百科兜底必须明确标注英文来源，未查到则如实说明。"
        + _EVIDENCE_CONTRACT
    )
    _required = ["action", "query"]
    _fields = {
        "query": StringSchema("Chinese search, item, guide, or tool-site query.", min_length=1),
        "limit": IntegerSchema(description="Maximum result count.", minimum=1, maximum=20),
        "tool_site": BooleanSchema(
            description=(
                "For guide action only: explicitly query FF14 tool websites. "
                "Uses qualified local links, then Water Crystal Station; never Wiki."
            )
        ),
    }


class FFXIVHousingTool(_FFXIVTool):
    _service_name = "housing"
    _tool_name = "ffxiv_housing"
    _actions = ("vacancies", "detail", "recommend")
    _action_required = {"detail": ("area", "ward", "plot")}
    _description = (
        "FF14 房区工具：server 必填中文服务器名或数字 ID；area 只能是海雾村、"
        "薰衣草苗圃、高脚孤丘、白银乡、穹顶皓天，不要把服务器填进 area。"
        "空房只包含“现正火热预约中！”或“即将开始抽签预约！”，公示期不算空房。"
        "每次最终回答必须包含：玩家工具上报聚合，非官方数据，可能延迟。"
        + _EVIDENCE_CONTRACT
    )
    _required = ["action", "server"]
    _fields = {
        "server": StringSchema("必填：中文服务器名或数字服务器 ID。"),
        "area": StringSchema("可选房区名，不是服务器名。", nullable=True),
        "ward": IntegerSchema(description="Optional ward number.", minimum=1),
        "plot": IntegerSchema(description="Optional plot number.", minimum=1),
        "size": StringSchema("Optional plot size.", enum=("S", "M", "L"), nullable=True),
        "phase": StringSchema(
            "Optional literal stage filter.",
            enum=("current", "upcoming", "published"),
            nullable=True,
        ),
        "eligibility": StringSchema(
            "Optional purchase eligibility.",
            enum=("personal", "free_company", "both"),
            nullable=True,
        ),
        "max_price": IntegerSchema(description="Optional maximum gil price.", minimum=0),
        "description_query": StringSchema("Optional visible-description filter.", nullable=True),
    }


class FFXIVMarketTool(_FFXIVTool):
    _service_name = "market"
    _tool_name = "ffxiv_market"
    _actions = ("price",)
    _description = (
        "FF14 市场工具：price 的 item_name 必填；scope 支持服务器、数据中心/大区和"
        "中国区，也接受“陆行鸟区”等 X区 口语。聚合价格未指定 quality 时默认 NQ。"
        "仅当用户明确要求当前挂单时设置 current_listings=true。"
        "item_name 先按用户原文查询；item_not_found 且没有可信 suggestions 时，只调用一次"
        "ffxiv_knowledge item/search 获取最多 3 个完整名称候选后重试，不要自行改名或转网页。"
        + _EVIDENCE_CONTRACT
    )
    _required = ["action", "item_name"]
    _fields = {
        "item_name": StringSchema("Chinese item name.", min_length=1),
        "scope": StringSchema("Server, data center, or region.", nullable=True),
        "quality": StringSchema("Quality filter.", enum=("any", "nq", "hq")),
        "current_listings": BooleanSchema(description="Request current listings explicitly."),
    }


def _finish_deferred_config_rebuild() -> None:
    """Complete the schema's documented lazy rebuild after an import cycle."""
    from nanobot.config import schema as config_schema

    if not config_schema.Config.__pydantic_complete__:
        getattr(config_schema, "_resolve_tool_config_refs")()


try:
    _finish_deferred_config_rebuild()
except ImportError:
    pass
