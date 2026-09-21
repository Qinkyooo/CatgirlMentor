"""Deterministic local fishing-window calculations."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fishcake import FishCakeFormatError, FishCakeGuide
from .fishing_repo import FishingRepository, normalize_name
from .fishing_snapshot import (
    FishingSnapshotStatus,
    FishingSnapshotUnavailableError,
)
from .fishing_types import Fish, Predator, WeatherRate
from .http import FetchError
from .result import error_result, success_result
from .types import Evidence, Freshness
from .weather import (
    WEATHER_PERIOD_SECONDS,
    WeatherError,
    iter_weather_periods,
    weather_pair_at,
)

EORZEAN_HOUR_SECONDS = 175
EORZEAN_DAY_SECONDS = 24 * EORZEAN_HOUR_SECONDS


class FishingWindowError(ValueError):
    """A fishing window query or its normalized conditions are invalid."""


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FishingWindowError("fishing window datetimes must be timezone-aware")


@dataclass(frozen=True, order=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start)
        _require_aware(self.end)
        if self.start >= self.end:
            raise FishingWindowError("fishing window start must be before end")

    def intersect(self, other: TimeWindow) -> TimeWindow | None:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return TimeWindow(start, end) if start < end else None


@dataclass(frozen=True, slots=True)
class ConditionEvidence:
    time_restricted: bool
    current_weather_id: int | None
    previous_weather_id: int | None


@dataclass(frozen=True, slots=True)
class MatchedFishingWindow:
    window: TimeWindow
    evidence: ConditionEvidence


@dataclass(frozen=True, slots=True)
class IntuitionRow:
    window: TimeWindow
    buff_window: TimeWindow
    predators: tuple[Predator, ...]
    intuition_seconds: int
    dormancy_seconds: int | None

    @property
    def duration_seconds(self) -> int:
        return int((self.window.end - self.window.start).total_seconds())


def _time_matches(fish: Fish, unix_seconds: float) -> bool:
    if fish.start_hour is None and fish.end_hour is None:
        return True
    if fish.start_hour is None or fish.end_hour is None:
        raise FishingWindowError("fish must define both time boundaries or neither")
    hour = (unix_seconds / EORZEAN_HOUR_SECONDS) % 24
    if fish.start_hour < fish.end_hour:
        return fish.start_hour <= hour < fish.end_hour
    if fish.start_hour > fish.end_hour:
        return hour >= fish.start_hour or hour < fish.end_hour
    return True


def _candidate_timestamps(fish: Fish, start: datetime, end: datetime) -> tuple[float, ...]:
    start_timestamp = start.timestamp()
    end_timestamp = end.timestamp()
    candidates = {start_timestamp, end_timestamp}
    if fish.start_hour is not None and fish.end_hour is not None:
        first_day = math.floor(start_timestamp / EORZEAN_DAY_SECONDS) - 1
        last_day = math.ceil(end_timestamp / EORZEAN_DAY_SECONDS) + 1
        for day in range(first_day, last_day + 1):
            for hour in (fish.start_hour, fish.end_hour):
                boundary = day * EORZEAN_DAY_SECONDS + hour * EORZEAN_HOUR_SECONDS
                if start_timestamp < boundary < end_timestamp:
                    candidates.add(boundary)
        # Nearest start-of-window boundary before the query start, so an
        # already-running window reports its true start instead of the query time.
        day = math.floor(start_timestamp / EORZEAN_DAY_SECONDS)
        previous_start_boundary = (
            day * EORZEAN_DAY_SECONDS + fish.start_hour * EORZEAN_HOUR_SECONDS
        )
        if previous_start_boundary >= start_timestamp:
            previous_start_boundary -= EORZEAN_DAY_SECONDS
        if start_timestamp - EORZEAN_DAY_SECONDS < previous_start_boundary < start_timestamp:
            candidates.add(float(previous_start_boundary))
    if fish.weather_ids or fish.previous_weather_ids:
        lookback_timestamp = min(candidates)
        boundary = (
            math.floor(lookback_timestamp / WEATHER_PERIOD_SECONDS)
            * WEATHER_PERIOD_SECONDS
        )
        while boundary < end_timestamp:
            candidates.add(float(boundary))
            boundary += WEATHER_PERIOD_SECONDS
    return tuple(sorted(candidates))


def fish_windows(
    fish: Fish,
    *,
    start: datetime,
    end: datetime,
    weather_rate: WeatherRate | None = None,
) -> tuple[MatchedFishingWindow, ...]:
    _require_aware(start)
    _require_aware(end)
    if start >= end:
        return ()
    uses_weather = bool(fish.weather_ids or fish.previous_weather_ids)
    if uses_weather and weather_rate is None:
        raise FishingWindowError("fish weather conditions require a weather rate")

    timezone = start.tzinfo
    assert timezone is not None
    timestamps = _candidate_timestamps(fish, start, end)
    matches: list[MatchedFishingWindow] = []
    for left, right in zip(timestamps, timestamps[1:]):
        probe = (left + right) / 2
        if not _time_matches(fish, probe):
            continue
        current_weather_id: int | None = None
        previous_weather_id: int | None = None
        if uses_weather:
            assert weather_rate is not None
            try:
                pair = weather_pair_at(datetime.fromtimestamp(probe, tz=timezone), weather_rate)
            except WeatherError as exc:
                raise FishingWindowError(str(exc)) from exc
            if fish.weather_ids and pair.current_weather_id not in fish.weather_ids:
                continue
            if (
                fish.previous_weather_ids
                and pair.previous_weather_id not in fish.previous_weather_ids
            ):
                continue
            current_weather_id = pair.current_weather_id if fish.weather_ids else None
            previous_weather_id = (
                pair.previous_weather_id if fish.previous_weather_ids else None
            )
        matched = MatchedFishingWindow(
            window=TimeWindow(
                datetime.fromtimestamp(left, tz=timezone),
                datetime.fromtimestamp(right, tz=timezone),
            ),
            evidence=ConditionEvidence(
                time_restricted=fish.start_hour is not None,
                current_weather_id=current_weather_id,
                previous_weather_id=previous_weather_id,
            ),
        )
        if (
            matches
            and matches[-1].window.end == matched.window.start
            and matches[-1].evidence == matched.evidence
        ):
            previous = matches[-1]
            matches[-1] = MatchedFishingWindow(
                TimeWindow(previous.window.start, matched.window.end),
                previous.evidence,
            )
        else:
            matches.append(matched)
    # Drop windows that already ended at/before the query start (their true
    # start lies before the query range; only in-progress windows may extend
    # backward so the reported start is the real window start).
    return tuple(item for item in matches if item.window.end > start)


def earliest_greedy_trigger(
    windows: Sequence[tuple[float, float]], *, search_start: float
) -> float | None:
    """Return when the last independent requirement can first be completed."""
    completions: list[float] = []
    for start, end in windows:
        if start >= end:
            raise FishingWindowError("predecessor window start must be before end")
        if end <= search_start:
            return None
        completions.append(max(start, search_start))
    return max(completions, default=search_start)


def _next_requirement_window(
    windows: Sequence[TimeWindow], cursor: datetime
) -> tuple[datetime, TimeWindow] | None:
    for window in sorted(windows):
        if window.end > cursor:
            return max(cursor, window.start), window
    return None


def _first_intersection(
    windows: Sequence[TimeWindow], opportunity: TimeWindow
) -> TimeWindow | None:
    for window in sorted(windows):
        match = window.intersect(opportunity)
        if match is not None:
            return match
    return None


def build_intuition_rows(
    target: Fish,
    *,
    predecessor_windows: Mapping[int, Sequence[TimeWindow]],
    target_windows: Sequence[TimeWindow],
    start: datetime,
    end: datetime,
) -> tuple[IntuitionRow, ...]:
    """Build FishCake-style trigger rows without intersecting predecessor windows."""
    _require_aware(start)
    _require_aware(end)
    if start >= end:
        return ()
    if not target.predators or not target.intuition_length:
        raise FishingWindowError("intuition rows require predecessors and a duration")
    query = TimeWindow(start, end)
    cursor = start
    rows: list[IntuitionRow] = []
    while cursor < end:
        selected: list[tuple[datetime, TimeWindow]] = []
        for predecessor in target.predators:
            candidate = _next_requirement_window(
                predecessor_windows.get(predecessor.fish_id, ()), cursor
            )
            if candidate is None:
                selected = []
                break
            selected.append(candidate)
        if not selected:
            break
        trigger = max(completion for completion, _window in selected)
        final_windows = [
            window for completion, window in selected if completion == trigger
        ]
        opportunity_end = min(window.end for window in final_windows)
        if trigger >= opportunity_end:
            cursor = opportunity_end
            continue
        base_opportunity = TimeWindow(trigger, opportunity_end).intersect(query)
        if base_opportunity is None:
            break
        display_window = _first_intersection(target_windows, base_opportunity)
        if display_window is None:
            cursor = opportunity_end
            continue
        timezone = display_window.start.tzinfo
        assert timezone is not None
        first_buff_limit = TimeWindow(
            trigger,
            datetime.fromtimestamp(
                min(trigger.timestamp() + target.intuition_length, end.timestamp()),
                tz=timezone,
            ),
        )
        buff_window = _first_intersection(target_windows, first_buff_limit)
        if buff_window is None:
            cursor = opportunity_end
            continue
        rows.append(
            IntuitionRow(
                window=display_window,
                buff_window=buff_window,
                predators=target.predators,
                intuition_seconds=target.intuition_length,
                dormancy_seconds=None,
            )
        )
        cursor = display_window.end

    with_dormancy = tuple(
        dataclass_replace(
            row,
            dormancy_seconds=(
                int((rows[index + 1].window.start - row.window.end).total_seconds())
                if index + 1 < len(rows)
                else None
            ),
        )
        for index, row in enumerate(rows)
    )
    merged: list[IntuitionRow] = []
    for row in with_dormancy:
        if merged and merged[-1].window.end == row.window.start:
            previous = merged[-1]
            merged[-1] = dataclass_replace(
                previous,
                window=TimeWindow(previous.window.start, row.window.end),
                dormancy_seconds=row.dormancy_seconds,
            )
        else:
            merged.append(row)
    return tuple(merged)


class _SnapshotManager(Protocol):
    async def refresh(self, *, force: bool = False) -> FishingSnapshotStatus: ...


class _DetailSource(Protocol):
    async def guide_for(self, fish_id: int) -> tuple[str, FishCakeGuide | None]: ...


@dataclass(frozen=True, slots=True)
class _ServiceData:
    repository: FishingRepository
    weather_names: Mapping[int, str]
    weather_rates: Mapping[int, WeatherRate]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FishingWindowError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise FishingWindowError(f"{label} keys must be strings")
    return cast(Mapping[str, object], raw)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise FishingWindowError(f"{label} must be an array")
    return cast(list[object], value)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FishingWindowError(f"{label} must be an integer")
    return value


def _load_service_data(path: Path, revision: str) -> _ServiceData:
    repository = FishingRepository.from_json(path, source_revision=revision)
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FishingWindowError("normalized fishing snapshot is unreadable") from exc
    root = _mapping(parsed, "snapshot")
    weather_names: dict[int, str] = {}
    for value in _array(root.get("weather"), "snapshot.weather"):
        item = _mapping(value, "weather")
        weather_id = _integer(item.get("weatherId"), "weather.weatherId")
        name = item.get("nameZh")
        if not isinstance(name, str):
            raise FishingWindowError("weather.nameZh must be text")
        weather_names[weather_id] = name
    rates: dict[int, WeatherRate] = {}
    for value in _array(root.get("weatherRates"), "snapshot.weatherRates"):
        item = _mapping(value, "weather rate")
        rate_id = _integer(item.get("weatherRateId"), "weatherRate.weatherRateId")
        weather_ids = tuple(
            _integer(entry, "weatherRate.weatherIds")
            for entry in _array(item.get("weatherIds"), "weatherRate.weatherIds")
        )
        weights = tuple(
            _integer(entry, "weatherRate.rates")
            for entry in _array(item.get("rates"), "weatherRate.rates")
        )
        rates[rate_id] = WeatherRate(rate_id, weather_ids, weights)
    return _ServiceData(repository, weather_names, rates)


def _parse_start(value: object, *, zone: ZoneInfo, clock: Callable[[], datetime]) -> datetime:
    if value is None:
        result = clock()
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FishingWindowError("start_at must be an ISO-8601 datetime") from exc
    else:
        raise FishingWindowError("start_at must be text")
    _require_aware(result)
    return result.astimezone(zone)


def _duration(value: object, *, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FishingWindowError("duration_minutes must be a positive integer")
    return value


def _fish_rate(data: _ServiceData, fish: Fish) -> WeatherRate | None:
    spots = {spot.spot_id: spot for spot in data.repository.spots}
    territories = {
        territory.territory_id: territory for territory in data.repository.territories
    }
    spot = spots.get(fish.spot_id)
    territory = territories.get(spot.territory_type_id) if spot else None
    return data.weather_rates.get(territory.weather_rate_id) if territory else None


def _window_rows(
    data: _ServiceData, fish: Fish, start: datetime, end: datetime
) -> tuple[TimeWindow, ...]:
    target_matches = fish_windows(
        fish, start=start, end=end, weather_rate=_fish_rate(data, fish)
    )
    target_windows = tuple(item.window for item in target_matches)
    if not fish.predators or not fish.intuition_length:
        return target_windows
    predecessor_windows: dict[int, tuple[TimeWindow, ...]] = {}
    for predecessor in fish.predators:
        resolution = data.repository.resolve_fish(predecessor.fish_id)
        if resolution.fish is None:
            raise FishingWindowError(
                f"missing predecessor fish {predecessor.fish_id}"
            )
        matches = fish_windows(
            resolution.fish,
            start=start,
            end=end,
            weather_rate=_fish_rate(data, resolution.fish),
        )
        predecessor_windows[predecessor.fish_id] = tuple(
            item.window for item in matches
        )
    return tuple(
        row.window
        for row in build_intuition_rows(
            fish,
            predecessor_windows=predecessor_windows,
            target_windows=target_windows,
            start=start,
            end=end,
        )
    )


def _window_payload(
    window: TimeWindow, *, range_start: datetime | None = None
) -> dict[str, object]:
    return {
        "start": window.start,
        "end": window.end,
        "durationSeconds": int((window.end - window.start).total_seconds()),
        "inProgress": range_start is not None and window.start < range_start,
    }


def _display_coordinate(value: int) -> int:
    """Convert FishCake's standard 2048px map coordinate to its visible label."""
    return round(41 * value / 2048 + 1)


