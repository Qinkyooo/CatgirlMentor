"""Check FF14 resources in this Python runtime; --live checks public data sources.

No model credentials, LLM calls, or existing user configuration are used.
Run with the packaged runtime/python.exe as well as the source environment.
"""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

from nanobot.agent.tools.games import _build_services, _ServiceSettings
from nanobot.games.ffxiv.housing import HOUSING_DISCLAIMER, VACANCY_STAGES
from nanobot.games.ffxiv.knowledge_assets import resolve_guide_database


async def smoke(live: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="CatgirlMentor-FF14-") as folder:
        root = Path(folder)
        guide = resolve_guide_database(None, data_root=root)
        services = _build_services(_ServiceSettings(
            data_root=root, update_timeout_seconds=5, guide_database=guide.path,
            guide_database_mode=guide.mode, wiki_cache_mb=16, timezone_name="Asia/Shanghai",
        ))
        assert guide.path.is_file()
        print("PASS: packaged FF14 services, server table, guide database and timezone", flush=True)
        if not live:
            return

        async def query(service, **args):
            result = json.loads(await service.execute(**args))
            assert result.get("ok"), (args, result)
            print(f"PASS: {args}", flush=True)
            return result

        for fish in ("红龙", "波太郎"):
            result = await query(services.fishing, action="fish_info", fish_name=fish)
            assert result["data"]["facts"]["name"] == fish
            assert result["data"]["guideSummary"], "Current player guide was not recovered"
        missing = json.loads(await services.fishing.execute(action="fish_info", fish_name="不存在的测试鱼"))
        assert missing["error"]["code"] == "fish_not_found", missing
        print("PASS: unknown fish returns a structured error", flush=True)
        await query(services.fishing, action="fish_windows", fish_name="红龙")
        await query(services.fishing, action="weather", zone="森都")
        market = await query(services.market, action="price", item_name="猫小胖", scope="陆行鸟区")
        assert market["data"]["item"]["rowId"] == 9347
        assert market["data"]["item"]["dataVersion"]
        item = await query(services.knowledge, action="item", query="猫小胖")
        assert item["kind"] == "knowledge_item", "FFCafe failed and fell back to a wiki"
        assert item["data"]["itemId"] == 9347
        await query(services.knowledge, action="guide", query="龙骑士循环")
        await query(services.knowledge, action="guide", query="钓鱼", tool_site=True)
        await query(services.pvp, action="current")
        vacancies = await query(services.housing, action="vacancies", server="红玉海", size="S")
        assert HOUSING_DISCLAIMER in vacancies["warnings"]
        cards = vacancies["data"]["cards"]
        assert all(card["stage"] in VACANCY_STAGES and card["size"] == "S" for card in cards)
        if cards:
            match = re.fullmatch(r"(.+) (\d+) 区 (\d+) 号", cards[0]["location"])
            assert match
            detail = await query(services.housing, action="detail", server="红玉海",
                                 area=match[1], ward=int(match[2]), plot=int(match[3]))
            assert detail["data"]["cards"][0]["location"] == cards[0]["location"]
        await query(services.housing, action="recommend", server="红玉海", size="M")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Contact public FF14 sources")
    asyncio.run(smoke(parser.parse_args().live))
