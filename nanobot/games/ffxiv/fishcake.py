"""Discover and decode FishCake's reviewed, versioned data assets."""

from __future__ import annotations

import json
import math
import re
import struct
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit

from nanobot.games.ffxiv.http import FetchResponse
from nanobot.games.ffxiv.result import to_jsonable

from .fishing_types import (
    CountMetrics,
    DriftReport,
    DriftWarning,
    Fish,
    FishingSnapshot,
    FishingSpot,
    Predator,
    SentinelResult,
    Territory,
    Weather,
    WeatherRate,
)

FISHCAKE_HOME_URL = "https://fish.ffmomola.com/"
FISHCAKE_HOSTS = frozenset({"fish.ffmomola.com"})
MAX_LENGTH_DELIMITED = 8 * 1024 * 1024
MAX_NESTING_DEPTH = 16

# Observed in normalFish-DWaOXxDd.bin. Its reviewed SHA-256 is
# cf475b305f27f7fa44f1dfcb37406683fd8c1200c6b381057c7721812e706a54.
FISH_FIELDS = {
    "fish_parameter_id": 1,
    "item_id": 2,
    "version": 3,
    "type": 4,
    "catch_type": 5,
    "tier_type": 6,
    "previous_weather_ids": 7,
    "weather_ids": 8,
    "start_hour": 9,
    "end_hour": 10,
    "start_hour_text": 11,
    "end_hour_text": 12,
    "tug": 13,
    "hookset": 14,
    "intuition_length": 15,
    "predators": 16,
}

_RARITIES = frozenset(
    {
        "normal",
        "bigFish",
        "livingLegend",
        "ikdNormalFish",
        "ikdBigFish",
        "ikdSpectralFish",
        "ikdBlueFish",
        "ikdGreenFish",
    }
)


class FishCakeFormatError(ValueError):
    """FishCake content no longer matches the reviewed data contract."""


@dataclass(frozen=True, slots=True)
class FishCakeGuide:
    """One player-authored strategy shown by FishCake."""

    title: str
    author: str
    updated_at: str
    body: str


_ACTION_WORDS = (
    "提钩",
    "钓上",
    "钓起",
    "拍水",
    "撒饵",
    "抛竿",
    "换饵",
    "切换",
    "雄心",
    "谦逊",
    "以小钓大",
)
_HTML_TAG = re.compile(r"<[^>]*>")


def _guide_date(value: str) -> date | None:
    try:
        year, month, day = (int(part) for part in value.replace("/", "-").split("-"))
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _valid_guide(guide: FishCakeGuide) -> bool:
    body = guide.body
    return (
        "undefined" not in body
        and ("钓饵" in body or "鱼饵" in body)
        and any(word in body for word in _ACTION_WORDS)
        and _guide_date(guide.updated_at) is not None
    )


def select_primary_guide(
    guides: Sequence[FishCakeGuide],
) -> FishCakeGuide | None:
    """Choose the newest complete guide, retaining page order for date ties."""
    valid = tuple(guide for guide in guides if _valid_guide(guide))
    return max(
        valid,
        key=lambda guide: _guide_date(guide.updated_at) or date.min,
        default=None,
    )


def _normalize_guide_date(value: str) -> str:
    parsed = _guide_date(value)
    if parsed is None:
        raise FishCakeFormatError("FishCake guide update date is invalid")
    return parsed.isoformat()


def _read_single_quoted_argument(source: str, start: int) -> str:
    result: list[str] = []
    cursor = start
    escapes = {"n": "\n", "r": "\r", "t": "\t", "'": "'", '"': '"', "\\": "\\"}
    while cursor < len(source):
        char = source[cursor]
        if char == "'":
            return "".join(result)
        if char != "\\":
            result.append(char)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(source):
            break
        escaped = source[cursor]
        if escaped == "u" and cursor + 4 < len(source):
            digits = source[cursor + 1 : cursor + 5]
            try:
                result.append(chr(int(digits, 16)))
            except ValueError as exc:
                raise FishCakeFormatError("invalid unicode escape in FishCake guide") from exc
            cursor += 5
            continue
        if escaped == "x" and cursor + 2 < len(source):
            digits = source[cursor + 1 : cursor + 3]
            try:
                result.append(chr(int(digits, 16)))
            except ValueError as exc:
                raise FishCakeFormatError("invalid byte escape in FishCake guide") from exc
            cursor += 3
            continue
        result.append(escapes.get(escaped, escaped))
        cursor += 1
    raise FishCakeFormatError("unterminated FishCake guide JSON string")


def _plain_guide_text(value: str) -> str:
    return " ".join(unescape(_HTML_TAG.sub(" ", value)).split())


def parse_primary_guide_asset(
    javascript: str | bytes, *, fish_id: int
) -> FishCakeGuide | None:
    """Parse the current primary guide's inert JSON literal without running JS."""
    source = _text(javascript)
    header = re.search(
        r'title:"(?P<title>[^"]+)".{0,500}?lastUpdate:"(?P<updated>[^"]+)"',
        source,
        flags=re.DOTALL,
    )
    marker = "JSON.parse('"
    start = source.find(marker)
    if header is None or start < 0:
        raise FishCakeFormatError("FishCake primary guide structure is missing")
    raw_json = _read_single_quoted_argument(source, start + len(marker))
    try:
        parsed: object = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise FishCakeFormatError("FishCake primary guide JSON is invalid") from exc
    if not isinstance(parsed, list):
        raise FishCakeFormatError("FishCake primary guide payload is not an array")
    for value in cast(list[object], parsed):
        if not isinstance(value, Mapping):
            raise FishCakeFormatError("FishCake primary guide entry is not an object")
        entry = cast(Mapping[object, object], value)
        if entry.get("itemId") != fish_id:
            continue
        author = entry.get("author")
        path = entry.get("bestCatchPath")
        tip = entry.get("tip")
        if not all(isinstance(item, str) for item in (author, path, tip)):
            raise FishCakeFormatError("FishCake primary guide entry is incomplete")
        body = _plain_guide_text(f"钓饵：{path}。{tip}")
        guide = FishCakeGuide(
            title=header.group("title"),
            author=cast(str, author),
            updated_at=_normalize_guide_date(header.group("updated")),
            body=body,
        )
        return guide if _valid_guide(guide) else None
    return None


