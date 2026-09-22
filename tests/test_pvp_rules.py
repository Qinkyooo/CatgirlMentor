"""Tests for the data-driven PvP rotation snapshot and service.

These exercise the service layer directly (the layer that owns the clock), which
is the same convention the other FF14 game services use.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from nanobot.games.ffxiv import pvp_rules as rotation
from nanobot.games.ffxiv.http import FetchError, FetchResponse
from nanobot.games.ffxiv.pvp import PVPService

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "nanobot" / "games" / "ffxiv" / "data" / "pvp-rules.json"
SNAPSHOT_BYTES = SNAPSHOT.read_bytes()
SHANGHAI = ZoneInfo("Asia/Shanghai")
CC_ZONE = ZoneInfo("UTC")


def bundled() -> rotation.RotationRulesBundle:
    return rotation.parse_rotation_rules(SNAPSHOT_BYTES, origin=str(SNAPSHOT))


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
        self.assertEqual(rules.rotation_digest, rotation.rotation_digest(rules.modes))
        self.assertEqual(rules.verified_at, "2026-09-15")

    def test_tampered_snapshot_is_rejected(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        payload["rotation"]["frontline"]["order"] = ["seize", "secure"]
        with self.assertRaises(rotation.RotationRulesError) as caught:
            rotation.parse_rotation_rules(json.dumps(payload).encode(), origin="memory")
        self.assertEqual(caught.exception.code, "pvp_rules_digest_mismatch")

    def test_unsupported_schema_version_is_rejected(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        payload["schemaVersion"] = 99
        with self.assertRaises(rotation.RotationRulesError) as caught:
            rotation.parse_rotation_rules(json.dumps(payload).encode(), origin="memory")
        self.assertEqual(caught.exception.code, "pvp_rules_schema_unsupported")

    def test_missing_map_name_is_rejected(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        del payload["maps"]["seize"]
        with self.assertRaises(rotation.RotationRulesError) as caught:
            rotation.parse_rotation_rules(json.dumps(payload).encode(), origin="memory")
        self.assertEqual(caught.exception.code, "pvp_rules_invalid")

    def test_digest_ignores_display_metadata(self):
        """A translation tweak upstream must not look like a schedule change."""
        rules = bundled()
        other = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        other["maps"]["seize"]["name"] = "renamed"
        other["verifiedAt"] = "2027-01-01"
        self.assertEqual(
            rotation.rotation_digest(rules.modes),
            rotation.rotation_digest(rotation.parse_rotation_rules(
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
    def test_partial_remote_rules_do_not_replace_supported_modes(self):
        payload = json.loads(SNAPSHOT_BYTES)
        del payload["rotation"]["cc"]
        payload["rotationDigest"] = rotation.rotation_digest({"frontline": bundled().modes["frontline"]})
        svc = service(http=FakeHttp(body=json.dumps(payload).encode()),
                      rules_url="https://example.invalid/rules.json")
        result = json.loads(str(run(svc.execute(action="current"))))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pvp_rules_invalid")
        self.assertIsNone(svc._cache)

    def test_matching_upstream_keeps_answering(self):
        http = FakeHttp(body=SNAPSHOT_BYTES)
        svc = service(http=http, rules_url="https://example.invalid/rules.json")
        result = json.loads(str(run(svc.execute(action="current"))))
        self.assertTrue(result["ok"], result)
        self.assertEqual(http.calls, 1)

    def test_valid_upstream_update_is_adopted(self):
        payload = json.loads(SNAPSHOT_BYTES.decode("utf-8"))
        payload["rotation"]["frontline"]["order"] = ["seize", "secure", "naadam"]
        payload["rotationDigest"] = rotation.rotation_digest(
            {
                "frontline": rotation.RotationModeRules(
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
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["ruleDigest"], payload["rotationDigest"])
        expected = service(rules=rotation.parse_rotation_rules(json.dumps(payload).encode(), origin="test"))
        expected_result = json.loads(str(run(expected.execute(action="current"))))
        self.assertEqual(result["data"]["current"], expected_result["data"]["current"])
        # A later network failure must not roll the accepted update back.
        svc._cache_seconds = 0
        http._error = FetchError("offline")
        fallback = json.loads(str(run(svc.execute(action="current"))))
        self.assertEqual(fallback["data"]["ruleDigest"], payload["rotationDigest"])
        self.assertTrue(fallback["freshness"]["stale"])

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
        source = (REPO / "nanobot" / "games" / "ffxiv" / "pvp.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("REVIEW_AFTER", source)
        self.assertNotIn("reviewAfter", source)

    def test_tool_source_has_no_expiry_gate(self):
        source = (REPO / "nanobot" / "agent" / "tools" / "ffxiv_pvp.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("REVIEW_AFTER", source)
        self.assertNotIn("reviewAfter", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
