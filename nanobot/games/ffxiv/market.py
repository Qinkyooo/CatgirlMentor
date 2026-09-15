"""Chinese FF14 item resolution and scoped Universalis prices."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Protocol, cast
from urllib.parse import quote

from .housing import SERVER_IDS
from .http import FetchError, FetchResponse
from .result import error_result, success_result
from .types import Evidence, Freshness
from .wiki import PINNED_VERSION, ItemCandidate, WikiSourceError, item_name_similarity

UNIVERSALIS_HOSTS = frozenset({"universalis.app"})
UNIVERSALIS_API = "https://universalis.app/api/v2"
DATA_CENTERS = frozenset({"陆行鸟", "莫古力", "猫小胖", "豆豆柴"})
REGIONS = frozenset({"中国"})


class _Http(Protocol):
    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
    ) -> FetchResponse: ...


class _Items(Protocol):
    async def search_items(self, name: str) -> tuple[ItemCandidate, ...]: ...


class MarketPayloadError(ValueError):
    """A market response cannot be safely normalized."""


@dataclass(frozen=True, slots=True)
class ItemRef:
    row_id: int
    name_zh: str
    item_level: int | None
    marketable: bool
    data_version: str


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(dict[str, object], raw)


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MarketPayloadError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise MarketPayloadError(f"{field} must be numeric") from exc
    if not number.is_finite() or number < 0:
        raise MarketPayloadError(f"{field} must be finite and non-negative")
    return number


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _timestamp(value: object, *, milliseconds: bool) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value) / (1000 if milliseconds else 1)
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _scope_layer(scope: str) -> str | None:
    if scope in SERVER_IDS:
        return "world"
    if scope in DATA_CENTERS:
        return "dc"
    if scope in REGIONS:
        return "region"
    return None


def _canonical_scope(scope: str) -> str:
    normalized = _normalize(scope)
    if _scope_layer(normalized) is not None:
        return normalized
    for suffix in ("大区", "区"):
        if normalized.endswith(suffix):
            candidate = normalized[: -len(suffix)]
            if _scope_layer(candidate) is not None:
                return candidate
    return normalized


def _nested_price(
    quality: Mapping[str, object],
    section: str,
    layer: str,
) -> Decimal | None:
    section_map = _mapping(quality.get(section))
    layer_map = _mapping(section_map.get(layer)) if section_map is not None else None
    if layer_map is None or "price" not in layer_map:
        return None
    return _decimal(layer_map["price"], field=f"{section}.{layer}.price")


def parse_aggregate(
    payload: object,
    *,
    item_id: int,
    layer: str,
    quality: str,
) -> dict[str, object]:
    root = _mapping(payload)
    results_value = root.get("results") if root is not None else None
    if not isinstance(results_value, list):
        raise MarketPayloadError("aggregate results must be a list")
    results = cast(list[object], results_value)
    if len(results) != 1:
        raise MarketPayloadError("aggregate response must contain one item")
    row = _mapping(results[0])
    if row is None or row.get("itemId") != item_id:
        raise MarketPayloadError("aggregate item ID mismatch")
    if quality not in {"nq", "hq"}:
        raise MarketPayloadError("aggregate quality must be nq or hq")
    quality_map = _mapping(row.get(quality))
    if quality_map is None:
        raise MarketPayloadError("aggregate quality block is missing")

    minimum = _nested_price(quality_map, "minListing", layer)
    average = _nested_price(quality_map, "averageSalePrice", layer)
    recent = _mapping(quality_map.get("recentPurchase"))
    recent_layer = _mapping(recent.get(layer)) if recent is not None else None
    statistics_at = (
        _timestamp(recent_layer.get("timestamp"), milliseconds=True)
        if recent_layer is not None
        else None
    )
    return {
        "kind": "aggregate",
        "minPrice": int(minimum) if minimum is not None and minimum == int(minimum) else (
            _decimal_text(minimum) if minimum is not None else None
        ),
        "averagePrice": _decimal_text(average) if average is not None else None,
        "medianPrice": None,
        "sampleCount": None,
        "statisticsAt": statistics_at,
    }


def parse_current_listings(
    payload: object,
    *,
    item_id: int,
    quality: str,
) -> dict[str, object]:
    root = _mapping(payload)
    if root is None or root.get("itemID") != item_id:
        raise MarketPayloadError("current listings item ID mismatch")
    listings_value = root.get("listings")
    if not isinstance(listings_value, list):
        raise MarketPayloadError("current listings must be a list")
    normalized: list[dict[str, object]] = []
    prices: list[Decimal] = []
    latest: datetime | None = None
    for raw in cast(list[object], listings_value)[:100]:
        listing = _mapping(raw)
        if listing is None:
            raise MarketPayloadError("listing must be an object")
        hq = listing.get("hq")
        if not isinstance(hq, bool):
            raise MarketPayloadError("listing hq must be boolean")
        if quality == "nq" and hq:
            continue
        if quality == "hq" and not hq:
            continue
        price = _decimal(listing.get("pricePerUnit"), field="pricePerUnit")
        quantity = listing.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise MarketPayloadError("listing quantity must be positive")
        reviewed = _timestamp(listing.get("lastReviewTime"), milliseconds=False)
        if reviewed is not None and (latest is None or reviewed > latest):
            latest = reviewed
        world_id = listing.get("worldID")
        world_name = listing.get("worldName")
        normalized.append(
            {
                "pricePerUnit": int(price)
                if price == int(price)
                else _decimal_text(price),
                "quantity": quantity,
                "hq": hq,
                "worldId": world_id if isinstance(world_id, int) else None,
                "worldName": world_name if isinstance(world_name, str) else None,
                "updatedAt": reviewed,
            }
        )
        prices.append(price)
    if not prices:
        return {
            "kind": "current_listings",
            "minPrice": None,
            "averagePrice": None,
            "medianPrice": None,
            "sampleCount": 0,
            "statisticsAt": latest,
            "listings": normalized,
        }
    average = sum(prices, Decimal(0)) / len(prices)
    middle = median(prices)
    return {
        "kind": "current_listings",
        "minPrice": int(min(prices)),
        "averagePrice": _decimal_text(average),
        "medianPrice": _decimal_text(middle),
        "sampleCount": len(prices),
        "statisticsAt": latest,
        "listings": normalized,
    }


class MarketService:
    def __init__(
        self,
        *,
        http: _Http,
        items: _Items,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._items = items
        self._clock = clock
        self._marketable: tuple[datetime, frozenset[int]] | None = None
        self._price_cache: dict[str, tuple[datetime, dict[str, object]]] = {}

    async def _json(self, url: str, *, max_bytes: int = 4_000_000) -> object:
        response = await self._http.get_bytes(
            url,
            allowed_hosts=UNIVERSALIS_HOSTS,
            max_bytes=max_bytes,
        )
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketPayloadError("market response is not valid JSON") from exc

    async def _marketable_ids(self) -> frozenset[int]:
        now = self._clock()
        if (
            self._marketable is not None
            and now - self._marketable[0] < timedelta(days=1)
        ):
            return self._marketable[1]
        payload = await self._json(f"{UNIVERSALIS_API}/marketable", max_bytes=2_000_000)
        if not isinstance(payload, list):
            raise MarketPayloadError("marketable response must be a list")
        ids = cast(list[object], payload)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in ids):
            raise MarketPayloadError("marketable response contains invalid IDs")
        result = frozenset(cast(list[int], ids))
        self._marketable = (now, result)
        return result

    async def _resolve_item(self, query: str) -> ItemRef | Any:
        normalized = _normalize(query)
        if not normalized:
            return error_result(
                "invalid_item",
                "请提供非空 item_name（准确中文物品名）。",
                suggestions=("铁矿",),
            )
        candidates = await self._items.search_items(normalized)
        exact = tuple(
            candidate
            for candidate in candidates
            if _normalize(candidate.name_zh) == normalized
        )
        if not exact and len(normalized) >= 4 and candidates:
            ranked = sorted(
                candidates,
                key=lambda candidate: item_name_similarity(normalized, candidate.name_zh),
                reverse=True,
            )
            best = item_name_similarity(normalized, ranked[0].name_zh)
            runner_up = (
                item_name_similarity(normalized, ranked[1].name_zh) if len(ranked) > 1 else 0
            )
            # A shortened name must preserve both ends and every input character.
            omitted_words = [
                candidate for candidate in ranked
                if item_name_similarity(normalized, candidate.name_zh) >= 0.8
                and re.fullmatch(".*".join(map(re.escape, normalized)),
                                 _normalize(candidate.name_zh))
            ]
            if best >= 0.8 and (
                best - runner_up >= 0.15
                or omitted_words == [ranked[0]]
            ):
                exact = (ranked[0],)
        if len(exact) != 1:
            if candidates:
                return error_result(
                    "ambiguous_item",
                    "未能唯一确认物品，找到以下相似名称，请选择完整中文名后重试。",
                    suggestions=tuple(
                        f"{candidate.name_zh}（ID {candidate.item_id}）"
                        for candidate in candidates[:10]
                    ),
                )
            return error_result("item_not_found", "未找到该中文物品。")
        selected = exact[0]
        marketable = selected.item_id in await self._marketable_ids()
        return ItemRef(
            row_id=selected.item_id,
            name_zh=selected.name_zh,
            item_level=selected.item_level,
            marketable=marketable,
            data_version=PINNED_VERSION,
        )

    async def execute(self, **kwargs: Any) -> Any:
        if kwargs.get("action") != "price":
            return error_result("invalid_action", "ffxiv_market 仅支持 price。")
        item_name = kwargs.get("item_name")
        if not isinstance(item_name, str):
            return error_result(
                "invalid_item",
                "请提供 item_name（准确中文物品名）。",
                suggestions=("铁矿",),
            )
        scope = kwargs.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            return error_result(
                "missing_market_scope",
                f"查询 {item_name.strip()} 还需指定服务器、大区或中国区。",
                suggestions=tuple((*DATA_CENTERS, "中国")),
            )
        scope = _canonical_scope(scope)
        layer = _scope_layer(scope)
        if layer is None:
            return error_result(
                "invalid_market_scope",
                "未知的服务器、大区或区域。",
                suggestions=tuple((*DATA_CENTERS, "中国")),
            )
        try:
            item = await self._resolve_item(item_name)
        except (FetchError, MarketPayloadError, WikiSourceError):
            return error_result("market_unavailable", "物品或市场数据源当前不可用。")
        if not isinstance(item, ItemRef):
            return item
        if not item.marketable:
            return error_result("item_not_marketable", f"{item.name_zh}不可在市场交易。")

        quality_defaulted = "quality" not in kwargs
        quality = kwargs.get("quality", "nq")
        if quality not in {"any", "nq", "hq"}:
            return error_result("invalid_quality", "quality 必须是 any、nq 或 hq。")
        current = kwargs.get("current_listings") is True
        encoded_scope = quote(scope, safe="")
        try:
            if current:
                url = (
                    f"{UNIVERSALIS_API}/{encoded_scope}/{item.row_id}"
                    "?listings=100&entries=0"
                )
                cached = self._price_cache.get(url)
                if cached is not None and self._clock() < cached[0]:
                    metrics = dict(cached[1])
                else:
                    payload = await self._json(url)
                    metrics = parse_current_listings(
                        payload,
                        item_id=item.row_id,
                        quality=cast(str, quality),
                    )
                    self._price_cache[url] = (
                        self._clock() + timedelta(minutes=1),
                        dict(metrics),
                    )
                warnings: tuple[str, ...] = ()
            else:
                if quality == "any":
                    return error_result(
                        "missing_quality",
                        "聚合价格必须指定 nq 或 hq，避免混合两种品质的不同统计口径。",
                        suggestions=("nq", "hq"),
                    )
                url = f"{UNIVERSALIS_API}/aggregated/{encoded_scope}/{item.row_id}"
                cache_key = f"{url}#{layer}#{quality}"
                cached = self._price_cache.get(cache_key)
                if cached is not None and self._clock() < cached[0]:
                    metrics = dict(cached[1])
                else:
                    payload = await self._json(url)
                    metrics = parse_aggregate(
                        payload,
                        item_id=item.row_id,
                        layer=layer,
                        quality=cast(str, quality),
                    )
                    self._price_cache[cache_key] = (
                        self._clock() + timedelta(minutes=2),
                        dict(metrics),
                    )
                warnings = (
                    "Universalis 聚合接口不提供中位数与样本数，这两个字段明确为 null。",
                )
                if quality_defaulted:
                    warnings += (
                        "未指定 quality，聚合价格默认 NQ（普通品质）口径。",
                    )
        except (FetchError, MarketPayloadError) as exc:
            return error_result("market_unavailable", f"市场价格当前不可用：{exc}")

        statistics_at = metrics.get("statisticsAt")
        if _normalize(item_name) != _normalize(item.name_zh):
            warnings += (
                f'已将“{item_name}”模糊匹配为“{item.name_zh}”（ID {item.row_id}），'
                "以下价格属于该完整名称的物品；请在回复中说明匹配名称。",
            )
        return success_result(
            kind="market_price",
            data={
                "item": item,
                "scope": scope,
                "scopeType": layer,
                "quality": quality,
                **metrics,
            },
            evidence=(
                Evidence(
                    source="Universalis",
                    source_url=url,
                    excerpt=f"{item.name_zh}；{scope}；{quality}",
                ),
            ),
            freshness=Freshness(
                source="Universalis",
                source_updated_at=(
                    statistics_at if isinstance(statistics_at, datetime) else None
                ),
                cache_status="fresh",
                stale=False,
            ),
            warnings=warnings,
        )
