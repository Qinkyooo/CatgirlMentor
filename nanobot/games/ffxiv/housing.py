"""Current FF14 housing sale cards mirrored from house.ffxiv.cyou."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .http import FetchError, FetchResponse
from .result import error_result, success_result
from .types import Evidence, Freshness

AREA_NAMES = ("海雾村", "薰衣草苗圃", "高脚孤丘", "白银乡", "穹顶皓天")
SIZE_NAMES = ("S", "M", "L")
PURCHASE_METHODS = {0: "不可购买", 1: "先到先得", 2: "抽签"}
ELIGIBILITY = {
    0: "部队或个人均可购买",
    1: "仅供部队购买",
    2: "仅供个人购买",
}
STAGE_TEXT = {
    0: "正在出售",
    1: "现正火热预约中！",
    2: "抽签结果已公布",
    3: "即将开始抽签预约！",
}
VACANCY_STAGES = frozenset({STAGE_TEXT[1], STAGE_TEXT[3]})

VisibleCard: TypeAlias = dict[str, str | int | bool | None]


class HousingPayloadError(ValueError):
    """The live sales payload does not match the reviewed contract."""


class UnsupportedHousingSiteVersionError(RuntimeError):
    """A site enum or rendering contract changed without review."""


@dataclass(frozen=True, slots=True)
class SaleCard:
    server_id: int
    area_id: int
    ward: int
    plot: int
    size: Literal["S", "M", "L"]
    price: int
    purchase_type: int
    region_type: int
    state: int
    phase_at: datetime | None
    participant_count: int | None
    winning_number: int | None
    inferred: bool
    updated_at: datetime | None
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class HousingCardMetadata:
    descriptions: Mapping[tuple[int, int], str]
    version: str


class _HousingHttp(Protocol):
    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int = 8 * 1024 * 1024,
    ) -> FetchResponse:
        ...


class _HousingScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        source = values.get("src")
        if tag == "script" and source and values.get("type") == "module":
            self.sources.append(source)
def _json_array_at(script: str, start: int) -> object:
    if start < 0 or start >= len(script) or script[start] != "[":
        raise UnsupportedHousingSiteVersionError("description table start was not found")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(script), start + 200_000)):
        char = script[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(script[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise UnsupportedHousingSiteVersionError(
                        "description table is not bounded JSON"
                    ) from exc
    raise UnsupportedHousingSiteVersionError("description table exceeds safety bound")


def extract_housing_metadata(script: bytes, *, version: str) -> HousingCardMetadata:
    try:
        text = script.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedHousingSiteVersionError("housing bundle is not UTF-8") from exc
    stages_found = False
    tables: list[list[object]] = []
    # Parse inert JSON arrays, independent of minified names and whitespace.
    # Keep the reviewed stage order and 5 x 60 plot mapping as invariants.
    for match in re.finditer(r'\[\s*(?:\[\s*)?"', text):
        try:
            candidate = _json_array_at(text, match.start())
        except UnsupportedHousingSiteVersionError:
            continue
        if candidate == list(STAGE_TEXT.values()):
            stages_found = True
        if isinstance(candidate, list):
            rows = cast(list[object], candidate)
            if len(rows) == len(AREA_NAMES) and all(
                isinstance(row, list) and len(cast(list[object], row)) == 60
                and all(isinstance(value, str) for value in cast(list[object], row))
                for row in rows
            ):
                tables.append(rows)
    if not stages_found:
        raise UnsupportedHousingSiteVersionError(
            "reviewed housing stage wording changed"
        )
    if len(tables) != 1:
        raise UnsupportedHousingSiteVersionError("housing description table missing or ambiguous")
    table_rows = tables[0]
    if len(table_rows) != len(AREA_NAMES):
        raise UnsupportedHousingSiteVersionError("housing description areas changed")
    descriptions: dict[tuple[int, int], str] = {}
    for area_id, entries in enumerate(table_rows):
        if not isinstance(entries, list):
            raise UnsupportedHousingSiteVersionError(
                "housing description plot table changed"
            )
        plot_descriptions = cast(list[object], entries)
        if len(plot_descriptions) != 60:
            raise UnsupportedHousingSiteVersionError(
                "housing description plot count changed"
            )
        for index, description in enumerate(plot_descriptions):
            if not isinstance(description, str) or len(description) > 300:
                raise UnsupportedHousingSiteVersionError(
                    "invalid housing description"
                )
            descriptions[(area_id, index + 1)] = description.strip()
    return HousingCardMetadata(descriptions=descriptions, version=version)


class HousingMetadataSource:
    """Discover and validate the site's current versioned card renderer."""

    def __init__(self, http: _HousingHttp) -> None:
        self._http = http
        self._cached: HousingCardMetadata | None = None
        self._expires_at = 0.0

    async def get(self) -> HousingCardMetadata:
        if self._cached is not None and time.monotonic() < self._expires_at:
            return self._cached
        try:
            home = await self._http.get_bytes(
                f"https://{PRIMARY_HOUSING_HOST}/",
                allowed_hosts=HOUSING_HOSTS,
                max_bytes=1_000_000,
            )
        except FetchError:
            home = await self._http.get_bytes(
                f"https://{FALLBACK_HOUSING_HOST}/",
                allowed_hosts=HOUSING_HOSTS,
                max_bytes=1_000_000,
            )
        parser = _HousingScripts()
        parser.feed(home.body.decode("utf-8", errors="replace"))
        urls = list(dict.fromkeys(urljoin(home.url, src) for src in parser.sources))
        if not urls or len(urls) > 4:
            raise UnsupportedHousingSiteVersionError("housing module scripts missing or exceed limit")
        found: list[HousingCardMetadata] = []
        for bundle_url in urls:
            parsed = urlsplit(bundle_url)
            if parsed.hostname not in HOUSING_HOSTS:
                continue
            bundle = await self._http.get_bytes(
                bundle_url, allowed_hosts=HOUSING_HOSTS, max_bytes=4_000_000,
            )
            try:
                found.append(extract_housing_metadata(bundle.body, version=Path(parsed.path).name))
            except UnsupportedHousingSiteVersionError:
                continue
        if len(found) != 1:
            raise UnsupportedHousingSiteVersionError("housing card metadata missing or ambiguous")
        metadata = found[0]
        self._cached = metadata
        self._expires_at = time.monotonic() + 300
        return metadata


