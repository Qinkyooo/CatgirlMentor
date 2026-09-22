from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from nanobot.games.ffxiv import fishing
from nanobot.games.ffxiv.fishing_types import Fish


def _fish(fish_id: int, name: str) -> Fish:
    return Fish(
        fish_parameter_id=fish_id,
        fish_id=fish_id,
        name_zh=name,
        zone_id=1,
        spot_id=1,
        spot_ids=(1,),
        start_hour=None,
        end_hour=None,
        weather_ids=(),
        previous_weather_ids=(),
        predators=(),
        intuition_length=None,
        rarity="normal",
        bait_names=(),
        tug="none",
        hookset="none",
    )


def test_qicai_tianzhu_uses_green_prismfish_windows_from_eorzea_day_start(monkeypatch):
    target = _fish(24994, "七彩天主")
    green = _fish(24204, "翠绿棱晶鱼")
    start = datetime(2026, 9, 22, 16, tzinfo=UTC)
    end = start + timedelta(hours=2)
    green_window = fishing.TimeWindow(start + timedelta(minutes=10), start + timedelta(hours=1))

    class Repository:
        spots = ()
        territories = ()

        def resolve_fish(self, fish_id):
            assert fish_id == 24204
            return SimpleNamespace(fish=green)

    calls = []

    def fake_fish_windows(fish, *, start, end, weather_rate):
        calls.append((fish.fish_id, start, end))
        assert fish.fish_id == 24204
        return (fishing.MatchedFishingWindow(green_window, fishing.ConditionEvidence(False, None, None)),)

    monkeypatch.setattr(fishing, "fish_windows", fake_fish_windows)
    data = fishing._ServiceData(Repository(), {}, {})

    result = fishing._window_rows(data, target, start, end)

    expected_start = datetime.fromtimestamp(
        (start.timestamp() // fishing.EORZEAN_DAY_SECONDS) * fishing.EORZEAN_DAY_SECONDS,
        tz=UTC,
    )
    assert result == (fishing.TimeWindow(expected_start, green_window.end),)
    assert calls == [(24204, start, end + timedelta(seconds=fishing.EORZEAN_DAY_SECONDS))]
