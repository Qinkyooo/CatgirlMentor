"""FF14 PvP rotation service: offline calendar driven by a rules snapshot."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from nanobot.games.ffxiv.http import FetchError, SafeHttpClient
from nanobot.games.ffxiv.pvp_rules import (
    RotationModeRules,
    RotationRulesBundle,
    RotationRulesError,
    host_for_url,
    parse_rotation_rules,
    resolve_timezone,
)
from nanobot.games.ffxiv.result import error_result, success_result
from nanobot.games.ffxiv.types import Evidence, Freshness

DEFAULT_TIMEZONE: Final[str] = "Asia/Shanghai"
MAX_RULES_BYTES: Final[int] = 256 * 1024
DEFAULT_CACHE_SECONDS: Final[float] = 1800.0
_ENTRY_GUARD: Final[int] = 4096

_DRIFT_SUGGESTION: Final[tuple[str, ...]] = (
    "上游轮换规则已变更，请更新规则快照（nanobot/games/ffxiv/data/pvp-rules.json）后发布。",
    "在快照更新前，请以游戏内任务搜索器显示的地图为准。",
)
_NETWORK_SUGGESTION: Final[tuple[str, ...]] = (
    "本次无法访问上游规则地址，结果按已核验快照推算；如需强制校验请检查网络或 tools.games.pvpRulesUrl。",
)


def _slot(
    rules: RotationModeRules,
    at: datetime,
    zone: ZoneInfo,
    name_for: Callable[[str], str],
) -> dict[str, Any]:
    index = (at - rules.reference) // rules.interval
    start = rules.reference + index * rules.interval
    end = start + rules.interval
    map_id = rules.order[index % len(rules.order)]
    return {
        "mapId": map_id,
        "mapName": name_for(map_id),
        "start": start.astimezone(zone).isoformat(),
        "end": end.astimezone(zone).isoformat(),
        "remainingSeconds": max(0, int((end - at).total_seconds())),
    }


class PVPService:
    """Answer PvP rotation questions from a versioned rules snapshot.

    Offline by default. When ``rules_url`` is configured the upstream snapshot is
    fetched and its rotation digest compared with the bundled one; a mismatch is
    reported as an error instead of silently answering from stale rules.
    """

    def __init__(
        self,
        *,
        rules: RotationRulesBundle,
        http: SafeHttpClient | None = None,
        rules_url: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
    ) -> None:
        self._rules = rules
        self._http = http
        self._rules_url = rules_url.strip() if rules_url and rules_url.strip() else None
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cache: tuple[datetime, RotationRulesBundle] | None = None

    async def _fetch_remote(self) -> RotationRulesBundle:
        if self._http is None or self._rules_url is None:
            raise RotationRulesError("pvp_rules_unavailable", "未配置远端规则校验")
        host = host_for_url(self._rules_url)
        if not host:
            raise RotationRulesError(
                "pvp_rules_url_invalid", f"规则地址缺少主机名: {self._rules_url}"
            )
        response = await self._http.get_bytes(
            self._rules_url,
            allowed_hosts=frozenset({host}),
            max_bytes=MAX_RULES_BYTES,
        )
        return parse_rotation_rules(response.body, origin=self._rules_url)

    async def _active_rules(self) -> tuple[RotationRulesBundle, tuple[str, ...], str]:
        """Resolve active rules; raise only when drift is positively detected."""
        if self._http is None or self._rules_url is None:
            return self._rules, (), "miss"

        reference = self._clock()
        cached = self._cache
        if cached is not None:
            age = (reference - cached[0]).total_seconds()
            if 0 <= age < self._cache_seconds:
                return cached[1], (), "hit"

        try:
            remote = await self._fetch_remote()
        except FetchError:
            return (
                self._rules,
                (
                    "本次无法校验社区轮换规则是否已更新，以下结果按已核验的内置规则快照推算。",
                ),
                "miss",
            )

        if remote.rotation_digest != self._rules.rotation_digest:
            raise RotationRulesError(
                "pvp_rules_outdated",
                "上游社区轮换规则与内置快照不一致，地图或轮换顺序可能已改版；"
                "本工具不用过期规则推算。",
            )
        self._cache = (reference, remote)
        return remote, (), "miss"

    async def execute(self, **kwargs: Any) -> Any:
        try:
            rules, warnings, cache_status = await self._active_rules()
        except RotationRulesError as exc:
            suggestions = _DRIFT_SUGGESTION if exc.code == "pvp_rules_outdated" else ()
            return error_result(exc.code, str(exc), suggestions=suggestions)
        try:
            return self._answer(
                kwargs, rules=rules, warnings=warnings, cache_status=cache_status
            )
        except RotationRulesError as exc:
            return error_result(exc.code, str(exc))

    def _answer(
        self,
        kwargs: dict[str, Any],
        *,
        rules: RotationRulesBundle,
        warnings: tuple[str, ...],
        cache_status: str,
    ) -> Any:
        action = kwargs["action"]
        mode = kwargs.get("mode", "frontline")
        mode_rules = rules.rules_for(mode)
        zone = resolve_timezone(kwargs.get("timezone", DEFAULT_TIMEZONE))
        now = self._clock().astimezone(UTC)
        at = self._resolve_at(kwargs, now=now)

        if action == "calendar":
            day = self._resolve_day(kwargs, at=at, zone=zone)
            start = datetime.combine(day, time(), zone).astimezone(UTC)
            end = datetime.combine(day + timedelta(days=kwargs.get("days", 7)), time(), zone)
            end = end.astimezone(UTC)
        else:
            start = at
            end = at + timedelta(hours=kwargs.get("hours", 30))

        if action == "current":
            current = _slot(mode_rules, start, zone, rules.name_for)
            following = _slot(
                mode_rules,
                datetime.fromisoformat(current["end"]),
                zone,
                rules.name_for,
            )
            data: dict[str, Any] = {"current": current, "next": following}
        else:
            data = {
                "entries": self._entries(
                    mode_rules, start=start, end=end, zone=zone, rules=rules
                )
            }

        return success_result(
            kind="pvp_rotation",
            data={
                **data,
                "mode": mode,
                "timezone": str(zone),
                "calculatedAt": now,
                "ruleVerifiedAt": rules.verified_at,
                "ruleSourceUrl": rules.source_url,
                "basis": "community_calendar_prediction",
                "regionParityVerified": False,
            },
            evidence=(
                Evidence(
                    source="FFXIV PvP Calendar",
                    source_url=rules.source_url,
                    excerpt=(
                        f"社区轮换规则快照，核验日期 {rules.verified_at}"
                        if rules.verified_at
                        else "社区轮换规则快照，未标注核验日期"
                    ),
                ),
            ),
            freshness=Freshness(
                source="community_rules",
                source_updated_at=None,
                cache_status=cache_status,
                stale=False,
            ),
            warnings=(
                "依据社区日历规则离线推算，非游戏服务器实时查询；"
                "若游戏内任务搜索器显示不同，以游戏内为准。",
                *warnings,
            ),
        )

    @staticmethod
    def _resolve_at(kwargs: dict[str, Any], *, now: datetime) -> datetime:
        raw = kwargs.get("at")
        if raw is None:
            return now
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise RotationRulesError(
                "invalid_pvp_query", f"at 不是合法的 ISO-8601 时间: {raw!r}"
            ) from exc
        if moment.tzinfo is None:
            raise RotationRulesError(
                "invalid_pvp_query", "at 必须包含时区，例如 +08:00"
            )
        try:
            return moment.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise RotationRulesError("invalid_pvp_query", "at 超出可计算范围") from exc

    @staticmethod
    def _resolve_day(kwargs: dict[str, Any], *, at: datetime, zone: ZoneInfo) -> date:
        raw = kwargs.get("date")
        if raw is None:
            return at.astimezone(zone).date()
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise RotationRulesError(
                "invalid_pvp_query", f"date 不是合法的 YYYY-MM-DD: {raw!r}"
            ) from exc

    @staticmethod
    def _entries(
        mode_rules: RotationModeRules,
        *,
        start: datetime,
        end: datetime,
        zone: ZoneInfo,
        rules: RotationRulesBundle,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        cursor = start
        guard = 0
        while cursor < end:
            guard += 1
            if guard > _ENTRY_GUARD:
                raise RotationRulesError("invalid_pvp_query", "查询范围过大，请缩小 days/hours")
            row = _slot(mode_rules, cursor, zone, rules.name_for)
            boundary = min(datetime.fromisoformat(row["end"]), end)
            row["start"] = cursor.astimezone(zone).isoformat()
            row["end"] = boundary.astimezone(zone).isoformat()
            row.pop("remainingSeconds")
            entries.append(row)
            cursor = boundary.astimezone(UTC)
        return entries
