import json

import httpx
import pytest

from nanobot.games.ffxiv.fishcake import (
    FishCakeDetailSource,
    FishCakeFormatError,
    _decode_static_tables,
)
from nanobot.games.ffxiv.housing import (
    STAGE_TEXT,
    HousingMetadataSource,
    UnsupportedHousingSiteVersionError,
    extract_housing_metadata,
)
from nanobot.games.ffxiv.http import FetchError, FetchResponse, SafeHttpClient


def static_data(*, territory_variable="v", duplicate=False):
    tables = {
        "N": [{"id": 37, "name": {"chs": "碧海钓鱼之王"}}],
        "weatherNew": [{"id": 1, "chs": "碧空", "iconId": 60201}],
        "ratesNew": [{"id": 14, "weatherIds": [1], "rates": [100]}],
        territory_variable: [{"id": 128, "regionPlaceNameId": 22,
                              "zonePlaceNameId": 500, "placeNameId": 28,
                              "mapId": 11, "weatherRate": 14}],
    }
    if duplicate:
        tables["otherTerritory"] = tables[territory_variable]
    return ("const prefix=0" + "".join(
        f",{name}=JSON.parse({json.dumps(json.dumps(rows))})"
        for name, rows in tables.items()
    ) + ";").encode()


@pytest.mark.parametrize("variable", ["v", "renamedAgain"])
def test_fishcake_tables_survive_minifier_variable_changes(variable):
    territories, weather, rates = _decode_static_tables(
        static_data(territory_variable=variable), {22: "拉诺西亚", 28: "利姆萨·罗敏萨上层甲板"}
    )
    assert territories[0].territory_id == 128
    assert territories[0].place_name_zh == "利姆萨·罗敏萨上层甲板"
    assert weather[0].name_zh == "碧空"
    assert rates[0].rates == (100,)


def test_fishcake_ambiguous_table_is_rejected():
    with pytest.raises(FishCakeFormatError, match="ambiguous"):
        _decode_static_tables(static_data(duplicate=True), {})


def test_unrelated_object_table_does_not_break_fishing():
    data = static_data().rstrip(b";") + b',settings=JSON.parse(\'{"theme":"light"}\');'
    territories, _, _ = _decode_static_tables(data, {})
    assert territories[0].territory_id == 128


async def test_ffxiv_http_retries_one_transient_timeout(monkeypatch):
    import nanobot.games.ffxiv.http as module

    monkeypatch.setattr(module, "resolve_url_target", lambda _: (True, None, ["1.1.1.1"]))
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, content=b"[9347]")

    client = SafeHttpClient(timeout_seconds=5, inner_transport=httpx.MockTransport(handler))
    response = await client.get_bytes("https://universalis.app/api/v2/marketable",
                                      allowed_hosts=frozenset({"universalis.app"}))
    assert response.body == b"[9347]"
    assert len(calls) == 2


def test_housing_metadata_survives_variable_and_whitespace_changes():
    descriptions = [[f"房区 {area} 地块 {plot}" for plot in range(60)] for area in range(5)]
    script = f"const stages={json.dumps(list(STAGE_TEXT.values()))},renamed = {json.dumps(descriptions)};"
    metadata = extract_housing_metadata(script.encode(), version="new-build.js")
    assert metadata.descriptions[(4, 60)] == "房区 4 地块 59"


@pytest.mark.parametrize("spacing", ["", " "])
@pytest.mark.parametrize("oversized", [False, True])
async def test_fishing_discovers_moved_guide_from_current_manifest(monkeypatch, spacing, oversized):
    from types import SimpleNamespace

    import nanobot.games.ffxiv.fishcake as module

    async def discovery(_):
        return SimpleNamespace(source_revision="new", script=FetchResponse(
            "https://fish.ffmomola.com/assets/index-new.js", 200, {},
            b'const deps=["assets/FishDetailTips-new.js"];'))

    monkeypatch.setattr(module, "discover_current_assets", discovery)
    rows = [{"itemId": 8775, "author": "渔友", "bestCatchPath": "漂浮诱饵蛙", "tip": "强力提钩"}]
    guide = ('const header={title:"测试攻略",lastUpdate:"2026-9-2"},rows=JSON.parse(\''
             + json.dumps(rows) + "');")
    responses = {
        "FishDetailTips-new.js": b'const x=[{id:"tip99",lastUpdate:"2026-9-2",fishItemIds:[8775,1e4],load:()=>wrap(()=>import("./tip99-new.js"),[])}];',
        "tip99-new.js": guide.encode(),
    }
    responses["FishDetailTips-new.js"] = responses["FishDetailTips-new.js"].replace(
        b"fishItemIds:", ("fishItemIds:" + spacing).encode())
    if oversized:
        responses["FishDetailTips-new.js"] = responses["FishDetailTips-new.js"].replace(b"1e4", b"9" * 400)

    class Http:
        async def get_bytes(self, url, **kwargs):
            assert kwargs["allowed_hosts"] == module.FISHCAKE_HOSTS
            return FetchResponse(url, 200, {}, responses[url.rsplit("/", 1)[1]])

    if oversized:
        with pytest.raises(FishCakeFormatError, match="IDs are invalid"):
            await FishCakeDetailSource(Http()).guide_for(8775)
        return
    revision, result = await FishCakeDetailSource(Http()).guide_for(8775)
    assert revision == "new"
    assert result.author == "渔友"
    assert result.updated_at == "2026-09-02"


async def test_housing_discovers_renamed_script_and_refreshes_cache(monkeypatch):

    now = [0.0]
    import time
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    calls = []
    table = [["实测房源" for _ in range(60)] for _ in range(5)]
    script = f"const stages={json.dumps(list(STAGE_TEXT.values()))},renamed={json.dumps(table)};"

    class Http:
        async def get_bytes(self, url, **kwargs):
            calls.append(url)
            body = (b'<script type="module" src="/static/application-v2.js"></script>'
                    if url.endswith("/") else script.encode())
            return FetchResponse(url, 200, {}, body)

    source = HousingMetadataSource(Http())
    assert (await source.get()).version == "application-v2.js"
    await source.get()
    assert len(calls) == 2
    now[0] = 301
    await source.get()
    assert len(calls) == 4


def test_housing_changed_stage_meaning_is_not_guessed():
    table = [["房源" for _ in range(60)] for _ in range(5)]
    script = f"const changed={json.dumps(list(reversed(list(STAGE_TEXT.values()))))},rows={json.dumps(table)};"
    with pytest.raises(UnsupportedHousingSiteVersionError, match="stage"):
        extract_housing_metadata(script.encode(), version="bad.js")


@pytest.mark.parametrize("mode,expected_calls", [("timeout", 2), ("http_error", 1), ("blocked", 0)])
async def test_ffxiv_retry_is_bounded_and_does_not_bypass_guards(monkeypatch, mode, expected_calls):
    import nanobot.games.ffxiv.http as module

    monkeypatch.setattr(module, "resolve_url_target", lambda _: (mode != "blocked", "blocked", []))
    calls = []

    async def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(403)

    client = SafeHttpClient(timeout_seconds=5, inner_transport=httpx.MockTransport(handler))
    with pytest.raises(FetchError):
        await client.get_bytes("https://universalis.app/api/v2/marketable",
                               allowed_hosts=frozenset({"universalis.app"}))
    assert len(calls) == expected_calls
