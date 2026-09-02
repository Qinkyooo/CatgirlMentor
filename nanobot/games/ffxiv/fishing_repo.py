"""Read-only local indexes over a normalized FishCake snapshot."""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from .fishing_types import Fish, FishingSnapshot, FishingSpot, Predator, Territory

LocationIntent = Literal["weather", "region"]
ResolutionStatus = Literal["found", "ambiguous", "not_found"]

LOCATION_ALIASES: Mapping[str, Mapping[LocationIntent, tuple[str, ...]]] = {
    "森都": {
        "weather": ("格里达尼亚新街", "格里达尼亚旧街"),
        "region": ("黑衣森林",),
    },
    "海都": {
        "weather": ("利姆萨·罗敏萨上层甲板", "利姆萨·罗敏萨下层甲板"),
        "region": ("拉诺西亚",),
    },
    "沙都": {
        "weather": ("乌尔达哈现世回廊", "乌尔达哈来生回廊"),
        "region": ("萨纳兰",),
    },
    "伊修加德": {
        "weather": ("伊修加德基础层", "伊修加德砥柱层"),
        "region": ("库尔札斯",),
    },
}


class FishingRepositoryError(ValueError):
    """A normalized fishing snapshot cannot produce trustworthy indexes."""


@dataclass(frozen=True, slots=True)
class FishResolution:
    status: ResolutionStatus
    fish: Fish | None
    candidates: tuple[Fish, ...] = ()


@dataclass(frozen=True, slots=True)
class LocationResolution:
    status: ResolutionStatus
    targets: tuple[str, ...]
    candidates: tuple[str, ...] = ()


def normalize_name(value: str) -> str:
    """Normalize width and whitespace without fuzzy or edit-distance guesses."""
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


class FishingRepository:
    def __init__(
        self,
        *,
        source_revision: str,
        fish: Sequence[Fish],
        spots: Sequence[FishingSpot],
        territories: Sequence[Territory],
    ) -> None:
        self.source_revision = source_revision
        self.fish = tuple(sorted(fish, key=lambda item: item.fish_id))
        self.spots = tuple(sorted(spots, key=lambda item: item.spot_id))
        self.territories = tuple(
            sorted(territories, key=lambda item: item.territory_id)
        )
        self._by_id = {item.fish_id: item for item in self.fish}
        if len(self._by_id) != len(self.fish):
            raise FishingRepositoryError("duplicate fish ID")
        for item in self.fish:
            for predecessor in item.predators:
                if predecessor.fish_id not in self._by_id:
                    raise FishingRepositoryError(
                        f"fish {item.fish_id} has missing predecessor {predecessor.fish_id}"
                    )

        exact: dict[str, list[Fish]] = defaultdict(list)
        normalized: dict[str, list[Fish]] = defaultdict(list)
        for item in self.fish:
            exact[item.name_zh].append(item)
            normalized[normalize_name(item.name_zh)].append(item)
        self._exact_names = {
            key: tuple(sorted(values, key=lambda item: item.fish_id))
            for key, values in exact.items()
        }
        self._normalized_names = {
            key: tuple(sorted(values, key=lambda item: item.fish_id))
            for key, values in normalized.items()
        }

        spots_by_id = {item.spot_id: item for item in self.spots}
        territories_by_id = {item.territory_id: item for item in self.territories}
        fish_regions: dict[int, frozenset[str]] = {}
        for item in self.fish:
            names: set[str] = set()
            for spot_id in item.spot_ids:
                spot = spots_by_id.get(spot_id)
                territory = territories_by_id.get(
                    spot.territory_type_id if spot is not None else -1
                )
                if territory is not None and territory.region_name_zh:
                    names.add(territory.region_name_zh)
            fish_regions[item.fish_id] = frozenset(names)
        self._fish_regions = fish_regions
        self._official_locations = {
            "weather": tuple(
                sorted(
                    {
                        item.place_name_zh
                        for item in self.territories
                        if item.place_name_zh
                    }
                )
            ),
            "region": tuple(
                sorted(
                    {
                        item.region_name_zh
                        for item in self.territories
                        if item.region_name_zh
                    }
                )
            ),
        }

    @classmethod
    def from_snapshot(cls, snapshot: FishingSnapshot) -> FishingRepository:
        return cls(
            source_revision=snapshot.source_revision,
            fish=snapshot.fish,
            spots=snapshot.spots,
            territories=snapshot.territories,
        )

    @classmethod
    def from_json(cls, path: Path, *, source_revision: str) -> FishingRepository:
        return _repository_from_json(
            str(Path(path).expanduser().resolve(strict=False)), source_revision
        )

    def resolve_fish(self, query: str | int) -> FishResolution:
        if isinstance(query, int) and not isinstance(query, bool):
            fish = self._by_id.get(query)
            return FishResolution("found", fish) if fish else FishResolution("not_found", None)
        if not isinstance(query, str):
            return FishResolution("not_found", None)
        stripped = query.strip()
        exact = self._exact_names.get(stripped, ())
        candidates = exact or self._normalized_names.get(normalize_name(stripped), ())
        if len(candidates) == 1:
            return FishResolution("found", candidates[0])
        if candidates:
            return FishResolution("ambiguous", None, candidates)
        return FishResolution("not_found", None)

    def resolve_location(
        self, query: str, *, intent: LocationIntent
    ) -> LocationResolution:
        stripped = query.strip()
        alias = LOCATION_ALIASES.get(stripped)
        if alias is not None:
            return LocationResolution("found", alias[intent])
        normalized_query = normalize_name(stripped)
        candidates = tuple(
            name
            for name in self._official_locations[intent]
            if normalize_name(name) == normalized_query
        )
        if len(candidates) == 1:
            return LocationResolution("found", candidates)
        if candidates:
            return LocationResolution("ambiguous", (), candidates)
        return LocationResolution("not_found", ())

    def regions_for(self, fish_id: int) -> frozenset[str]:
        return self._fish_regions.get(fish_id, frozenset())

    def filter_fish(
        self, *, rarity: str | None = None, region: str | None = None
    ) -> tuple[Fish, ...]:
        regions: frozenset[str] | None = None
        if region is not None:
            resolution = self.resolve_location(region, intent="region")
            if resolution.status != "found":
                return ()
            regions = frozenset(resolution.targets)
        return tuple(
            item
            for item in self.fish
            if (rarity is None or item.rarity == rarity)
            and (regions is None or bool(self.regions_for(item.fish_id) & regions))
        )


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FishingRepositoryError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise FishingRepositoryError(f"{label} keys must be strings")
    return cast(Mapping[str, object], raw)