def _display_hookset(value: str) -> str:
    return {
        "powerful": "强力提钩",
        "precision": "精准提钩",
        "none": "不需要",
    }.get(value, value)


def _display_tug(value: str) -> str:
    return {"light": "!", "medium": "!!", "heavy": "!!!"}.get(value, value)


class FishingService:
    """Read FishCake's current detail data and answer local bulk queries."""

    def __init__(
        self,
        *,
        snapshot_manager: _SnapshotManager,
        detail_source: _DetailSource,
        timezone_name: str,
        clock: Callable[[], datetime],
    ) -> None:
        try:
            self._zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {timezone_name!r}") from exc
        self._snapshot_manager = snapshot_manager
        self._detail_source = detail_source
        self._timezone_name = timezone_name
        self._clock = clock

    async def execute(self, **kwargs: Any) -> object:
        action = kwargs.get("action")
        if action not in {"fish_info", "fish_windows", "weather"}:
            return error_result("invalid_action", "unknown fishing action")
        if action == "fish_info" and not str(kwargs.get("fish_name") or "").strip():
            return error_result(
                "missing_fish",
                "请提供 fish_name（准确中文鱼名）。",
                suggestions=("波太郎",),
            )
        if (
            action == "fish_windows"
            and kwargs.get("include_reminder") is True
            and not str(kwargs.get("fish_name") or "").strip()
        ):
            return error_result(
                "missing_fish",
                "创建鱼窗提醒时请提供 fish_name（准确中文鱼名）。",
                suggestions=("波太郎",),
            )
        if action == "weather" and not str(kwargs.get("zone") or "").strip():
            return error_result(
                "missing_zone",
                "请提供 zone（官方地区名或已审核简称）。",
                suggestions=("森都", "海都", "沙都", "伊修加德"),
            )
        try:
            status = await self._snapshot_manager.refresh(force=action == "fish_info")
        except FishingSnapshotUnavailableError as exc:
            return error_result("source_unavailable", str(exc))
        except FishCakeFormatError as exc:
            return error_result("source_format_changed", f"鱼糕数据格式已变化，请更新应用后重试：{exc}")
        if action == "fish_info" and status.state != "fresh":
            return error_result(
                "current_source_unavailable",
                "FishCake current detail is unavailable; stale timetable was not returned.",
            )
        try:
            data = _load_service_data(status.data_path, status.source_revision)
            start = _parse_start(kwargs.get("start_at"), zone=self._zone, clock=self._clock)
            if action == "fish_info":
                return await self._fish_info(data, status, start, kwargs)
            if action == "fish_windows":
                return self._fish_windows(data, status, start, kwargs)
            return self._weather(data, status, start, kwargs)
        except (FishingWindowError, ValueError) as exc:
            return error_result("invalid_query", str(exc))

    def _freshness(self, status: FishingSnapshotStatus) -> Freshness:
        return Freshness(
            source="FishCake",
            source_updated_at=status.source_updated_at,
            cache_status=status.state,
            stale=status.state == "stale",
        )

    def _resolve_fish(
        self, data: _ServiceData, query: object
    ) -> Fish | object:
        if not isinstance(query, str) or not query.strip():
            return error_result(
                "missing_fish",
                "请提供 fish_name（准确中文鱼名）。",
                suggestions=("波太郎",),
            )
        resolution = data.repository.resolve_fish(query)
        suggestions = tuple(
            f"{item.name_zh} ({item.fish_id})"
            for item in sorted(
                (
                    fish
                    for fish in data.repository.fish
                    if normalize_name(query) in normalize_name(fish.name_zh)
                ),
                key=lambda fish: (len(normalize_name(fish.name_zh)), fish.fish_id),
            )[:10]
        )
        if resolution.status == "ambiguous":
            return error_result(
                "ambiguous_fish",
                f"fish name {query!r} is ambiguous",
                suggestions=tuple(str(item.fish_id) for item in resolution.candidates),
            )
        if resolution.fish is None:
            return error_result(
                "fish_not_found", f"fish {query!r} was not found", suggestions=suggestions
            )
        return resolution.fish

    async def _fish_info(
        self,
        data: _ServiceData,
        status: FishingSnapshotStatus,
        start: datetime,
        kwargs: Mapping[str, Any],
    ) -> object:
        resolved = self._resolve_fish(data, kwargs.get("fish_name"))
        if not isinstance(resolved, Fish):
            return resolved
        try:
            revision, guide = await self._detail_source.guide_for(resolved.fish_id)
        except (FetchError, FishCakeFormatError) as exc:
            return error_result("current_source_unavailable", str(exc))
        if revision != status.source_revision:
            return error_result(
                "current_source_unavailable",
                "FishCake detail and local snapshot revisions do not match.",
            )
        end = start + timedelta(hours=48)
        windows = _window_rows(data, resolved, start, end)
        spots = {spot.spot_id: spot for spot in data.repository.spots}
        territories = {
            item.territory_id: item for item in data.repository.territories
        }
        spot = spots.get(resolved.spot_id)
        territory = territories.get(spot.territory_type_id) if spot else None
        if spot is None or territory is None:
            return error_result("invalid_snapshot", "fish location is incomplete")
        predators: list[dict[str, object]] = []
        for predecessor in resolved.predators:
            predecessor_fish = data.repository.resolve_fish(predecessor.fish_id).fish
            predators.append(
                {
                    "fishId": predecessor.fish_id,
                    "name": predecessor_fish.name_zh if predecessor_fish else "",
                    "count": predecessor.count,
                }
            )
        window_status = "none"
        starts_in_minutes: int | None = None
        if windows:
            if windows[0].start <= start:
                window_status, starts_in_minutes = "active", 0
            else:
                window_status = "upcoming"
                starts_in_minutes = math.ceil((windows[0].start - start).total_seconds() / 60)
        facts: dict[str, object] = {
            "name": resolved.name_zh,
            "fishId": resolved.fish_id,
            "spot": {
                "name": spot.name_zh,
                "map": territory.place_name_zh,
                "coordinates": [
                    _display_coordinate(spot.x),
                    _display_coordinate(spot.y),
                ],
            },
            "predators": predators,
            "baits": list(resolved.bait_names),
            "tug": _display_tug(resolved.tug),
            "hookset": _display_hookset(resolved.hookset),
            "intuitionSeconds": resolved.intuition_length,
            "nextWindow": (
                _window_payload(windows[0], range_start=start) if windows else None
            ),
            "windowStatus": window_status,
            "startsInMinutes": starts_in_minutes,
            "windows": [
                _window_payload(window, range_start=start) for window in windows[:10]
            ],
        }
        payload: dict[str, object] = {
            "displayTimezone": self._timezone_name,
            "facts": facts,
            "guideSummary": (
                {
                    "claimType": "player_strategy",
                    "guideTitle": guide.title,
                    "author": guide.author,
                    "updatedAt": guide.updated_at,
                    "body": guide.body,
                }
                if guide is not None
                else None
            ),
        }
        if kwargs.get("include_reminder") is True:
            reminder = self._reminder(
                resolved, spot.name_zh, predators, windows, data.weather_names
            )
            if reminder is not None:
                payload["reminder"] = reminder
        evidence = [
            Evidence(
                source="FishCake",
                source_url=(
                    f"https://fish.ffmomola.com/#/wiki/fishing/spot/"
                    f"{spot.spot_id}/fish/{resolved.fish_id}"
                ),
                heading="基本信息与时间表",
            )
        ]
        if guide is not None:
            evidence.append(
                Evidence(
                    source="FishCake player guide",
                    heading=guide.title,
                    excerpt=f"{guide.author}，更新于 {guide.updated_at}",
                )
            )
        return success_result(
            kind="ffxiv_fish_info",
            data=payload,
            evidence=evidence,
            freshness=self._freshness(status),
            warnings=status.warnings,
        )

    def _reminder(
        self,
        fish: Fish,
        spot_name: str,
        predators: Sequence[Mapping[str, object]],
        windows: Sequence[TimeWindow],
        weather_names: Mapping[int, str],
    ) -> dict[str, object] | None:
        now = self._clock().astimezone(self._zone)
        lead = timedelta(minutes=5)
        window = next((item for item in windows if item.start - lead > now), None)
        if window is None:
            return None
        predecessor_text = "、".join(
            f"{item['name']}×{item['count']}" for item in predators
        ) or "无"
        weather_parts: list[str] = []
        if fish.previous_weather_ids:
            names = "、".join(
                weather_names.get(item, f"ID {item}") for item in fish.previous_weather_ids
            )
            weather_parts.append(f"前置天气{names}")
        if fish.weather_ids:
            names = "、".join(
                weather_names.get(item, f"ID {item}") for item in fish.weather_ids
            )
            weather_parts.append(f"当前天气{names}")
        weather_text = "；".join(weather_parts) or "无特定天气"
        duration = int((window.end - window.start).total_seconds())
        reminder_at = window.start - lead
        message = (
            f"5分钟后{fish.name_zh}进入窗口：地点{spot_name}，"
            f"前置鱼{predecessor_text}，天气条件{weather_text}，窗口持续{duration}秒。"
        )
        return {
            "windowStart": window.start,
            "windowEnd": window.end,
            "reminderAt": reminder_at,
            "reminderMessage": message,
            "cronArgs": {"action": "add", "at": reminder_at, "message": message},
            "deliveryReliability": "best_effort",
            "requiresBotOnline": True,
            "requiresLlm": True,
            "missedDeliveryPolicy": "no_catch_up",
        }

    def _fish_windows(
        self,
        data: _ServiceData,
        status: FishingSnapshotStatus,
        start: datetime,
        kwargs: Mapping[str, Any],
    ) -> object:
        duration_value = kwargs.get("duration_minutes")
        duration = _duration(duration_value, default=24 * 60)
        end = start + timedelta(minutes=duration)
        fish_name = kwargs.get("fish_name")
        if fish_name is not None:
            resolved = self._resolve_fish(data, fish_name)
            if not isinstance(resolved, Fish):
                return resolved
            selected = (resolved,)
        else:
            rarity = kwargs.get("rarity")
            region = kwargs.get("region")
            available_rarities = tuple(sorted({fish.rarity for fish in data.repository.fish}))
            if rarity is not None and (
                not isinstance(rarity, str) or rarity not in available_rarities
            ):
                return error_result(
                    "invalid_rarity",
                    "rarity 不在当前快照支持的分类中。",
                    suggestions=available_rarities,
                )
            if region is not None and (
                not isinstance(region, str)
                or data.repository.resolve_location(region, intent="region").status
                != "found"
            ):
                return error_result(
                    "invalid_region",
                    "region 必须是官方地区名或已审核简称。",
                    suggestions=("森都", "海都", "沙都", "伊修加德"),
                )
            selected = data.repository.filter_fish(
                rarity=rarity if isinstance(rarity, str) else None,
                region=region if isinstance(region, str) else None,
            )
        spots = {spot.spot_id: spot for spot in data.repository.spots}
        territories = {
            item.territory_id: item for item in data.repository.territories
        }
        rows: list[dict[str, object]] = []
        reminder: dict[str, object] | None = None
        for fish in selected:
            windows = _window_rows(data, fish, start, end)
            if fish_name is not None and duration_value is None:
                for search_days in (3, 7, 14, 30, 90, 365):
                    if len(windows) >= 5:
                        break
                    end = start + timedelta(days=search_days)
                    windows = _window_rows(data, fish, start, end)
                windows = windows[:5]
            if windows:
                spot = spots.get(fish.spot_id)
                territory = territories.get(spot.territory_type_id) if spot else None
                if spot is None or territory is None:
                    return error_result("invalid_snapshot", "fish location is incomplete")
                rows.append(
                    {
                        "name": fish.name_zh,
                        "fishId": fish.fish_id,
                        "rarity": fish.rarity,
                        "region": territory.region_name_zh,
                        "zone": territory.place_name_zh,
                        "windows": [
                            _window_payload(window, range_start=start)
                            for window in windows
                        ],
                    }
                )
                if fish_name is not None and kwargs.get("include_reminder") is True:
                    predators: list[dict[str, object]] = []
                    for predecessor in fish.predators:
                        predecessor_fish = data.repository.resolve_fish(
                            predecessor.fish_id
                        ).fish
                        predators.append(
                            {
                                "fishId": predecessor.fish_id,
                                "name": predecessor_fish.name_zh if predecessor_fish else "",
                                "count": predecessor.count,
                            }
                        )
                    reminder = self._reminder(
                        fish,
                        spot.name_zh,
                        predators,
                        windows,
                        data.weather_names,
                    )
        payload: dict[str, object] = {
            "displayTimezone": self._timezone_name,
            "rangeStart": start,
            "rangeEnd": end,
            "fish": rows,
        }
        if reminder is not None:
            payload["reminder"] = reminder
        return success_result(
            kind="ffxiv_fish_windows",
            data=payload,
            evidence=[Evidence(source="FishCake normalized current asset")],
            freshness=self._freshness(status),
            warnings=(
                status.warnings
                if rows
                else (*status.warnings, "所选区间内无可用窗口。")
            ),
        )

    def _weather(
        self,
        data: _ServiceData,
        status: FishingSnapshotStatus,
        start: datetime,
        kwargs: Mapping[str, Any],
    ) -> object:
        query = kwargs.get("zone")
        if not isinstance(query, str) or not query.strip():
            return error_result(
                "missing_zone",
                "请提供 zone（官方地区名或已审核简称）。",
                suggestions=("森都", "海都", "沙都", "伊修加德"),
            )
        resolution = data.repository.resolve_location(query, intent="weather")
        if resolution.status != "found":
            return error_result("zone_not_found", f"weather zone {query!r} was not found")
        duration = _duration(kwargs.get("duration_minutes"), default=24 * 60)
        end = start + timedelta(minutes=duration)
        territories = {
            territory.place_name_zh: territory
            for territory in data.repository.territories
        }
        zones: list[dict[str, object]] = []
        missing_rates: list[str] = []
        for target in resolution.targets:
            territory = territories.get(target)
            rate = (
                data.weather_rates.get(territory.weather_rate_id)
                if territory is not None
                else None
            )
            if rate is None:
                missing_rates.append(target)
                continue
            periods = [
                {
                    "start": period.period_start,
                    "end": period.period_end,
                    "previousWeather": data.weather_names.get(
                        period.previous_weather_id, ""
                    ),
                    "weather": data.weather_names.get(period.current_weather_id, ""),
                }
                for period in iter_weather_periods(start, end, rate)
            ]
            zones.append({"zone": target, "periods": periods})
        if missing_rates:
            return error_result(
                "weather_rate_unavailable",
                "weather rate data is unavailable for one or more resolved zones",
                suggestions=missing_rates,
            )
        return success_result(
            kind="ffxiv_weather",
            data={
                "displayTimezone": self._timezone_name,
                "rangeStart": start,
                "rangeEnd": end,
                "zones": zones,
            },
            evidence=[Evidence(source="FF14 weather formula + FishCake data")],
            freshness=self._freshness(status),
            warnings=status.warnings,
        )
