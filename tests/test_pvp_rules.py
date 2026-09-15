"""Tests for the data-driven PvP rotation snapshot and service.

These exercise the service layer directly (the layer that owns the clock), which
is the same convention the other FF14 game services use.

The rotation logic deliberately sits below the pydantic-backed tool layer, so it
is loaded here through a small import shim: only ``base.ToolResult`` and the
bounded-HTTP client type are stood in for, and ``pvp.py`` / ``pvp_rules.py`` are
executed from their real source files.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from zoneinfo import ZoneInfo

def _repo_root() -> Path:
    """Locate the checkout that contains this test, regardless of nesting."""
    for candidate in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        if (candidate / "nanobot" / "games" / "ffxiv" / "pvp.py").is_file():
            return candidate
    raise RuntimeError("找不到包含 nanobot/games/ffxiv/pvp.py 的仓库根目录")


DEV = _repo_root()
SNAPSHOT = DEV / "nanobot" / "games" / "ffxiv" / "data" / "pvp-rules.json"
_FFXIV = DEV / "nanobot" / "games" / "ffxiv"
_MODULE_FILES = {
    "types": _FFXIV / "types.py",
    "result": _FFXIV / "result.py",
    "pvp_rules": _FFXIV / "pvp_rules.py",
    "pvp": _FFXIV / "pvp.py",
}

# --- Minimal package wiring so the module can be imported without pydantic ---
_nanobot = types.ModuleType("nanobot")
_nanobot.__path__ = []
sys.modules.setdefault("nanobot", _nanobot)
_pkg = types.ModuleType("nanobot.games.ffxiv")
_pkg.__path__ = [str(DEV)]
sys.modules.setdefault("nanobot.games", types.ModuleType("nanobot.games"))
sys.modules.setdefault("nanobot.games.ffxiv", _pkg)

_base = types.ModuleType("nanobot.agent.tools.base")


class _ToolResult(str):
    @classmethod
    def error(cls, content: str) -> "_ToolResult":
        return cls(content)


_base.ToolResult = _ToolResult
sys.modules.setdefault("nanobot.agent", types.ModuleType("nanobot.agent"))
sys.modules.setdefault("nanobot.agent.tools", types.ModuleType("nanobot.agent.tools"))
sys.modules.setdefault("nanobot.agent.tools.base", _base)

# httpx and the SSRF guard are not needed to exercise the rotation logic.
_http_mod = types.ModuleType("nanobot.games.ffxiv.http")


class FetchError(RuntimeError):
    """Stand-in for the bounded-fetch failure raised by the real client."""


class SafeHttpClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


@dataclasses.dataclass(frozen=True)
class FetchResponse:
    url: str
    status_code: int
    headers: dict
    body: bytes


_http_mod.FetchError = FetchError
_http_mod.SafeHttpClient = SafeHttpClient
_http_mod.FetchResponse = FetchResponse
sys.modules["nanobot.games.ffxiv.http"] = _http_mod

for mod, path in _MODULE_FILES.items():
    spec_name = f"nanobot.games.ffxiv.{mod}"
    module = types.ModuleType(spec_name)
    module.__file__ = str(path)
    module.__package__ = "nanobot.games.ffxiv"
    sys.modules[spec_name] = module
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)

from nanobot.games.ffxiv import pvp_rules as R  # noqa: E402
from nanobot.games.ffxiv.pvp import PVPService  # noqa: E402

SNAPSHOT_BYTES = SNAPSHOT.read_bytes()
SHANGHAI = ZoneInfo("Asia/Shanghai")
CC_ZONE = ZoneInfo("UTC")


def bundled() -> R.RotationRulesBundle:
    return R.parse_rotation_rules(SNAPSHOT_BYTES, origin=str(SNAPSHOT))


class FakeHttp:
    """Returns a canned snapshot body, or raises to simulate a network failure."""

    def __init__(self, body: bytes | None = None, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.calls = 0

    async def get_bytes(self, url, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return FetchResponse(url=url, status_code=200, headers={}, body=self._body or b"")


def service(
    *,
    http=None,
    rules_url=None,
    rules=None,
    now: datetime | None = None,
) -> PVPService:
    fixed = now or datetime(2026, 9, 15, 14, 10, tzinfo=UTC)
    return PVPService(
        rules=rules or bundled(),
        http=http,
        rules_url=rules_url,
        clock=lambda: fixed,
    )


def run(coro):
    return asyncio.run(coro)


def query(**kwargs):
    result = run(service().execute(**kwargs))
    return json.loads(str(result))


class SnapshotTest(unittest.TestCase):
    def test_snapshot_parses_and_digest_is_self_consistent(self):
        rules = bundled()
        self.assertEqual(rules.schema_version, 1)
        self.assertEqual(rules.rotation_digest, R.rotation_digest(rules.modes))
        self.assertEqual(rules.verified_at, "2026-09-15")

    def test_tampered_snapshot_is_rejected(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        payload["rotation"]["frontline"]["order"] = ["seize", "secure"]
        with self.assertRaises(R.RotationRulesError) as caught:
            R.parse_rotation_rules(json.dumps(payload).encode(), origin="memory")
        self.assertEqual(caught.exception.code, "pvp_rules_digest_mismatch")

    def test_unsupported_schema_version_is_rejected(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        payload["schemaVersion"] = 99
        with self.assertRaises(R.RotationRulesError) as caught:
            R.parse_rotation_rules(json.dumps(payload).encode(), origin="memory")
        self.assertEqual(caught.exception.code, "pvp_rules_schema_unsupported")

    def test_missing_map_name_is_rejected(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        del payload["maps"]["seize"]
        with self.assertRaises(R.RotationRulesError) as caught:
            R.parse_rotation_rules(json.dumps(payload).encode(), origin="memory")
        self.assertEqual(caught.exception.code, "pvp_rules_invalid")

    def test_digest_ignores_display_metadata(self):
        """A translation tweak upstream must not look like a schedule change."""
        rules = bundled()
        other = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        other["maps"]["seize"]["name"] = "renamed"
        other["verifiedAt"] = "2027-01-01"
        self.assertEqual(
            R.rotation_digest(rules.modes),
            R.rotation_digest(R.parse_rotation_rules(
                json.dumps(other).encode(), origin="memory").modes),
        )


class RotationMathTest(unittest.TestCase):
    def test_frontline_before_and_at_reset(self):
        before = query(action="current", at="2026-09-15T22:59:59+08:00")
        after = query(action="current", at="2026-09-15T23:00:00+08:00")
        self.assertTrue(before["ok"], before)
        self.assertEqual(before["data"]["current"]["mapId"], "seize")
        self.assertEqual(before["data"]["current"]["remainingSeconds"], 1)
        self.assertEqual(after["data"]["current"]["mapId"], "shatter")
        self.assertEqual(after["data"]["current"]["remainingSeconds"], 86400)

    def test_calendar_splits_midnight_day_at_reset(self):
        result = query(action="calendar", date="2026-09-15", days=1)
        entries = result["data"]["entries"]
        self.assertEqual([row["mapId"] for row in entries], ["seize", "shatter"])
        self.assertEqual(entries[-1]["end"], "2026-09-16T00:00:00+08:00")

    def test_cc_reference_and_hour_boundary(self):
        self.assertEqual(
            query(action="current", mode="cc", at="2026-04-28T13:00:00Z")["data"]["current"]["mapId"],
            "palaistra",
        )
        self.assertEqual(
            query(action="current", mode="cc", at="2026-04-28T14:00:00Z")["data"]["current"]["mapId"],
            "volcanic",
        )

    def test_cc_timeline_has_no_gaps(self):
        rows = query(action="timeline", mode="cc", hours=3)["data"]["entries"]
        self.assertEqual(len(rows), 4)
        for left, right in pairwise(rows):
            self.assertEqual(left["end"], right["start"])

    def test_far_future_is_answered_not_refused(self):
        """No build-time expiry: the formula keeps working without a code change."""
        result = query(action="current", at="2030-06-01T00:00:00Z")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("reviewAfter", json.dumps(result))

    def test_far_past_is_answered(self):
        result = query(action="current", at="2020-01-01T00:00:00Z")
        self.assertTrue(result["ok"], result)

    def test_result_carries_provenance_and_warnings(self):
        result = query(action="current")
        self.assertEqual(result["data"]["ruleVerifiedAt"], "2026-09-15")
        self.assertTrue(result["evidence"][0]["sourceUrl"])
        self.assertTrue(result["warnings"])
        self.assertFalse(result["data"]["regionParityVerified"])

    def test_no_self_doubt_disclaimers(self):
        """Warnings stay about the prediction, not about what the tool cannot know."""
        result = query(action="current")
        for fragment in ("维护", "未单独核实", "未核实"):
            self.assertFalse(
                any(fragment in warning for warning in result["warnings"]),
                fragment,
            )


class InputValidationTest(unittest.TestCase):
    def test_naive_datetime_is_rejected(self):
        result = query(action="current", at="2026-09-15T12:00:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_pvp_query")

    def test_bad_timezone_is_rejected(self):
        result = query(action="current", timezone="bad/zone")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_pvp_query")

    def test_bad_date_is_rejected(self):
        result = query(action="calendar", date="not-a-date")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_pvp_query")

    def test_unknown_mode_is_rejected(self):
        result = query(action="current", mode="unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pvp_rules_invalid")


class RemoteRuleTest(unittest.TestCase):
    def test_matching_upstream_keeps_answering(self):
        http = FakeHttp(body=SNAPSHOT_BYTES)
        svc = service(http=http, rules_url="https://example.invalid/rules.json")
        result = json.loads(str(run(svc.execute(action="current"))))
        self.assertTrue(result["ok"], result)
        self.assertEqual(http.calls, 1)

    def test_drifted_upstream_refuses_to_guess(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        payload["rotation"]["frontline"]["order"] = ["seize", "secure", "naadam"]
        payload["rotationDigest"] = R.rotation_digest(
            {
                "frontline": R.RotationModeRules(
                    reference=datetime(2026, 4, 27, 15, tzinfo=UTC),
                    interval=timedelta(days=1),
                    order=("seize", "secure", "naadam"),
                ),
                "cc": bundled().modes["cc"],
            }
        )
        http = FakeHttp(body=json.dumps(payload).encode())
        svc = service(http=http, rules_url="https://example.invalid/rules.json")
        result = json.loads(str(run(svc.execute(action="current"))))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pvp_rules_outdated")
        self.assertTrue(result["suggestions"])

    def test_unreachable_upstream_degrades_with_warning(self):
        http = FakeHttp(error=FetchError("boom"))
        svc = service(http=http, rules_url="https://example.invalid/rules.json")
        result = json.loads(str(run(svc.execute(action="current"))))
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("无法校验" in w for w in result["warnings"]))

    def test_verified_result_is_cached(self):
        http = FakeHttp(body=SNAPSHOT_BYTES)
        svc = service(http=http, rules_url="https://example.invalid/rules.json")
        run(svc.execute(action="current"))
        run(svc.execute(action="current"))
        self.assertEqual(http.calls, 1)

    def test_invalid_upstream_schema_is_reported(self):
        http = FakeHttp(body=b'{"schemaVersion": 42}')
        svc = service(http=http, rules_url="https://example.invalid/rules.json")
        result = json.loads(str(run(svc.execute(action="current"))))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pvp_rules_schema_unsupported")


class NoBuildTimeExpiryTest(unittest.TestCase):
    def test_service_source_has_no_expiry_gate(self):
        source = _MODULE_FILES["pvp"].read_text(encoding="utf-8")
        self.assertNotIn("REVIEW_AFTER", source)
        self.assertNotIn("reviewAfter", source)

    def test_tool_source_has_no_expiry_gate(self):
        source = (DEV / "nanobot" / "agent" / "tools" / "ffxiv_pvp.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("REVIEW_AFTER", source)
        self.assertNotIn("reviewAfter", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