def _items(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise FishingRepositoryError(f"{label} must be an array")
    return cast(list[object], value)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FishingRepositoryError(f"{label} must be an integer")
    return value


def _text_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FishingRepositoryError(f"{label} must be text")
    return value


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    return tuple(_integer(item, label) for item in _items(value, label))


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(_text_value(item, label) for item in _items(value, label))


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise FishingRepositoryError(f"{label} must be a number or null")
    return float(value)


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _fish_from_json(value: object) -> Fish:
    data = _object(value, "fish")
    predators = tuple(
        Predator(
            fish_id=_integer(_object(item, "predator").get("fishId"), "predator.fishId"),
            count=_integer(_object(item, "predator").get("count"), "predator.count"),
        )
        for item in _items(data.get("predators"), "fish.predators")
    )
    return Fish(
        fish_parameter_id=_integer(data.get("fishParameterId"), "fish.fishParameterId"),
        fish_id=_integer(data.get("fishId"), "fish.fishId"),
        name_zh=_text_value(data.get("nameZh"), "fish.nameZh"),
        zone_id=_integer(data.get("zoneId"), "fish.zoneId"),
        spot_id=_integer(data.get("spotId"), "fish.spotId"),
        spot_ids=_integer_tuple(data.get("spotIds"), "fish.spotIds"),
        start_hour=_optional_number(data.get("startHour"), "fish.startHour"),
        end_hour=_optional_number(data.get("endHour"), "fish.endHour"),
        weather_ids=_integer_tuple(data.get("weatherIds"), "fish.weatherIds"),
        previous_weather_ids=_integer_tuple(
            data.get("previousWeatherIds"), "fish.previousWeatherIds"
        ),
        predators=predators,
        intuition_length=_optional_integer(
            data.get("intuitionLength"), "fish.intuitionLength"
        ),
        rarity=_text_value(data.get("rarity"), "fish.rarity"),
        bait_names=_text_tuple(data.get("baitNames"), "fish.baitNames"),
        tug=_text_value(data.get("tug"), "fish.tug"),
        hookset=_text_value(data.get("hookset"), "fish.hookset"),
    )


def _spot_from_json(value: object) -> FishingSpot:
    data = _object(value, "fishing spot")
    radius = data.get("radius")
    if not isinstance(radius, int | float) or isinstance(radius, bool):
        raise FishingRepositoryError("fishing spot radius must be a number")
    return FishingSpot(
        spot_id=_integer(data.get("spotId"), "spot.spotId"),
        place_name_id=_integer(data.get("placeNameId"), "spot.placeNameId"),
        name_zh=_text_value(data.get("nameZh"), "spot.nameZh"),
        territory_type_id=_integer(
            data.get("territoryTypeId"), "spot.territoryTypeId"
        ),
        fish_ids=_integer_tuple(data.get("fishIds"), "spot.fishIds"),
        x=_integer(data.get("x"), "spot.x"),
        y=_integer(data.get("y"), "spot.y"),
        radius=float(radius),
        category=_text_value(data.get("category"), "spot.category"),
    )


def _territory_from_json(value: object) -> Territory:
    data = _object(value, "territory")
    return Territory(
        territory_id=_integer(data.get("territoryId"), "territory.territoryId"),
        region_place_name_id=_integer(
            data.get("regionPlaceNameId"), "territory.regionPlaceNameId"
        ),
        region_name_zh=_text_value(data.get("regionNameZh"), "territory.regionNameZh"),
        zone_place_name_id=_integer(
            data.get("zonePlaceNameId"), "territory.zonePlaceNameId"
        ),
        zone_name_zh=_text_value(data.get("zoneNameZh"), "territory.zoneNameZh"),
        place_name_id=_integer(data.get("placeNameId"), "territory.placeNameId"),
        place_name_zh=_text_value(data.get("placeNameZh"), "territory.placeNameZh"),
        map_id=_integer(data.get("mapId"), "territory.mapId"),
        weather_rate_id=_integer(
            data.get("weatherRateId"), "territory.weatherRateId"
        ),
    )


@lru_cache(maxsize=8)
def _repository_from_json(path: str, source_revision: str) -> FishingRepository:
    try:
        parsed: object = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FishingRepositoryError("fishing snapshot JSON is unreadable") from exc
    data = _object(parsed, "fishing snapshot")
    actual_revision = data.get("sourceRevision")
    if actual_revision != source_revision:
        raise FishingRepositoryError(
            f"snapshot revision mismatch: expected {source_revision}, got {actual_revision}"
        )
    return FishingRepository(
        source_revision=source_revision,
        fish=tuple(_fish_from_json(item) for item in _items(data.get("fish"), "fish")),
        spots=tuple(
            _spot_from_json(item) for item in _items(data.get("spots"), "spots")
        ),
        territories=tuple(
            _territory_from_json(item)
            for item in _items(data.get("territories"), "territories")
        ),
    )