def _require_int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise HousingPayloadError(f"{key} must be an integer")
    return value


def _timestamp(value: int, *, key: str, optional: bool = False) -> datetime | None:
    if optional and value <= 0:
        return None
    if value < 0:
        raise HousingPayloadError(f"{key} must be a Unix timestamp")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise HousingPayloadError(f"{key} is outside the supported timestamp range") from exc


def _parse_row(row: object, *, expected_server: int) -> SaleCard:
    if not isinstance(row, dict):
        raise HousingPayloadError("every sale row must be an object")
    dynamic = cast(dict[str, Any], row)
    server_id = _require_int(dynamic, "Server")
    if server_id != expected_server:
        raise HousingPayloadError(
            f"sale response server {server_id} does not match requested server "
            f"{expected_server}"
        )
    area_id = _require_int(dynamic, "Area")
    slot = _require_int(dynamic, "Slot")
    plot = _require_int(dynamic, "ID")
    price = _require_int(dynamic, "Price")
    size_id = _require_int(dynamic, "Size")
    purchase_type = _require_int(dynamic, "PurchaseType")
    region_type = _require_int(dynamic, "RegionType")
    state = _require_int(dynamic, "State")
    first_seen = _require_int(dynamic, "FirstSeen")
    last_seen = _require_int(dynamic, "LastSeen")
    participant = _require_int(dynamic, "Participate")
    winner = _require_int(dynamic, "Winner")
    end_time = _require_int(dynamic, "EndTime")
    update_time = _require_int(dynamic, "UpdateTime")

    if area_id not in range(len(AREA_NAMES)):
        raise HousingPayloadError("Area must be between 0 and 4")
    if slot not in range(30):
        raise HousingPayloadError("Slot must be between 0 and 29")
    if plot not in range(1, 61):
        raise HousingPayloadError("ID must be between 1 and 60")
    if price < 0:
        raise HousingPayloadError("Price must not be negative")
    if size_id not in range(len(SIZE_NAMES)):
        raise HousingPayloadError("Size must be 0, 1, or 2")
    if purchase_type not in PURCHASE_METHODS:
        raise HousingPayloadError("PurchaseType has an unsupported value")
    if region_type not in ELIGIBILITY:
        raise HousingPayloadError("RegionType has an unsupported value")
    if state not in STAGE_TEXT:
        raise HousingPayloadError("State has an unsupported value")

    source_updated = _timestamp(last_seen, key="LastSeen")
    explicit_update = _timestamp(update_time, key="UpdateTime", optional=True)
    return SaleCard(
        server_id=server_id,
        area_id=area_id,
        ward=slot + 1,
        plot=plot,
        size=SIZE_NAMES[size_id],
        price=price,
        purchase_type=purchase_type,
        region_type=region_type,
        state=state,
        phase_at=_timestamp(end_time, key="EndTime", optional=True),
        participant_count=participant if participant >= 0 else None,
        winning_number=winner if winner > 0 else None,
        inferred=explicit_update is None,
        updated_at=explicit_update or source_updated,
        first_seen_at=cast(datetime, _timestamp(first_seen, key="FirstSeen")),
    )


