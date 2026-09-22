"""Compatible upstream updates must not require a new application build."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from nanobot.games.ffxiv.http import FetchResponse
from nanobot.games.ffxiv.market import MarketService
from nanobot.games.ffxiv.wiki import FFCafeClient

SCHEMA = "exdschema@2:rev:e773c41a90aed788cf4c1c48469fa85618ef01fb"
VERSION = "2026090100000000"


class UpdatedItems:
    def __init__(self):
        self.bad_search = None
        self.bad_payload = None
        self.price_calls = 0

    async def get_bytes(self, url, **kwargs):
        query = parse_qs(urlsplit(url).query)
        if "/search?" in url:
            sheet = query["sheets"][0]
            fields = {
                "Item": {"Name": "猫小胖", "LevelItem": {"row_id": 1}},
                "Recipe": {"ItemResult@as(raw)": 9347, "AmountResult": 1,
                           "CraftType": {"fields": {"Name": "雕金匠"}},
                           "RecipeLevelTable": {"fields": {"ClassJobLevel": 50}}},
                "GatheringItem": {"Item@as(raw)": 9347,
                                  "GatheringItemLevel": {"fields": {"GatheringItemLevel": 50}}},
            }[sheet]
            payload = {"schema": SCHEMA, "version": VERSION, "newMetadata": True,
                       "results": [{"sheet": sheet, "row_id": 9347, "fields": fields}]}
            if sheet == "Item" and self.bad_search:
                self.bad_search(payload)
        elif "/sheet/Item/" in url:
            payload = {"schema": SCHEMA, "version": VERSION, "row_id": 9347,
                       "fields": {"Name": "猫小胖", "Description": "宠物",
                                  "ItemUICategory": {"fields": {"Name": "宠物"}}}}
        elif url.endswith("/marketable"):
            payload = [9347]
        else:
            self.price_calls += 1
            payload = {"results": [{"itemId": 9347, "nq": {
                "minListing": {"dc": {"price": 123456}},
                "averageSalePrice": {"dc": {"price": 197169}},
            }}]}
        if self.bad_payload:
            self.bad_payload(url, payload)
        return FetchResponse(url, 200, {}, json.dumps(payload).encode())


async def test_market_accepts_new_schema_and_reports_actual_data_version():
    http = UpdatedItems()
    service = MarketService(http=http, items=FFCafeClient(http=http))
    result = json.loads(await service.execute(action="price", item_name="猫小胖", scope="陆行鸟区"))
    assert result["ok"], result
    assert result["data"]["item"]["rowId"] == 9347
    assert result["data"]["item"]["dataVersion"] == VERSION
    assert result["data"]["minPrice"] == 123456


async def test_item_facts_recipe_and_gathering_accept_compatible_updates():
    fact = await FFCafeClient(http=UpdatedItems()).game_fact(9347)
    assert fact.name_zh == "猫小胖"
    assert fact.schema == SCHEMA
    assert fact.data_version == VERSION
    assert [method.kind for method in fact.acquisition_methods] == ["craft", "gather"]


@pytest.mark.parametrize("corrupt", [
    lambda p: p.pop("results"),
    lambda p: p.update(results={}),
    lambda p: p["results"][0].update(row_id=True),
    lambda p: p["results"][0].update(row_id=-1),
    lambda p: p["results"][0].update(sheet="Quest"),
    lambda p: p["results"][0]["fields"].update(Name=123),
    lambda p: p["results"][0]["fields"].update(LevelItem={"row_id": "bad"}),
])
async def test_invalid_item_contract_never_becomes_a_price_or_not_found(corrupt):
    http = UpdatedItems()
    http.bad_search = corrupt
    service = MarketService(http=http, items=FFCafeClient(http=http))
    result = json.loads(await service.execute(action="price", item_name="猫小胖", scope="陆行鸟区"))
    assert not result["ok"]
    assert result["error"]["code"] == "source_format_changed"
    assert "稍后重试" not in result["error"]["message"]
    assert http.price_calls == 0


async def test_valid_empty_search_is_not_a_format_error():
    http = UpdatedItems()
    http.bad_search = lambda p: p.update(results=[])
    assert await FFCafeClient(http=http).search_items("不存在") == ()


@pytest.mark.parametrize("sheet,field,value", [
    ("Recipe", "AmountResult", -1),
    ("Recipe", "AmountResult", "one"),
    ("Recipe", "CraftType", {"fields": {"Name": 123}}),
    ("Recipe", "RecipeLevelTable", {"fields": {"ClassJobLevel": -1}}),
    ("GatheringItem", "GatheringItemLevel", {"fields": {"GatheringItemLevel": "high"}}),
    ("GatheringItem", "GatheringItemLevel", {"fields": {"GatheringItemLevel": -5}}),
])
async def test_changed_acquisition_field_types_are_not_silently_used(sheet, field, value):
    from nanobot.games.ffxiv.wiki import WikiFormatError

    http = UpdatedItems()

    def corrupt(url, payload):
        if parse_qs(urlsplit(url).query).get("sheets") == [sheet]:
            payload["results"][0]["fields"][field] = value

    http.bad_payload = corrupt
    with pytest.raises(WikiFormatError):
        await FFCafeClient(http=http).game_fact(9347)


async def test_fishing_refreshes_changed_data_even_without_a_version_bump(tmp_path, monkeypatch):
    import nanobot.games.ffxiv.fishing_snapshot as module

    now = [datetime(2026, 9, 21, tzinfo=UTC)]
    value = [1]
    roles = tuple(module._ROLE_FILENAMES)

    class Http:
        async def get_bytes(self, url, **kwargs):
            return FetchResponse(url, 200, {}, str(value[0]).encode())

    async def discover(_):
        return SimpleNamespace(source_revision="same-version",
                               home=FetchResponse("https://fish.ffmomola.com/", 200, {}, b"home"),
                               script=FetchResponse("https://fish.ffmomola.com/app.js", 200, {}, b"app"),
                               assets=tuple(SimpleNamespace(role=r, url=f"https://fish.ffmomola.com/{r}") for r in roles))

    def normalize(**kwargs):
        if value[0] == 3:
            from nanobot.games.ffxiv.fishcake import FishCakeFormatError
            raise FishCakeFormatError("changed field meaning")
        return SimpleNamespace(drift_report=SimpleNamespace(warnings=()), value=value[0])

    monkeypatch.setattr(module, "discover_current_assets", discover)
    monkeypatch.setattr(module, "normalize_fishcake_assets", normalize)
    monkeypatch.setattr(module, "serialize_fishing_snapshot", lambda s: json.dumps({"value": s.value}))
    manager = module.FishingSnapshotManager(data_dir=tmp_path, client=Http(), ttl=timedelta(minutes=5),
                                            clock=lambda: now[0], baseline=None)
    first = await manager.refresh()
    value[0] = 2
    now[0] += timedelta(minutes=6)
    second = await manager.refresh()
    assert json.loads(second.data_path.read_text())["value"] == 2
    assert first.data_path != second.data_path
    assert json.loads(first.data_path.read_text())["value"] == 1
    value[0] = 3
    now[0] += timedelta(minutes=6)
    stale = await manager.refresh()
    assert stale.state == "stale"
    assert stale.data_path == second.data_path
    assert stale.warnings
    assert json.loads(manager.current_path.read_text())["sourceRevision"] == second.source_revision


def test_directory_survives_css_class_rename():
    from nanobot.games.ffxiv.tool_directory import parse_directory_html

    entries = parse_directory_html('<h4>钓鱼</h4><a class="new-card" href="https://fish.ffmomola.com/">'
                                   '<strong>鱼糕</strong><p>钓鱼时钟与攻略</p></a>'.encode())
    assert len(entries) == 1
    assert entries[0].name == "鱼糕"


async def test_fishing_discovers_current_app_without_hashed_filename_or_version_tag():
    from nanobot.games.ffxiv.fishcake import discover_current_assets

    assets = ["data-json-new.js", "fishingSpot-new.bin", "fishBaitAndMooch-new.bin",
              "normalFish-new.bin", "item-new.bin", "itemNameCHS-new.bin", "placeNameCHS-new.bin"]
    bodies = {
        "/": b'<script type="module" src="/assets/metrics.js"></script><script type="module" src="/assets/app.js"></script>',
        "/assets/metrics.js": b"export {};",
        "/assets/app.js": json.dumps(assets).encode(),
    }

    class Http:
        async def get_bytes(self, url, **kwargs):
            return FetchResponse(url, 200, {}, bodies[urlsplit(url).path])

    result = await discover_current_assets(Http())
    assert result.script.url.endswith("/app.js")
    assert len(result.assets) == 7
    assert result.source_revision


async def test_wiki_refreshes_changed_content_after_daily_ttl(tmp_path):
    from nanobot.games.ffxiv.wiki import WikiLookup
    from nanobot.games.ffxiv.wiki_cache import WikiCache

    now = datetime(2026, 9, 21, tzinfo=UTC)
    cache = WikiCache(tmp_path / "wiki.sqlite3", max_bytes=1000000)
    text = ["旧版攻略内容" * 50]

    class Http:
        async def get_bytes(self, url, **kwargs):
            body = f'<html><title>测试攻略</title><main><p>{text[0]}</p></main></html>'.encode()
            return FetchResponse(url, 200, {}, body)

    wiki = WikiLookup(http=Http(), cache=cache)
    before = await wiki.lookup("测试攻略", now=now)
    assert before.page is not None
    text[0] = "更新后的攻略内容" * 50
    after = await wiki.lookup("测试攻略", now=now + timedelta(days=1, seconds=1))
    assert after.page is not None
    assert "更新后" in after.page.normalized_text