@dataclass(frozen=True, slots=True)
class WireField:
    number: int
    wire_type: int
    value: int | bytes


def read_varint(data: bytes, cursor: int, end: int) -> tuple[int, int]:
    if cursor < 0 or end < cursor or end > len(data):
        raise FishCakeFormatError("invalid protobuf boundary")
    value = 0
    for index in range(10):
        if cursor >= end:
            raise FishCakeFormatError("truncated protobuf varint")
        byte = data[cursor]
        cursor += 1
        if index == 9 and byte > 1:
            raise FishCakeFormatError("protobuf varint exceeds uint64")
        value |= (byte & 0x7F) << (index * 7)
        if byte < 0x80:
            return value, cursor
    raise FishCakeFormatError("protobuf varint exceeds 10 bytes")


def read_message(data: bytes, *, depth: int = 0) -> tuple[WireField, ...]:
    if depth < 0 or depth > MAX_NESTING_DEPTH:
        raise FishCakeFormatError("protobuf nesting exceeds depth 16")
    cursor = 0
    end = len(data)
    values: list[WireField] = []
    while cursor < end:
        key, cursor = read_varint(data, cursor, end)
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number == 0:
            raise FishCakeFormatError("protobuf field number 0")
        if wire_type == 0:
            value, cursor = read_varint(data, cursor, end)
        elif wire_type == 1:
            next_cursor = cursor + 8
            if next_cursor > end:
                raise FishCakeFormatError("truncated protobuf fixed64")
            value = data[cursor:next_cursor]
            cursor = next_cursor
        elif wire_type == 2:
            length, cursor = read_varint(data, cursor, end)
            if length > MAX_LENGTH_DELIMITED:
                raise FishCakeFormatError("protobuf length-delimited field exceeds 8 MiB")
            next_cursor = cursor + length
            if next_cursor > end:
                raise FishCakeFormatError("truncated protobuf length-delimited field")
            value = data[cursor:next_cursor]
            cursor = next_cursor
        elif wire_type == 5:
            next_cursor = cursor + 4
            if next_cursor > end:
                raise FishCakeFormatError("truncated protobuf fixed32")
            value = data[cursor:next_cursor]
            cursor = next_cursor
        else:
            raise FishCakeFormatError(f"unsupported protobuf wire type {wire_type}")
        values.append(WireField(field_number, wire_type, value))
    return tuple(values)


def _records(data: bytes, label: str) -> tuple[tuple[WireField, ...], ...]:
    result: list[tuple[WireField, ...]] = []
    for field in read_message(data):
        if field.number != 1 or field.wire_type != 2 or not isinstance(field.value, bytes):
            raise FishCakeFormatError(f"invalid {label} record envelope")
        result.append(read_message(field.value, depth=1))
    return tuple(result)


def _matching(fields: Sequence[WireField], number: int) -> tuple[WireField, ...]:
    return tuple(field for field in fields if field.number == number)


def _one(fields: Sequence[WireField], number: int) -> WireField | None:
    matches = _matching(fields, number)
    if len(matches) > 1:
        raise FishCakeFormatError(f"duplicate protobuf field {number}")
    return matches[0] if matches else None


def _optional_varint(fields: Sequence[WireField], number: int) -> int | None:
    field = _one(fields, number)
    if field is None:
        return None
    if field.wire_type != 0 or not isinstance(field.value, int):
        raise FishCakeFormatError(f"protobuf field {number} is not a varint")
    return field.value


def _required_varint(fields: Sequence[WireField], number: int, label: str) -> int:
    value = _optional_varint(fields, number)
    if value is None:
        raise FishCakeFormatError(f"missing required {label}")
    return value


def _optional_text(fields: Sequence[WireField], number: int) -> str | None:
    field = _one(fields, number)
    if field is None:
        return None
    if field.wire_type != 2 or not isinstance(field.value, bytes):
        raise FishCakeFormatError(f"protobuf field {number} is not text")
    try:
        return field.value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FishCakeFormatError(f"protobuf field {number} is not UTF-8") from exc


def _required_text(fields: Sequence[WireField], number: int, label: str) -> str:
    value = _optional_text(fields, number)
    if not value:
        raise FishCakeFormatError(f"missing required {label}")
    return value


def _optional_fixed32_float(fields: Sequence[WireField], number: int) -> float | None:
    field = _one(fields, number)
    if field is None:
        return None
    if field.wire_type != 5 or not isinstance(field.value, bytes) or len(field.value) != 4:
        raise FishCakeFormatError(f"protobuf field {number} is not fixed32")
    return struct.unpack("<f", field.value)[0]