def parse_sales(body: bytes, *, expected_server: int) -> tuple[SaleCard, ...]:
    """Parse one current /api/sales response without retaining unrelated fields."""
    if len(body) > 8 * 1024 * 1024:
        raise HousingPayloadError("sales response is too large")
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HousingPayloadError("sales response is not valid JSON") from exc
    if not isinstance(payload, list):
        raise HousingPayloadError("sales response must be a list")
    raw_rows = cast(list[object], payload)
    if len(raw_rows) > 5_000:
        raise HousingPayloadError("sales response contains too many rows")

    result: list[SaleCard] = []
    identities: dict[tuple[int, int, int, int], SaleCard] = {}
    for raw_row in raw_rows:
        row = _parse_row(raw_row, expected_server=expected_server)
        identity = (row.server_id, row.area_id, row.ward, row.plot)
        previous = identities.get(identity)
        if previous is not None:
            if previous != row:
                raise HousingPayloadError(f"conflicting duplicate sale identity: {identity}")
            continue
        identities[identity] = row
        result.append(row)
    return tuple(result)


def _site_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def _format_time(value: datetime, timezone_name: str, pattern: str) -> str:
    return value.astimezone(_site_timezone(timezone_name)).strftime(pattern)


def render_sale_card(
    card: SaleCard,
    *,
    metadata: HousingCardMetadata,
    timezone_name: str,
) -> VisibleCard:
    """Render only fields visible on the reviewed sale-card UI."""
    if card.purchase_type not in PURCHASE_METHODS:
        raise UnsupportedHousingSiteVersionError("unknown PurchaseType")
    if card.region_type not in ELIGIBILITY:
        raise UnsupportedHousingSiteVersionError("unknown RegionType")
    if card.state not in STAGE_TEXT:
        raise UnsupportedHousingSiteVersionError("unknown State")

    if card.purchase_type == 0:
        stage = "即将开放购买"
    else:
        stage = STAGE_TEXT[card.state]
    participant_text: str | None = None
    if card.purchase_type == 2 and not card.inferred:
        if card.state == 1 and card.participant_count is not None:
            participant_text = f"{card.participant_count} 人预约"
        elif card.state == 2 and card.winning_number is not None:
            participant_text = f"{card.winning_number} 号中签"
        elif card.state == 2 and card.participant_count == 0:
            participant_text = "无人参与"

    phase_time: str | None = None
    if card.purchase_type == 2 and card.phase_at is not None:
        suffix = "开始" if card.state == 3 else "截止"
        phase_time = f"{_format_time(card.phase_at, timezone_name, '%m-%d %H:%M')} {suffix}"

    return {
        "location": f"{AREA_NAMES[card.area_id]} {card.ward} 区 {card.plot} 号",
        "size": card.size,
        "price": f"{card.price:,}",
        "description": metadata.descriptions.get((card.area_id, card.plot)),
        "purchaseMethod": PURCHASE_METHODS[card.purchase_type],
        "eligibility": ELIGIBILITY[card.region_type],
        "stage": stage,
        "phaseTime": phase_time,
        "participantText": participant_text,
        "inferred": card.inferred,
        "updatedAt": (
            _format_time(card.updated_at, timezone_name, "%Y-%m-%d %H:%M:%S")
            if card.updated_at is not None
            else None
        ),
    }


