"""Immutable normalized value objects for FishCake fishing data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Predator:
    fish_id: int
    count: int


@dataclass(frozen=True, slots=True)
class Fish:
    fish_parameter_id: int
    fish_id: int
    name_zh: str
    zone_id: int
    spot_id: int
    spot_ids: tuple[int, ...]
    start_hour: float | None
    end_hour: float | None
    weather_ids: tuple[int, ...]
    previous_weather_ids: tuple[int, ...]
    predators: tuple[Predator, ...]
    intuition_length: int | None
    rarity: str
    bait_names: tuple[str, ...]
    tug: str
    hookset: str


@dataclass(frozen=True, slots=True)
class FishingSpot:
    spot_id: int
    place_name_id: int
    name_zh: str
    territory_type_id: int
    fish_ids: tuple[int, ...]
    x: int
    y: int
    radius: float
    category: str


@dataclass(frozen=True, slots=True)
class Territory:
    territory_id: int
    region_place_name_id: int
    region_name_zh: str
    zone_place_name_id: int
    zone_name_zh: str
    place_name_id: int
    place_name_zh: str
    map_id: int
    weather_rate_id: int


@dataclass(frozen=True, slots=True)
class Weather:
    weather_id: int
    name_zh: str


@dataclass(frozen=True, slots=True)
class WeatherRate:
    weather_rate_id: int
    weather_ids: tuple[int, ...]
    rates: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CountMetrics:
    fish_count: int
    fish_with_predators: int
    predator_relation_count: int
    rarity_distribution: tuple[tuple[str, int], ...]
    required_field_completeness: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class DriftWarning:
    metric: str
    baseline: int
    current: int


@dataclass(frozen=True, slots=True)
class SentinelResult:
    fish_id: int
    expected: tuple[Predator, ...]
    actual: tuple[Predator, ...]
    matched: bool


@dataclass(frozen=True, slots=True)
class DriftReport:
    current: CountMetrics
    baseline: CountMetrics
    warnings: tuple[DriftWarning, ...]
    sentinels: tuple[SentinelResult, ...]


@dataclass(frozen=True, slots=True)
class FishingSnapshot:
    schema_version: int
    source_revision: str
    source_sha256: str
    fetched_at: datetime
    item_names: tuple[tuple[int, str], ...]
    place_names: tuple[tuple[int, str], ...]
    territories: tuple[Territory, ...]
    weather: tuple[Weather, ...]
    weather_rates: tuple[WeatherRate, ...]
    spots: tuple[FishingSpot, ...]
    fish: tuple[Fish, ...]
    drift_report: DriftReport