def _packed_varints(fields: Sequence[WireField], number: int) -> tuple[int, ...]:
    field = _one(fields, number)
    if field is None:
        return ()
    if field.wire_type != 2 or not isinstance(field.value, bytes):
        raise FishCakeFormatError(f"protobuf field {number} is not packed varints")
    cursor = 0
    result: list[int] = []
    while cursor < len(field.value):
        value, cursor = read_varint(field.value, cursor, len(field.value))
        result.append(value)
    return tuple(result)


def _decode_name_table(data: bytes, label: str) -> tuple[tuple[int, str], ...]:
    values: dict[int, str] = {}
    for fields in _records(data, label):
        if not fields:
            continue
        item_id = _required_varint(fields, 1, f"{label} ID")
        name = _required_text(fields, 2, f"{label} name")
        if item_id <= 0:
            raise FishCakeFormatError(f"invalid {label} ID {item_id}")
        if item_id in values:
            raise FishCakeFormatError(f"duplicate {label} ID {item_id}")
        values[item_id] = name
    return tuple(sorted(values.items()))


def _decode_js_string(value: str) -> str:
    result: list[str] = []
    cursor = 0
    escapes = {"\\": "\\", "'": "'", '"': '"', "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while cursor < len(value):
        character = value[cursor]
        cursor += 1
        if character != "\\":
            result.append(character)
            continue
        if cursor >= len(value):
            raise FishCakeFormatError("truncated JavaScript string escape")
        escape = value[cursor]
        cursor += 1
        if escape in escapes:
            result.append(escapes[escape])
            continue
        if escape != "u" or cursor + 4 > len(value):
            raise FishCakeFormatError(f"unsupported JavaScript string escape \\{escape}")
        digits = value[cursor : cursor + 4]
        try:
            codepoint = int(digits, 16)
        except ValueError as exc:
            raise FishCakeFormatError("invalid JavaScript Unicode escape") from exc
        cursor += 4
        if 0xD800 <= codepoint <= 0xDBFF:
            if value[cursor : cursor + 2] != "\\u" or cursor + 6 > len(value):
                raise FishCakeFormatError("unpaired JavaScript high surrogate")
            try:
                low = int(value[cursor + 2 : cursor + 6], 16)
            except ValueError as exc:
                raise FishCakeFormatError("invalid JavaScript Unicode escape") from exc
            if not 0xDC00 <= low <= 0xDFFF:
                raise FishCakeFormatError("unpaired JavaScript high surrogate")
            cursor += 6
            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
        elif 0xDC00 <= codepoint <= 0xDFFF:
            raise FishCakeFormatError("unpaired JavaScript low surrogate")
        result.append(chr(codepoint))
    return "".join(result)


def _extract_json_parse_value(data: bytes, variable: str) -> object:
    text = _text(data)
    marker = f",{variable}=JSON.parse("
    start = text.find(marker)
    if start < 0 or text.find(marker, start + len(marker)) >= 0:
        raise FishCakeFormatError(f"FishCake static table {variable} is missing or ambiguous")
    cursor = start + len(marker)
    if cursor >= len(text) or text[cursor] not in {"'", '"', "`"}:
        raise FishCakeFormatError(f"FishCake static table {variable} is not a string")
    quote = text[cursor]
    cursor += 1
    value_start = cursor
    escaped = False
    while cursor < len(text):
        character = text[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            break
        cursor += 1
    if cursor >= len(text) or text[cursor + 1 : cursor + 2] != ")":
        raise FishCakeFormatError(f"FishCake static table {variable} string is truncated")
    decoded = _decode_js_string(text[value_start:cursor])
    try:
        parsed: object = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise FishCakeFormatError(f"FishCake static table {variable} is invalid JSON") from exc
    return parsed


def _json_record(value: object, label: str) -> Mapping[str, object]:
    return _require_mapping(value, label)


def _find_static_table(data: bytes, fields: frozenset[str]) -> list[object]:
    # Minified variable names change between FishCake builds. Identify the
    # table by its schema, then let the normal row validators check all values.
    matches: list[list[object]] = []
    for variable in re.findall(r",([A-Za-z_$][\w$]*)=JSON\.parse\(", _text(data)):
        value = _extract_json_parse_value(data, variable)
        if not isinstance(value, list):
            continue
        rows = cast(list[object], value)
        if rows and isinstance(rows[0], dict) and fields.issubset(cast(dict[object, object], rows[0])):
            matches.append(rows)
    if len(matches) != 1:
        raise FishCakeFormatError(
            f"FishCake static table {sorted(fields)} is missing or ambiguous"
        )
    return matches[0]


def _json_int(record: Mapping[str, object], key: str, label: str) -> int:
    return _require_int(record.get(key), f"{label}.{key}")


def _json_signed_int(record: Mapping[str, object], key: str, label: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise FishCakeFormatError(f"{label}.{key} must be an integer")
    return value


def _json_int_list(record: Mapping[str, object], key: str, label: str) -> tuple[int, ...]:
    value = record.get(key)
    if not isinstance(value, list):
        raise FishCakeFormatError(f"{label}.{key} must be an array")
    return tuple(
        _require_int(item, f"{label}.{key}") for item in cast(list[object], value)
    )


def _decode_static_tables(
    data: bytes, place_names: Mapping[int, str]
) -> tuple[tuple[Territory, ...], tuple[Weather, ...], tuple[WeatherRate, ...]]:
    weather: list[Weather] = []
    weather_ids: set[int] = set()
    for index, raw in enumerate(_find_static_table(data, frozenset({"id", "chs", "iconId"}))):
        record = _json_record(raw, f"weather[{index}]")
        weather_id = _json_int(record, "id", f"weather[{index}]")
        name = record.get("chs")
        if weather_id <= 0 or weather_id in weather_ids or not isinstance(name, str):
            raise FishCakeFormatError(f"invalid weather record {weather_id}")
        weather.append(Weather(weather_id=weather_id, name_zh=name))
        weather_ids.add(weather_id)

    weather_rates: list[WeatherRate] = []
    rate_ids: set[int] = set()
    for index, raw in enumerate(_find_static_table(data, frozenset({"id", "weatherIds", "rates"}))):
        record = _json_record(raw, f"weatherRate[{index}]")
        rate_id = _json_int(record, "id", f"weatherRate[{index}]")
        ids = _json_int_list(record, "weatherIds", f"weatherRate[{index}]")
        rates = _json_int_list(record, "rates", f"weatherRate[{index}]")
        if rate_id <= 0 or rate_id in rate_ids or len(ids) != len(rates):
            raise FishCakeFormatError(f"invalid weather rate table {rate_id}")
        if any(rate > 0 and weather_id not in weather_ids for weather_id, rate in zip(ids, rates)):
            raise FishCakeFormatError(f"weather rate table {rate_id} has a broken weather reference")
        weather_rates.append(
            WeatherRate(weather_rate_id=rate_id, weather_ids=ids, rates=rates)
        )
        rate_ids.add(rate_id)

    territories: list[Territory] = []
    territory_ids: set[int] = set()
    for index, raw in enumerate(_find_static_table(data, frozenset({"id", "regionPlaceNameId", "zonePlaceNameId", "placeNameId", "weatherRate", "mapId"}))):
        record = _json_record(raw, f"territory[{index}]")
        territory_id = _json_int(record, "id", f"territory[{index}]")
        region_id = _json_int(record, "regionPlaceNameId", f"territory[{index}]")
        zone_id = _json_int(record, "zonePlaceNameId", f"territory[{index}]")
        place_id = _json_int(record, "placeNameId", f"territory[{index}]")
        weather_rate_id = _json_int(record, "weatherRate", f"territory[{index}]")
        if territory_id <= 0 or territory_id in territory_ids:
            raise FishCakeFormatError(f"invalid territory record {territory_id}")
        if weather_rate_id and weather_rate_id not in rate_ids:
            raise FishCakeFormatError(
                f"territory {territory_id} has a broken weather-rate reference"
            )
        territories.append(
            Territory(
                territory_id=territory_id,
                region_place_name_id=region_id,
                region_name_zh=place_names.get(region_id, ""),
                zone_place_name_id=zone_id,
                zone_name_zh=place_names.get(place_id, ""),
                place_name_id=place_id,
                place_name_zh=place_names.get(place_id, ""),
                map_id=_json_signed_int(record, "mapId", f"territory[{index}]"),
                weather_rate_id=weather_rate_id,
            )
        )
        territory_ids.add(territory_id)
    rates_by_id = {item.weather_rate_id: item for item in weather_rates}
    for rate_id in sorted({item.weather_rate_id for item in territories} - {0}):
        if sum(rates_by_id[rate_id].rates) != 100:
            raise FishCakeFormatError(
                f"referenced weather rate table {rate_id} must total 100"
            )
    return (
        tuple(sorted(territories, key=lambda item: item.territory_id)),
        tuple(sorted(weather, key=lambda item: item.weather_id)),
        tuple(sorted(weather_rates, key=lambda item: item.weather_rate_id)),
    )


def _decode_baits(data: bytes) -> dict[int, tuple[int, ...]]:
    result: dict[int, tuple[int, ...]] = {}
    for fields in _records(data, "fish bait"):
        fish_id = _required_varint(fields, 1, "bait fish ID")
        if fish_id in result:
            raise FishCakeFormatError(f"duplicate bait record for fish {fish_id}")
        result[fish_id] = _packed_varints(fields, 2)
    return result


def _decode_spots(
    data: bytes, place_names: Mapping[int, str]
) -> tuple[FishingSpot, ...]:
    spots: list[FishingSpot] = []
    seen: set[int] = set()
    for fields in _records(data, "fishing spot"):
        spot_id = _required_varint(fields, 1, "fishing spot ID")
        place_name_id = _required_varint(fields, 2, "fishing spot place name ID")
        territory_type_id = _optional_varint(fields, 3) or 0
        if spot_id <= 0 or spot_id in seen:
            raise FishCakeFormatError(f"invalid or duplicate fishing spot ID {spot_id}")
        if place_name_id < 0 or territory_type_id < 0:
            raise FishCakeFormatError(f"negative location ID for fishing spot {spot_id}")
        name = place_names.get(place_name_id)
        if not name:
            raise FishCakeFormatError(f"missing Chinese name for fishing spot {spot_id}")
        radius = _optional_fixed32_float(fields, 7)
        spots.append(
            FishingSpot(
                spot_id=spot_id,
                place_name_id=place_name_id,
                name_zh=name,
                territory_type_id=territory_type_id,
                fish_ids=_packed_varints(fields, 4),
                x=_optional_varint(fields, 5) or 0,
                y=_optional_varint(fields, 6) or 0,
                radius=radius or 0.0,
                category=_optional_text(fields, 11) or "",
            )
        )
        seen.add(spot_id)
    return tuple(sorted(spots, key=lambda item: item.spot_id))


def _decode_predators(fields: Sequence[WireField]) -> tuple[Predator, ...]:
    result: list[Predator] = []
    for field in _matching(fields, FISH_FIELDS["predators"]):
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            raise FishCakeFormatError("predator relation is not a nested message")
        relation = read_message(field.value, depth=2)
        fish_id = _required_varint(relation, 1, "predator fish ID")
        count = _required_varint(relation, 2, "predator count")
        if fish_id <= 0 or count <= 0:
            raise FishCakeFormatError("predator IDs and counts must be positive")
        result.append(Predator(fish_id=fish_id, count=count))
    return tuple(result)


def _hour(value: float, label: str) -> float:
    if not math.isfinite(value) or value < 0 or value > 24:
        raise FishCakeFormatError(f"invalid {label} {value}")
    return value


def _normalize_hours(fields: Sequence[WireField]) -> tuple[float | None, float | None]:
    start = _hour(
        _optional_fixed32_float(fields, FISH_FIELDS["start_hour"]) or 0.0,
        "start hour",
    )
    end_value = _optional_fixed32_float(fields, FISH_FIELDS["end_hour"])
    end = _hour(24.0 if end_value is None else end_value, "end hour")
    if start == 0 and end == 24:
        return None, None
    if start == 24:
        start = 0
    if end == 24:
        end = 0
    if not (0 <= start < 24 and 0 <= end < 24):
        raise FishCakeFormatError("fish hours must normalize to the range [0, 24)")
    return start, end


def _decode_fish(
    data: bytes,
    *,
    item_names: Mapping[int, str],
    bait_ids: Mapping[int, tuple[int, ...]],
    spots: Sequence[FishingSpot],
) -> tuple[tuple[Fish, ...], tuple[tuple[str, int], ...]]:
    fish_to_spots: dict[int, list[FishingSpot]] = defaultdict(list)
    for spot in spots:
        for fish_id in spot.fish_ids:
            fish_to_spots[fish_id].append(spot)

    fish: list[Fish] = []
    seen: set[int] = set()
    completeness: Counter[str] = Counter()
    for fields in _records(data, "normal fish"):
        fish_parameter_id = _required_varint(
            fields, FISH_FIELDS["fish_parameter_id"], "fishParameterId"
        )
        fish_id = _required_varint(fields, FISH_FIELDS["item_id"], "itemId")
        _required_text(fields, FISH_FIELDS["type"], "type")
        _required_text(fields, FISH_FIELDS["catch_type"], "catchType")
        rarity = _required_text(fields, FISH_FIELDS["tier_type"], "tierType")
        if fish_parameter_id <= 0 or fish_id <= 0 or fish_id in seen:
            raise FishCakeFormatError(f"invalid or duplicate fish ID {fish_id}")
        name = item_names.get(fish_id)
        if not name:
            raise FishCakeFormatError(f"missing Chinese name for fish {fish_id}")
        if rarity not in _RARITIES:
            raise FishCakeFormatError(f"unknown fish rarity {rarity!r}")
        completeness.update(
            (
                "fishParameterId",
                "itemId",
                "type",
                "catchType",
                "tierType",
                "chineseItemName",
            )
        )
        locations = tuple(sorted(fish_to_spots.get(fish_id, ()), key=lambda item: item.spot_id))
        primary = locations[0] if locations else None
        predators = _decode_predators(fields)
        intuition_length = _optional_varint(fields, FISH_FIELDS["intuition_length"])
        if predators and (intuition_length is None or intuition_length <= 0):
            raise FishCakeFormatError(f"intuition fish {fish_id} has no positive duration")
        weather_ids = _packed_varints(fields, FISH_FIELDS["weather_ids"])
        previous_weather_ids = _packed_varints(
            fields, FISH_FIELDS["previous_weather_ids"]
        )
        if any(value < 0 for value in (*weather_ids, *previous_weather_ids)):
            raise FishCakeFormatError(f"fish {fish_id} has a negative weather ID")
        direct_bait_ids = bait_ids.get(fish_id, ())
        bait_names: list[str] = []
        for bait_id in direct_bait_ids:
            bait_name = item_names.get(bait_id)
            if not bait_name:
                raise FishCakeFormatError(
                    f"missing Chinese name for bait {bait_id} used by fish {fish_id}"
                )
            bait_names.append(bait_name)
        start_hour, end_hour = _normalize_hours(fields)
        fish.append(
            Fish(
                fish_parameter_id=fish_parameter_id,
                fish_id=fish_id,
                name_zh=name,
                zone_id=primary.territory_type_id if primary else 0,
                spot_id=primary.spot_id if primary else 0,
                spot_ids=tuple(item.spot_id for item in locations),
                start_hour=start_hour,
                end_hour=end_hour,
                weather_ids=weather_ids,
                previous_weather_ids=previous_weather_ids,
                predators=predators,
                intuition_length=intuition_length,
                rarity=rarity,
                bait_names=tuple(bait_names),
                tug=_optional_text(fields, FISH_FIELDS["tug"]) or "",
                hookset=_optional_text(fields, FISH_FIELDS["hookset"]) or "",
            )
        )
        seen.add(fish_id)

    known_ids = frozenset(seen)
    for item in fish:
        for predator in item.predators:
            if predator.fish_id not in known_ids:
                raise FishCakeFormatError(
                    f"fish {item.fish_id} has broken predecessor reference {predator.fish_id}"
                )
    return tuple(sorted(fish, key=lambda item: item.fish_id)), tuple(sorted(completeness.items()))


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FishCakeFormatError(f"{label} must be an object")
    untyped = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in untyped):
        raise FishCakeFormatError(f"{label} keys must be strings")
    return cast(Mapping[str, object], untyped)


def _require_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FishCakeFormatError(f"{label} must be a non-negative integer")
    return value


def _named_counts(value: object, label: str) -> tuple[tuple[str, int], ...]:
    mapping = _require_mapping(value, label)
    return tuple(
        sorted((key, _require_int(item, f"{label}.{key}")) for key, item in mapping.items())
    )


def _baseline_metrics(baseline: Mapping[str, object]) -> CountMetrics:
    return CountMetrics(
        fish_count=_require_int(baseline.get("fishCount"), "baseline.fishCount"),
        fish_with_predators=_require_int(
            baseline.get("fishWithPredators"), "baseline.fishWithPredators"
        ),
        predator_relation_count=_require_int(
            baseline.get("predatorRelationCount"), "baseline.predatorRelationCount"
        ),
        rarity_distribution=_named_counts(
            baseline.get("rarityDistribution"), "baseline.rarityDistribution"
        ),
        required_field_completeness=_named_counts(
            baseline.get("requiredFieldCompleteness"),
            "baseline.requiredFieldCompleteness",
        ),
    )


def _metric_warnings(current: CountMetrics, baseline: CountMetrics) -> tuple[DriftWarning, ...]:
    values: list[tuple[str, int, int]] = [
        ("fishCount", baseline.fish_count, current.fish_count),
        (
            "fishWithPredators",
            baseline.fish_with_predators,
            current.fish_with_predators,
        ),
        (
            "predatorRelationCount",
            baseline.predator_relation_count,
            current.predator_relation_count,
        ),
    ]
    for prefix, baseline_values, current_values in (
        ("rarityDistribution", baseline.rarity_distribution, current.rarity_distribution),
        (
            "requiredFieldCompleteness",
            baseline.required_field_completeness,
            current.required_field_completeness,
        ),
    ):
        baseline_map = dict(baseline_values)
        current_map = dict(current_values)
        for key in sorted(baseline_map.keys() | current_map.keys()):
            values.append(
                (f"{prefix}.{key}", baseline_map.get(key, 0), current_map.get(key, 0))
            )
    return tuple(
        DriftWarning(metric=metric, baseline=expected, current=actual)
        for metric, expected, actual in values
        if expected != actual
    )


def _sentinel_results(
    fish: Sequence[Fish], baseline: Mapping[str, object]
) -> tuple[SentinelResult, ...]:
    raw_sentinels = _require_mapping(
        baseline.get("relationshipSentinels"), "baseline.relationshipSentinels"
    )
    by_id = {item.fish_id: item for item in fish}
    results: list[SentinelResult] = []
    for raw_fish_id, raw_relations in sorted(raw_sentinels.items(), key=lambda item: int(item[0])):
        try:
            fish_id = int(raw_fish_id)
        except ValueError as exc:
            raise FishCakeFormatError("sentinel fish ID must be numeric") from exc
        if not isinstance(raw_relations, list):
            raise FishCakeFormatError(f"sentinel {fish_id} must be a list")
        expected: list[Predator] = []
        for raw_relation in cast(list[object], raw_relations):
            relation = _require_mapping(raw_relation, f"sentinel {fish_id} relation")
            expected.append(
                Predator(
                    fish_id=_require_int(relation.get("fishId"), "sentinel predecessor ID"),
                    count=_require_int(relation.get("count"), "sentinel predecessor count"),
                )
            )
        actual = by_id.get(fish_id)
        actual_predators = actual.predators if actual else ()
        expected_predators = tuple(expected)
        results.append(
            SentinelResult(
                fish_id=fish_id,
                expected=expected_predators,
                actual=actual_predators,
                matched=actual_predators == expected_predators,
            )
        )
    return tuple(results)


def normalize_fishcake_assets(
    *,
    normal_fish: bytes,
    data_json: bytes,
    fishing_spot: bytes,
    fish_bait_and_mooch: bytes,
    item_name_chs: bytes,
    place_name_chs: bytes,
    source_revision: str,
    source_sha256: str,
    fetched_at: datetime,
    baseline: object,
) -> FishingSnapshot:
    """Decode one reviewed FishCake asset set without executing downloaded code."""
    if not source_revision or not source_sha256:
        raise FishCakeFormatError("FishCake snapshot metadata is incomplete")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise FishCakeFormatError("FishCake fetchedAt must include a timezone")
    baseline_mapping = _require_mapping(baseline, "baseline")
    item_names = _decode_name_table(item_name_chs, "item Chinese name")
    place_names = _decode_name_table(place_name_chs, "place Chinese name")
    territories, weather, weather_rates = _decode_static_tables(
        data_json, dict(place_names)
    )
    spots = _decode_spots(fishing_spot, dict(place_names))
    fish, completeness = _decode_fish(
        normal_fish,
        item_names=dict(item_names),
        bait_ids=_decode_baits(fish_bait_and_mooch),
        spots=spots,
    )
    current = CountMetrics(
        fish_count=len(fish),
        fish_with_predators=sum(bool(item.predators) for item in fish),
        predator_relation_count=sum(len(item.predators) for item in fish),
        rarity_distribution=tuple(sorted(Counter(item.rarity for item in fish).items())),
        required_field_completeness=completeness,
    )
    expected = _baseline_metrics(baseline_mapping)
    required = dict(current.required_field_completeness)
    incomplete = sorted(name for name, count in required.items() if count != current.fish_count)
    if incomplete:
        raise FishCakeFormatError(f"required fish fields are incomplete: {', '.join(incomplete)}")
    sentinels = _sentinel_results(fish, baseline_mapping)
    mismatches = [str(item.fish_id) for item in sentinels if not item.matched]
    if mismatches:
        raise FishCakeFormatError(
            f"relationship sentinel mismatch for fish {', '.join(mismatches)}"
        )
    return FishingSnapshot(
        schema_version=1,
        source_revision=source_revision,
        source_sha256=source_sha256,
        fetched_at=fetched_at,
        item_names=item_names,
        place_names=place_names,
        territories=territories,
        weather=weather,
        weather_rates=weather_rates,
        spots=spots,
        fish=fish,
        drift_report=DriftReport(
            current=current,
            baseline=expected,
            warnings=_metric_warnings(current, expected),
            sentinels=sentinels,
        ),
    )


def serialize_fishing_snapshot(snapshot: FishingSnapshot) -> str:
    """Serialize a normalized snapshot with numeric lookup-table ordering."""
    payload = {
        "schemaVersion": snapshot.schema_version,
        "sourceRevision": snapshot.source_revision,
        "sourceSha256": snapshot.source_sha256,
        "fetchedAt": snapshot.fetched_at.isoformat(),
        "itemNames": {str(item_id): name for item_id, name in snapshot.item_names},
        "placeNames": {str(item_id): name for item_id, name in snapshot.place_names},
        "territories": to_jsonable(snapshot.territories),
        "weather": to_jsonable(snapshot.weather),
        "weatherRates": to_jsonable(snapshot.weather_rates),
        "spots": to_jsonable(snapshot.spots),
        "fish": to_jsonable(snapshot.fish),
        "driftReport": to_jsonable(snapshot.drift_report),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class DiscoveredAsset:
    role: str
    url: str


@dataclass(frozen=True, slots=True)
class FishCakeDiscovery:
    source_revision: str
    home: FetchResponse
    script: FetchResponse
    assets: tuple[DiscoveredAsset, ...]


class _FetchClient(Protocol):
    async def get_bytes(
        self, url: str, *, allowed_hosts: frozenset[str]
    ) -> FetchResponse: ...


_HASHED_SCRIPT = re.compile(r"(?:^|/)[^/]+-[A-Za-z0-9_-]+\.js$")
_PRIMARY_GUIDE_ASSET = re.compile(r"(?:^|/)tips-[A-Za-z0-9_-]+\.js$")
_ASSET_PATTERNS = (
    (
        "data_json",
        re.compile(
            r"(?:^|/)data-json-(?!cosmic-|diadem-)[A-Za-z0-9_-]+\.js$"
        ),
    ),
    ("fishing_spot", re.compile(r"(?:^|/)fishingSpot-[A-Za-z0-9_-]+\.bin$")),
    (
        "fish_bait_and_mooch",
        re.compile(r"(?:^|/)fishBaitAndMooch-[A-Za-z0-9_-]+\.bin$"),
    ),
    ("normal_fish", re.compile(r"(?:^|/)normalFish-[A-Za-z0-9_-]+\.bin$")),
    ("item", re.compile(r"(?:^|/)item-[A-Za-z0-9_-]+\.bin$")),
    (
        "item_name_chs",
        re.compile(r"(?:^|/)itemNameCHS-[A-Za-z0-9_-]+\.bin$"),
    ),
    (
        "place_name_chs",
        re.compile(r"(?:^|/)placeNameCHS-[A-Za-z0-9_-]+\.bin$"),
    ),
)


def _quoted_values(source: str) -> tuple[str, ...]:
    """Read JS string tokens without treating the opposite quote as a terminator."""
    values: list[str] = []
    cursor = 0
    while cursor < len(source):
        quote = source[cursor]
        if quote not in {"'", '"'}:
            cursor += 1
            continue
        cursor += 1
        start = cursor
        escaped = False
        while cursor < len(source):
            character = source[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                values.append(source[start:cursor])
                cursor += 1
                break
            cursor += 1
    return tuple(values)


def _text(value: str | bytes) -> str:
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FishCakeFormatError("FishCake asset is not UTF-8") from exc


def _same_origin_url(reference: str, base_url: str) -> str:
    if "\\" in reference:
        raise FishCakeFormatError("FishCake asset URL contains a backslash")
    try:
        base = urlsplit(base_url)
        resolved = urljoin(base_url, reference)
        candidate = urlsplit(resolved)
        base_port = base.port
        candidate_port = candidate.port
    except ValueError as exc:
        raise FishCakeFormatError("FishCake asset URL is invalid") from exc
    if candidate.username is not None or candidate.password is not None:
        raise FishCakeFormatError("FishCake asset URL contains credentials")
    if (
        candidate.scheme != base.scheme
        or candidate.hostname != base.hostname
        or candidate_port != base_port
    ):
        raise FishCakeFormatError("FishCake asset URL is cross-origin")
    return resolved


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []
        self.source_revision: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() == "meta":
            values = dict(attrs)
            if values.get("name") == "version":
                self.source_revision = values.get("content")
            return
        if tag.casefold() != "script":
            return
        values = dict(attrs)
        source = values.get("src")
        if (values.get("type") or "").casefold() == "module" and source:
            self.sources.append(source)


def discover_script_urls(html: str | bytes, base_url: str) -> tuple[str, ...]:
    parser = _ScriptParser()
    parser.feed(_text(html))
    result: list[str] = []
    for source in parser.sources:
        url = _same_origin_url(source, base_url)
        if not _HASHED_SCRIPT.search(urlsplit(url).path):
            continue
        if url not in result:
            result.append(url)
    if not result:
        raise FishCakeFormatError("FishCake content-hashed application script is missing")
    return tuple(result)


def discover_data_assets(
    javascript: str | bytes, base_url: str
) -> tuple[DiscoveredAsset, ...]:
    found: dict[str, DiscoveredAsset] = {}
    order: list[str] = []
    for reference in _quoted_values(_text(javascript)):
        path = urlsplit(reference).path
        role = next(
            (name for name, pattern in _ASSET_PATTERNS if pattern.search(path)),
            None,
        )
        if role is None:
            continue
        rooted = f"/{reference}" if reference.startswith("assets/") else reference
        url = _same_origin_url(rooted, base_url)
        existing = found.get(role)
        if existing is not None and existing.url != url:
            raise FishCakeFormatError(f"multiple FishCake assets for {role}")
        if existing is None:
            found[role] = DiscoveredAsset(role=role, url=url)
            order.append(role)

    for role, _pattern in _ASSET_PATTERNS:
        if role not in found:
            raise FishCakeFormatError(f"missing FishCake asset for {role}")
    return tuple(found[role] for role in order)


def discover_primary_guide_url(javascript: str | bytes, base_url: str) -> str:
    matches: list[str] = []
    for reference in _quoted_values(_text(javascript)):
        if not _PRIMARY_GUIDE_ASSET.search(urlsplit(reference).path):
            continue
        rooted = f"/{reference}" if reference.startswith("assets/") else reference
        url = _same_origin_url(rooted, base_url)
        if url not in matches:
            matches.append(url)
    if len(matches) != 1:
        raise FishCakeFormatError("FishCake primary guide asset is missing or ambiguous")
    return matches[0]


@dataclass(frozen=True, slots=True)
class FishCakeDetailSource:
    client: _FetchClient

    async def _guide_url(self, discovery: FishCakeDiscovery, fish_id: int) -> str | None:
        try:
            return discover_primary_guide_url(discovery.script.body, discovery.script.url)
        except FishCakeFormatError:
            pass
        # New site builds split guides into lazy chunks. Follow the published
        # manifest's fish IDs and dates instead of guessing the newest filename.
        references = re.findall(
            r'["\']([^"\'\s]{1,200}FishDetailTips-[A-Za-z0-9_-]+\.js)["\']',
            _text(discovery.script.body),
        )
        urls = {
            _same_origin_url(f"/{ref}" if ref.startswith("assets/") else ref, discovery.script.url)
            for ref in references
        }
        if len(urls) != 1:
            raise FishCakeFormatError("FishCake guide manifest is missing or ambiguous")
        manifest_url = urls.pop()
        response = await self.client.get_bytes(manifest_url, allowed_hosts=FISHCAKE_HOSTS)
        entries = re.findall(
            r'\{\s*id\s*:\s*"[^"]+"\s*,\s*lastUpdate\s*:\s*"([^"]+)"\s*,'
            r'\s*fishItemIds\s*:\s*(\[[\deE+.,\s-]*\])\s*,'
            r'\s*load\s*:[^{}]{0,160}?import\(\s*"([^"]+)"\s*\)',
            _text(response.body),
        )
        expected_entries = re.findall(r'\{\s*id\s*:\s*"[^"]+"\s*,\s*lastUpdate\s*:', _text(response.body))
        if not entries or len(entries) != len(expected_entries):
            raise FishCakeFormatError("FishCake guide manifest format changed")
        candidates: list[tuple[date, str]] = []
        for updated, ids, reference in entries:
            try:
                fish_ids: object = json.loads(ids)
            except ValueError as exc:
                raise FishCakeFormatError("FishCake guide manifest fish IDs are invalid") from exc
            if not isinstance(fish_ids, list) or not all(
                isinstance(value, int | float) and not isinstance(value, bool)
                and 0 <= value <= 2**31 - 1 and value == int(value)
                for value in cast(list[object], fish_ids)
            ):
                raise FishCakeFormatError("FishCake guide manifest fish IDs are invalid")
            if fish_id not in cast(list[object], fish_ids):
                continue
            parsed_date = _guide_date(updated)
            if parsed_date is None:
                raise FishCakeFormatError("FishCake guide manifest date is invalid")
            candidates.append((parsed_date, _same_origin_url(reference, manifest_url)))
        if not candidates:
            return None
        newest = max(updated for updated, _ in candidates)
        selected = {url for updated, url in candidates if updated == newest}
        if len(selected) != 1:
            raise FishCakeFormatError("FishCake newest guide is ambiguous")
        return selected.pop()

    async def guide_for(self, fish_id: int) -> tuple[str, FishCakeGuide | None]:
        discovery = await discover_current_assets(self.client)
        url = await self._guide_url(discovery, fish_id)
        if url is None:
            return discovery.source_revision, None
        response = await self.client.get_bytes(url, allowed_hosts=FISHCAKE_HOSTS)
        return discovery.source_revision, parse_primary_guide_asset(
            response.body, fish_id=fish_id
        )


async def discover_current_assets(client: _FetchClient) -> FishCakeDiscovery:
    home = await client.get_bytes(FISHCAKE_HOME_URL, allowed_hosts=FISHCAKE_HOSTS)
    parser = _ScriptParser()
    parser.feed(_text(home.body))
    if not parser.source_revision:
        raise FishCakeFormatError("FishCake source revision is missing")
    scripts = discover_script_urls(home.body, home.url)
    if len(scripts) != 1:
        raise FishCakeFormatError("multiple FishCake application scripts")
    script = await client.get_bytes(scripts[0], allowed_hosts=FISHCAKE_HOSTS)
    return FishCakeDiscovery(
        source_revision=parser.source_revision,
        home=home,
        script=script,
        assets=discover_data_assets(script.body, script.url),
    )