def normalize_site_sale(card: SaleCard, *, now: datetime) -> SaleCard:
    """Mirror the reviewed site's cycle fill-in for missing lottery snapshots."""
    if card.purchase_type != 2 or (card.state != 0 and card.phase_at is not None):
        return card
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    application = timedelta(days=5)
    results = timedelta(days=4)
    cycle = application + results
    cycle_start = datetime(2022, 8, 8, 15, 0, tzinfo=UTC)
    first_seen = card.first_seen_at.astimezone(UTC)
    current = now.astimezone(UTC)
    while cycle_start > first_seen + cycle:
        cycle_start -= cycle
    while cycle_start < first_seen:
        cycle_start += cycle

    if current < cycle_start:
        return replace(card, state=3, phase_at=cycle_start, inferred=True)
    while current > cycle_start + cycle:
        cycle_start += cycle
    if current < cycle_start + application:
        return replace(
            card,
            state=1,
            phase_at=cycle_start + application,
            inferred=True,
        )
    return replace(card, state=2, phase_at=cycle_start + cycle, inferred=True)


def _card_price(card: Mapping[str, object]) -> int:
    value = card.get("price")
    if not isinstance(value, str):
        return -1
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return -1


def filter_vacancies(
    cards: Sequence[VisibleCard],
    *,
    phase: str | None = None,
    eligibility: str | None = None,
    size: str | None = None,
    area: str | None = None,
    max_price: int | None = None,
    description_query: str | None = None,
) -> list[VisibleCard]:
    """Filter the two website stages the user defines as an available vacancy."""
    result: list[VisibleCard] = []
    phase_stages = {
        "current": STAGE_TEXT[1],
        "upcoming": STAGE_TEXT[3],
        "published": STAGE_TEXT[2],
    }
    phase_stage = phase_stages.get(phase) if phase is not None else None
    eligibility_options = {
        "personal": {ELIGIBILITY[0], ELIGIBILITY[2]},
        "free_company": {ELIGIBILITY[0], ELIGIBILITY[1]},
        "both": {ELIGIBILITY[0]},
    }
    eligibility_text = (
        eligibility_options.get(eligibility) if eligibility is not None else None
    )
    area_value = area.strip() if isinstance(area, str) else None
    description_value = (
        description_query.strip() if isinstance(description_query, str) else None
    )

    for card in cards:
        if card.get("stage") not in VACANCY_STAGES:
            continue
        if card.get("purchaseMethod") != PURCHASE_METHODS[2]:
            continue
        if phase_stage is not None and card.get("stage") != phase_stage:
            continue
        if eligibility_text is not None and card.get("eligibility") not in eligibility_text:
            continue
        if size is not None and card.get("size") != size:
            continue
        location = card.get("location")
        if area_value and (
            not isinstance(location, str) or not location.startswith(f"{area_value} ")
        ):
            continue
        if max_price is not None and _card_price(card) > max_price:
            continue
        description = card.get("description")
        if description_value and (
            not isinstance(description, str) or description_value not in description
        ):
            continue
        result.append(dict(card))
    return result


