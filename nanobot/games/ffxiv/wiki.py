"""Structured FFCafe facts and bounded encyclopedia fallbacks."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlencode

from .http import FetchError, FetchResponse
from .wiki_cache import RemotePage, WikiCache, WikiCacheHit

FFCAFE_API = "https://xivapi-v2.xivcdn.com/api"
FFCAFE_HOSTS = frozenset({"xivapi-v2.xivcdn.com"})
HUIJI_HOSTS = frozenset({"ff14.huijiwiki.com"})
CONSOLE_HOSTS = frozenset({"ffxiv.consolegameswiki.com"})
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_WIKI_BYTES = 2 * 1024 * 1024
MAX_WIKI_TEXT = 20_000

AcquisitionKind = Literal["craft", "gather", "drop", "shop", "exchange", "quest", "other"]
AcquisitionCoverage = Literal["complete", "partial", "unavailable"]


class _HttpClient(Protocol):
    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        etag: str | None = None,
    ) -> FetchResponse: ...


class WikiSourceError(RuntimeError):
    """A remote source response could not be trusted or normalized."""


class WikiFormatError(WikiSourceError):
    """Required source fields are incompatible, rather than temporarily offline."""


@dataclass(frozen=True, slots=True)
class ItemCandidate:
    item_id: int
    name_zh: str
    item_level: int | None = None
    data_version: str = ""


def item_name_similarity(query: str, name: str) -> float:
    left = "".join(unicodedata.normalize("NFKC", query).split())
    right = "".join(unicodedata.normalize("NFKC", name).split())
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


@dataclass(frozen=True, slots=True)
class AcquisitionMethod:
    kind: AcquisitionKind
    summary: str
    source: str
    source_row_id: int | None
    source_url: str | None


@dataclass(frozen=True, slots=True)
class GameFact:
    item_id: int
    name_zh: str
    item_type: str
    description: str
    uses: tuple[str, ...]
    acquisition_methods: tuple[AcquisitionMethod, ...]
    acquisition_coverage: AcquisitionCoverage
    data_version: str
    schema: str


@dataclass(frozen=True, slots=True)
class WikiLookupResult:
    page: RemotePage | None
    stale: bool
    warnings: tuple[str, ...]
    unavailable: bool


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(Mapping[str, object], raw)


def _list(value: object) -> tuple[object, ...]:
    return tuple(cast(list[object], value)) if isinstance(value, list) else ()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _relation_fields(fields: Mapping[str, object], name: str) -> Mapping[str, object]:
    if name not in fields:
        return {}
    relation = _mapping(fields.get(name))
    nested = _mapping(relation.get("fields")) if relation is not None else None
    if nested is None:
        raise WikiFormatError(f"FFCafe {name} 关联字段无效")
    return nested


def _field_text(fields: Mapping[str, object], name: str) -> str:
    value = fields.get(name, "")
    if not isinstance(value, str):
        raise WikiFormatError(f"FFCafe {name} 必须是文本")
    return value.strip()


def _field_integer(fields: Mapping[str, object], name: str, *, minimum: int = 0) -> int | None:
    if name not in fields:
        return None
    value = _integer(fields[name])
    if value is None or value < minimum:
        raise WikiFormatError(f"FFCafe {name} 必须是大于等于 {minimum} 的整数")
    return value


def _validate_ffcafe(payload: Mapping[str, object]) -> None:
    # Revisions are provenance, not a compatibility contract. Validate the
    # fields consumed by each endpoint below, including on familiar revisions.
    for key in ("schema", "version"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise WikiFormatError(f"FFCafe 缺少有效的 {key} 标识")


def _result_rows(payload: Mapping[str, object], sheet: str) -> tuple[Mapping[str, object], ...]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise WikiFormatError(f"FFCafe {sheet} results 必须是数组")
    result: list[Mapping[str, object]] = []
    for raw in cast(list[object], rows):
        row = _mapping(raw)
        if row is None:
            raise WikiFormatError(f"FFCafe {sheet} 行必须是对象")
        row_id = _integer(row.get("row_id"))
        if row_id is None or row_id <= 0 or row.get("sheet", sheet) != sheet:
            raise WikiFormatError(f"FFCafe {sheet} 行 ID 或类型无效")
        if _mapping(row.get("fields")) is None:
            raise WikiFormatError(f"FFCafe {sheet} 缺少 fields")
        result.append(row)
    return tuple(result)


def normalize_recipe_methods(
    payload: Mapping[str, object], *, item_id: int
) -> tuple[AcquisitionMethod, ...]:
    _validate_ffcafe(payload)
    methods: list[AcquisitionMethod] = []
    for raw_row in _result_rows(payload, "Recipe"):
        row = _mapping(raw_row)
        fields = _mapping(row.get("fields")) if row is not None else None
        if row is None or fields is None:
            continue
        target = _integer(fields.get("ItemResult@as(raw)"))
        if target is None or target <= 0:
            raise WikiFormatError("FFCafe Recipe 缺少有效的物品关联")
        if target != item_id:
            continue
        row_id = _integer(row.get("row_id"))
        craft_type = _field_text(_relation_fields(fields, "CraftType"), "Name")
        level = _field_integer(
            _relation_fields(fields, "RecipeLevelTable"), "ClassJobLevel"
        )
        amount = _field_integer(fields, "AmountResult", minimum=1)
        details = [craft_type or "生产职业"]
        if level is not None:
            details.append(f"配方等级 {level}")
        if amount is not None:
            details.append(f"产量 {amount}")
        methods.append(
            AcquisitionMethod(
                kind="craft",
                summary=f"可通过{'，'.join(details)}制作",
                source="FFCafe Recipe",
                source_row_id=row_id,
                source_url=(
                    f"{FFCAFE_API}/sheet/Recipe/{row_id}?language=chs"
                    if row_id is not None
                    else None
                ),
            )
        )
    return tuple(methods)


def normalize_gathering_methods(
    payload: Mapping[str, object], *, item_id: int
) -> tuple[AcquisitionMethod, ...]:
    _validate_ffcafe(payload)
    methods: list[AcquisitionMethod] = []
    for raw_row in _result_rows(payload, "GatheringItem"):
        row = _mapping(raw_row)
        fields = _mapping(row.get("fields")) if row is not None else None
        if row is None or fields is None:
            continue
        target = _integer(fields.get("Item@as(raw)"))
        if target is None or target <= 0:
            raise WikiFormatError("FFCafe GatheringItem 缺少有效的物品关联")
        if target != item_id:
            continue
        row_id = _integer(row.get("row_id"))
        level = _field_integer(
            _relation_fields(fields, "GatheringItemLevel"), "GatheringItemLevel"
        )
        summary = "可通过采集获得"
        if level is not None:
            summary += f"（采集品等级 {level}）"
        methods.append(
            AcquisitionMethod(
                kind="gather",
                summary=summary,
                source="FFCafe GatheringItem",
                source_row_id=row_id,
                source_url=(
                    f"{FFCAFE_API}/sheet/GatheringItem/{row_id}?language=chs"
                    if row_id is not None
                    else None
                ),
            )
        )
    return tuple(methods)


class FFCafeClient:
    def __init__(self, *, http: _HttpClient) -> None:
        self._http = http

    async def _json(self, url: str) -> Mapping[str, object]:
        try:
            response = await self._http.get_bytes(
                url,
                allowed_hosts=FFCAFE_HOSTS,
                max_bytes=MAX_JSON_BYTES,
            )
            value = json.loads(response.body)
        except FetchError as exc:
            raise WikiSourceError(f"FFCafe 请求失败: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WikiFormatError("FFCafe 响应不是有效的 JSON") from exc
        payload = _mapping(value)
        if payload is None:
            raise WikiFormatError("FFCafe 响应不是 JSON 对象")
        _validate_ffcafe(payload)
        return payload

    async def search_items(self, name: str) -> tuple[ItemCandidate, ...]:
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        query = urlencode(
            {
                "sheets": "Item",
                "fields": "Name,LevelItem",
                "query": f'Name="{escaped}"',
                "language": "chs",
                "limit": "10",
            }
        )
        payload = await self._json(f"{FFCAFE_API}/search?{query}")
        fuzzy = not _result_rows(payload, "Item")
        if fuzzy and len(name) >= 2:
            # Bounded recall for omitted words and small typos; exact lookup stays first.
            terms = tuple(dict.fromkeys(name[i:i + 2] for i in range(len(name) - 1)))[:24]
            clauses = [f'Name~{json.dumps(term, ensure_ascii=False)}' for term in terms]
            query = urlencode({
                "sheets": "Item", "fields": "Name,LevelItem",
                "query": " ".join(clauses), "language": "chs", "limit": "50",
            })
            payload = await self._json(f"{FFCAFE_API}/search?{query}")
        candidates: list[ItemCandidate] = []
        for raw in _result_rows(payload, "Item"):
            row = _mapping(raw)
            fields = _mapping(row.get("fields")) if row is not None else None
            if row is None or fields is None:
                continue
            item_id = _integer(row.get("row_id"))
            item_name = _text(fields.get("Name"))
            if not item_name:
                raise WikiFormatError("FFCafe Item 缺少有效的 Name")
            if item_id is not None and item_name:
                level_item = _mapping(fields.get("LevelItem"))
                item_level = (
                    _integer(level_item.get("row_id"))
                    if level_item is not None
                    else None
                )
                if "LevelItem" in fields and (item_level is None or item_level < 0):
                    raise WikiFormatError("FFCafe Item LevelItem 无效")
                candidates.append(ItemCandidate(item_id, item_name, item_level,
                                                _text(payload.get("version"))))
        if fuzzy:
            candidates = sorted(
                (candidate for candidate in candidates
                 if item_name_similarity(name, candidate.name_zh) >= 0.6),
                key=lambda candidate: item_name_similarity(name, candidate.name_zh),
                reverse=True,
            )[:10]
        return tuple(candidates)

    async def game_fact(self, item_id: int) -> GameFact:
        item_fields = (
            "Name,Description,ItemUICategory.Name,"
            "ItemSearchCategory.Name,ClassJobUse.Name"
        )
        item_query = urlencode({"fields": item_fields, "language": "chs"})
        item = await self._json(f"{FFCAFE_API}/sheet/Item/{item_id}?{item_query}")
        if _integer(item.get("row_id")) != item_id:
            raise WikiFormatError("FFCafe Item 行 ID 不匹配")
        fields = _mapping(item.get("fields"))
        if fields is None:
            raise WikiFormatError("FFCafe Item 缺少 fields")
        if not _text(fields.get("Name")) or not isinstance(fields.get("Description"), str):
            raise WikiFormatError("FFCafe Item 缺少有效的名称或描述")

        recipe_query = urlencode(
            {
                "sheets": "Recipe",
                "fields": (
                    "ItemResult.Name,ItemResult@as(raw),AmountResult,"
                    "CraftType.Name,RecipeLevelTable.ClassJobLevel"
                ),
                "query": f"ItemResult={item_id}",
                "language": "chs",
                "limit": "100",
            }
        )
        gathering_query = urlencode(
            {
                "sheets": "GatheringItem",
                "fields": "Item.Name,Item@as(raw),GatheringItemLevel.GatheringItemLevel",
                "query": f"Item={item_id}",
                "language": "chs",
                "limit": "100",
            }
        )
        recipes = await self._json(f"{FFCAFE_API}/search?{recipe_query}")
        gathering = await self._json(f"{FFCAFE_API}/search?{gathering_query}")
        methods = (
            *normalize_recipe_methods(recipes, item_id=item_id),
            *normalize_gathering_methods(gathering, item_id=item_id),
        )
        item_type = _field_text(_relation_fields(fields, "ItemUICategory"), "Name")
        market_category = _field_text(
            _relation_fields(fields, "ItemSearchCategory"), "Name"
        )
        class_job = _field_text(_relation_fields(fields, "ClassJobUse"), "Name")
        uses = tuple(
            value
            for value in (
                f"市场分类：{market_category}" if market_category else "",
                f"可用职业：{class_job}" if class_job else "",
            )
            if value
        )
        return GameFact(
            item_id=item_id,
            name_zh=_text(fields.get("Name")),
            item_type=item_type or "未分类",
            description=_text(fields.get("Description")),
            uses=uses,
            acquisition_methods=methods,
            acquisition_coverage="partial" if methods else "unavailable",
            data_version=_text(item.get("version")),
            schema=_text(item.get("schema")),
        )


class _VisibleTextParser(HTMLParser):
    _IGNORED = frozenset({"script", "style", "nav", "footer", "header", "aside"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        lowered = tag.casefold()
        if lowered in self._IGNORED:
            self._ignored_depth += 1
        elif lowered == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        elif lowered == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)


def _normalize_wiki_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()[:MAX_WIKI_TEXT]


def _console_page(payload: Mapping[str, object], response: FetchResponse, now: datetime) -> RemotePage | None:
    query = _mapping(payload.get("query"))
    pages = _list(query.get("pages")) if query is not None else ()
    if not pages:
        return None
    page = _mapping(pages[0])
    if page is None or page.get("missing") is True:
        return None
    title = _text(page.get("title"))
    extract = _normalize_wiki_text(_text(page.get("extract")))
    if not title or not extract or "may refer to:" in extract.casefold():
        return None
    page_id = _integer(page.get("pageid"))
    return RemotePage(
        source="console_wiki",
        canonical_title=title,
        language="en",
        source_url=response.url,
        fetched_at=now,
        source_revision=str(page_id) if page_id is not None else None,
        etag=response.headers.get("etag"),
        normalized_text=extract,
        raw_size_bytes=len(response.body),
        facts={"pageId": page_id, "title": title},
    )


def _huiji_page(response: FetchResponse, now: datetime, requested_title: str) -> RemotePage | None:
    parser = _VisibleTextParser()
    parser.feed(response.body.decode("utf-8-sig"))
    parser.close()
    text = _normalize_wiki_text(" ".join(parser.parts))
    title = _normalize_wiki_text(" ".join(parser.title_parts)) or requested_title
    if not text:
        return None
    return RemotePage(
        source="huiji_wiki",
        canonical_title=title,
        language="zh",
        source_url=response.url,
        fetched_at=now,
        source_revision=None,
        etag=response.headers.get("etag"),
        normalized_text=text,
        raw_size_bytes=len(response.body),
        facts={"title": title},
    )


class WikiLookup:
    def __init__(self, *, http: _HttpClient, cache: WikiCache) -> None:
        self._http = http
        self._cache = cache

    async def _cache_alias(self, requested: str, page: RemotePage, now: datetime) -> None:
        await self._cache.put(page, now=now)
        if page.canonical_title != requested:
            await self._cache.put(
                RemotePage(
                    source=page.source,
                    canonical_title=requested,
                    language=page.language,
                    source_url=page.source_url,
                    fetched_at=page.fetched_at,
                    source_revision=page.source_revision,
                    etag=page.etag,
                    normalized_text=page.normalized_text,
                    raw_size_bytes=page.raw_size_bytes,
                    facts={**dict(page.facts), "resolvedTitle": page.canonical_title},
                ),
                now=now,
            )

    async def lookup(
        self,
        title: str,
        *,
        now: datetime | None = None,
    ) -> WikiLookupResult:
        current = now or datetime.now(UTC)
        cached: dict[str, WikiCacheHit] = {}
        for source, language in (("huiji_wiki", "zh"), ("console_wiki", "en")):
            hit = await self._cache.get(source, title, language, now=current)
            if hit is None:
                continue
            cached[source] = hit
            if not hit.stale:
                return WikiLookupResult(hit.page, False, (), False)

        warnings: list[str] = []
        sources = (
            (
                "huiji_wiki",
                f"https://ff14.huijiwiki.com/wiki/{quote(title, safe='')}",
                HUIJI_HOSTS,
            ),
            (
                "console_wiki",
                "https://ffxiv.consolegameswiki.com/mediawiki/api.php?"
                + urlencode(
                    {
                        "action": "query",
                        "prop": "extracts",
                        "explaintext": "1",
                        "redirects": "1",
                        "format": "json",
                        "formatversion": "2",
                        "titles": title[:200],
                    }
                ),
                CONSOLE_HOSTS,
            ),
        )
        for source, url, hosts in sources:
            old = cached.get(source)
            try:
                response = await self._http.get_bytes(
                    url,
                    allowed_hosts=hosts,
                    max_bytes=MAX_WIKI_BYTES,
                    etag=old.page.etag if old is not None else None,
                )
                if response.status_code == 304 and old is not None:
                    page = RemotePage(
                        source=old.page.source,
                        canonical_title=old.page.canonical_title,
                        language=old.page.language,
                        source_url=old.page.source_url,
                        fetched_at=current,
                        source_revision=old.page.source_revision,
                        etag=old.page.etag,
                        normalized_text=old.page.normalized_text,
                        raw_size_bytes=old.page.raw_size_bytes,
                        facts=old.page.facts,
                    )
                elif source == "huiji_wiki":
                    page = _huiji_page(response, current, title)
                else:
                    parsed = _mapping(json.loads(response.body))
                    page = (
                        _console_page(parsed, response, current)
                        if parsed is not None
                        else None
                    )
                if page is None:
                    warnings.append(f"{source}: page missing or ambiguous")
                    continue
                await self._cache_alias(title, page, current)
                return WikiLookupResult(page, False, tuple(warnings), False)
            except (FetchError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                warnings.append(f"{source}: {exc}")

        stale = next((cached[source].page for source, _url, _hosts in sources if source in cached), None)
        if stale is not None:
            return WikiLookupResult(stale, True, tuple(warnings), False)
        return WikiLookupResult(None, False, tuple(warnings), True)
