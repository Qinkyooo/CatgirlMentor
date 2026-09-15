"""Offline PvP calendar, calculated from a versioned community rules snapshot."""

from __future__ import annotations

from typing import ClassVar

from nanobot.agent.tools.games import _FFXIVTool
from nanobot.agent.tools.schema import IntegerSchema, StringSchema


class FFXIVPVPTool(_FFXIVTool):
    """Offline PvP rotation tool backed by a data snapshot, not a code constant."""

    _service_name = "pvp"
    _tool_name = "ffxiv_pvp"
    _actions = ("current", "calendar", "timeline")
    _description = (
        "FF14 PvP 轮换：今天战场/纷争前线、下一张地图、切换倒计时、日期日历和水晶冲突时间表。"
        "优先调用本工具，离线计算，不需要网页搜索、知识库扫描或 exec。"
        "current 返回当前和下一张；calendar 默认 7 天；timeline 默认 30 小时。"
        "结果为社区规则推算，回答必须说明来源与不确定性，并保留工具返回的 warning；"
        "不含职业强度推荐或胜利分数。"
        "返回 pvp_rules_outdated 时说明上游规则已改版，应如实告知并建议以游戏内为准，"
        "不要改用网页搜索猜测地图，也不要自行修改换算公式。"
    )
    _fields: ClassVar[dict[str, object]] = {
        "mode": StringSchema(
            description="frontline=纷争前线（默认），cc=水晶冲突。",
            enum=("frontline", "cc"),
            nullable=True,
        ),
        "at": StringSchema(
            description="可选 ISO 时间，必须带时区；不填使用真实当前时间。",
            nullable=True,
        ),
        "date": StringSchema(
            description="calendar 可选起始日期 YYYY-MM-DD。",
            nullable=True,
        ),
        "days": IntegerSchema(
            description="calendar 天数，默认 7。", minimum=1, maximum=31
        ),
        "hours": IntegerSchema(
            description="timeline 小时数，默认 30。", minimum=1, maximum=48
        ),
        "timezone": StringSchema(description="默认 Asia/Shanghai。", nullable=True),
    }