def _load_servers() -> tuple[dict[str, int], dict[int, str]]:
    path = Path(__file__).with_name("ffxiv-servers.json")
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("invalid packaged FF14 server table")
    payload_map = cast(dict[str, object], payload)
    data_centers = payload_map.get("dataCenters")
    if not isinstance(data_centers, dict):
        raise RuntimeError("invalid packaged FF14 server table")
    by_name: dict[str, int] = {}
    by_id: dict[int, str] = {}
    centers = cast(dict[str, object], data_centers)
    for worlds in centers.values():
        if not isinstance(worlds, dict):
            raise RuntimeError("invalid packaged FF14 server table")
        world_map = cast(dict[object, object], worlds)
        for name, server_id in world_map.items():
            if (
                not isinstance(name, str)
                or not isinstance(server_id, int)
                or isinstance(server_id, bool)
            ):
                raise RuntimeError("invalid packaged FF14 server table")
            if name in by_name or server_id in by_id:
                raise RuntimeError("duplicate packaged FF14 server mapping")
            by_name[name] = server_id
            by_id[server_id] = name
    return by_name, by_id


SERVER_IDS, SERVER_NAMES = _load_servers()
HOUSING_HOSTS = frozenset({"house.ffxiv.cyou", "househelper.ffxiv.cyou"})
PRIMARY_HOUSING_HOST = "house.ffxiv.cyou"
FALLBACK_HOUSING_HOST = "househelper.ffxiv.cyou"
HOUSING_DISCLAIMER = "玩家工具上报聚合，非官方数据，可能延迟"


def resolve_server(value: object) -> tuple[int, str] | None:
    if isinstance(value, int) and not isinstance(value, bool):
        name = SERVER_NAMES.get(value)
        return (value, name) if name is not None else None
    if not isinstance(value, str):
        return None
    normalized = "".join(value.split())
    if normalized.isdecimal():
        server_id = int(normalized)
        name = SERVER_NAMES.get(server_id)
        return (server_id, name) if name is not None else None
    server_id = SERVER_IDS.get(normalized)
    return (server_id, normalized) if server_id is not None else None


