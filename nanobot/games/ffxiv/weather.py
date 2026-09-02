"""Pure Final Fantasy XIV weather calculations."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from .fishing_types import WeatherRate

WEATHER_PERIOD_SECONDS = 1400


class WeatherError(ValueError):
    """Weather input or normalized rate data is invalid."""


@dataclass(frozen=True, order=True, slots=True)
class WeatherPeriod:
    period_start: datetime
    period_end: datetime
    previous_weather_id: int
    current_weather_id: int


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WeatherError("weather datetimes must be timezone-aware")
    return value.timestamp()


def weather_target(unix_seconds: int) -> int:
    bell = unix_seconds // 175
    increment = (bell + 8 - (bell % 8)) % 24
    total_days = unix_seconds // 4200
    calc_base = total_days * 100 + increment
    step1 = ((calc_base << 11) ^ calc_base) & 0xFFFFFFFF
    step2 = ((step1 >> 8) ^ step1) & 0xFFFFFFFF
    return step2 % 100


def weather_for_target(rate: WeatherRate, target: int) -> int:
    if len(rate.weather_ids) != len(rate.rates) or sum(rate.rates) != 100:
        raise WeatherError("weather rates must total 100")
    if not 0 <= target < 100:
        raise WeatherError("weather target must be in the range [0, 100)")
    cumulative = 0
    for weather_id, weight in zip(rate.weather_ids, rate.rates):
        if weight < 0:
            raise WeatherError("weather rates must not be negative")
        cumulative += weight
        if target < cumulative:
            return weather_id
    raise WeatherError("weather rate table does not cover its target")


def weather_pair_at(moment: datetime, rate: WeatherRate) -> WeatherPeriod:
    timestamp = math.floor(_timestamp(moment))
    period_timestamp = timestamp - timestamp % WEATHER_PERIOD_SECONDS
    previous_timestamp = period_timestamp - WEATHER_PERIOD_SECONDS
    timezone = moment.tzinfo
    assert timezone is not None
    return WeatherPeriod(
        period_start=datetime.fromtimestamp(period_timestamp, tz=timezone),
        period_end=datetime.fromtimestamp(
            period_timestamp + WEATHER_PERIOD_SECONDS, tz=timezone
        ),
        previous_weather_id=weather_for_target(
            rate, weather_target(previous_timestamp)
        ),
        current_weather_id=weather_for_target(rate, weather_target(period_timestamp)),
    )


def iter_weather_periods(
    start: datetime, end: datetime, rate: WeatherRate
) -> Iterator[WeatherPeriod]:
    start_timestamp = math.floor(_timestamp(start))
    end_timestamp = _timestamp(end)
    if end_timestamp <= start_timestamp:
        return
    cursor = start_timestamp - start_timestamp % WEATHER_PERIOD_SECONDS
    timezone = start.tzinfo
    assert timezone is not None
    while cursor < end_timestamp:
        yield weather_pair_at(datetime.fromtimestamp(cursor, tz=timezone), rate)
        cursor += WEATHER_PERIOD_SECONDS