def _area_id(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return -1
    normalized = "".join(value.split())
    try:
        return AREA_NAMES.index(normalized)
    except ValueError:
        return -1


class HousingService:
    """Fetch current website rows for every action and mirror visible cards."""

    def __init__(
        self,
        *,
        http: _HousingHttp,
        metadata: HousingCardMetadata | None = None,
        metadata_source: HousingMetadataSource | None = None,
        timezone_name: str,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._metadata = metadata
        self._static_metadata = metadata is not None
        self._metadata_source = metadata_source or HousingMetadataSource(http)
        self._timezone_name = timezone_name
        self._clock = clock

    async def _fetch_sales(
        self, server_id: int
    ) -> tuple[tuple[SaleCard, ...], FetchResponse]:
        primary = f"https://{PRIMARY_HOUSING_HOST}/api/sales?server={server_id}"
        try:
            response = await self._http.get_bytes(
                primary,
                allowed_hosts=HOUSING_HOSTS,
                max_bytes=8 * 1024 * 1024,
            )
        except FetchError:
            fallback = f"https://{FALLBACK_HOUSING_HOST}/api/sales?server={server_id}"
            response = await self._http.get_bytes(
                fallback,
                allowed_hosts=HOUSING_HOSTS,
                max_bytes=8 * 1024 * 1024,
            )
        return parse_sales(response.body, expected_server=server_id), response

    def _render(self, rows: Sequence[SaleCard]) -> list[VisibleCard]:
        if self._metadata is None:
            raise UnsupportedHousingSiteVersionError("housing metadata is not loaded")
        now = self._clock()
        return [
            render_sale_card(
                normalize_site_sale(row, now=now),
                metadata=self._metadata,
                timezone_name=self._timezone_name,
            )
            for row in rows
        ]

    async def execute(self, **kwargs: Any) -> Any:
        action = kwargs.get("action")
        if action not in {"vacancies", "detail", "recommend"}:
            return error_result("invalid_action", f"不支持的房屋 action: {action}")
        server_value = kwargs.get("server")
        if server_value is None or (
            isinstance(server_value, str) and not server_value.strip()
        ):
            return error_result(
                "missing_server",
                "查询房屋必须指定中文服务器名或服务器 ID。",
                suggestions=tuple(SERVER_IDS),
            )
        resolved = resolve_server(server_value)
        if resolved is None:
            return error_result(
                "unknown_server",
                "未找到完全匹配的中国区服务器；不会模糊猜测。",
                suggestions=tuple(SERVER_IDS),
            )
        server_id, server_name = resolved

        area_value = _area_id(kwargs.get("area"))
        if area_value == -1:
            return error_result(
                "invalid_area",
                "房区必须是海雾村、薰衣草苗圃、高脚孤丘、白银乡或穹顶皓天。",
                suggestions=AREA_NAMES,
            )
        ward = kwargs.get("ward")
        plot = kwargs.get("plot")
        if action == "detail" and (
            area_value is None
            or not isinstance(ward, int)
            or isinstance(ward, bool)
            or not isinstance(plot, int)
            or isinstance(plot, bool)
        ):
            return error_result(
                "missing_house_identity",
                "detail 必须同时指定 area、ward 和 plot。",
                suggestions=("area", "ward", "plot"),
            )
        try:
            rows, response = await self._fetch_sales(server_id)
        except FetchError:
            return error_result(
                "housing_unavailable",
                "房屋网站当前不可用，未使用旧数据冒充实时结果。",
            )
        except HousingPayloadError as exc:
            return error_result(
                "unsupported_housing_payload",
                f"房屋网站响应与已审核契约不一致：{exc}",
            )

        if not self._static_metadata:
            try:
                self._metadata = await self._metadata_source.get()
            except (FetchError, UnsupportedHousingSiteVersionError) as exc:
                return error_result(
                    "unsupported_housing_site_version",
                    f"无法验证房屋网站当前卡片展示契约：{exc}",
                )

        assert self._metadata is not None  # Injected at construction or loaded above.
        cards = self._render(rows)
        if action == "detail":
            assert area_value is not None
            location = f"{AREA_NAMES[area_value]} {ward} 区 {plot} 号"
            selected = [card for card in cards if card.get("location") == location]
            if not selected:
                return error_result(
                    "housing_plot_not_listed",
                    "当前房源卡片中未列出指定房屋。",
                    suggestions=(location,),
                )
        else:
            selected = filter_vacancies(
                cards,
                phase=kwargs.get("phase"),
                eligibility=kwargs.get("eligibility"),
                size=kwargs.get("size"),
                area=AREA_NAMES[area_value] if area_value is not None else None,
                max_price=kwargs.get("max_price"),
                description_query=kwargs.get("description_query"),
            )
            if action == "recommend":
                size_score = {"S": 1, "M": 2, "L": 3}
                selected.sort(
                    key=lambda card: (
                        -size_score.get(cast(str, card.get("size")), 0),
                        _card_price(card),
                    )
                )

        fetched_at = self._clock()
        latest_update = max(
            (row.updated_at for row in rows if row.updated_at is not None),
            default=None,
        )
        return success_result(
            kind=f"housing_{action}",
            data={
                "serverId": server_id,
                "serverName": server_name,
                "cards": selected,
                "filters": {
                    key: kwargs[key]
                    for key in (
                        "area",
                        "ward",
                        "plot",
                        "size",
                        "phase",
                        "eligibility",
                        "max_price",
                        "description_query",
                    )
                    if kwargs.get(key) is not None
                },
                "fetchedAt": fetched_at,
                "websiteUpdateTime": latest_update,
                "renderMetadataVersion": self._metadata.version,
            },
            evidence=(
                Evidence(
                    source="FF14房屋玩家上报网站",
                    source_url=response.url,
                    excerpt=HOUSING_DISCLAIMER,
                ),
            ),
            freshness=Freshness(
                source="FF14房屋玩家上报网站",
                source_updated_at=latest_update,
                cache_status="fresh",
                stale=False,
            ),
            warnings=(HOUSING_DISCLAIMER,),
        )
