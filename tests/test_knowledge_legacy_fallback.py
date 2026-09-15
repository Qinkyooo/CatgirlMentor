"""Tests for the legacy-fallback knowledge database resolution path.

`tests/test_knowledge_deployment.py` covers the case where the bundled database
itself is missing. This module covers the other half: a configured/pre-existing
legacy database path that no longer exists, where the service must point at the
real in-package location instead of a stale build directory.

The rotation/tool layer imports pydantic, whose compiled extension targets an
interpreter that is not installed in this checkout, so only the
`nanobot.agent.tools.base.ToolResult` leaf is stood in for. `knowledge.py` and
`knowledge_assets.py` are the real production modules.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class _ToolResult(str):
    @classmethod
    def error(cls, content: str) -> "_ToolResult":
        return cls(content)


_base = types.ModuleType("nanobot.agent.tools.base")
_base.ToolResult = _ToolResult
for _name in ("nanobot.agent", "nanobot.agent.tools"):
    _module = types.ModuleType(_name)
    _module.__path__ = [str(REPO / Path(*_name.split(".")))]
    sys.modules.setdefault(_name, _module)
sys.modules["nanobot.agent.tools.base"] = _base

from nanobot.games.ffxiv import knowledge_assets as assets  # noqa: E402
from nanobot.games.ffxiv.knowledge import KnowledgeService  # noqa: E402


def _service(database: Path, mode: str) -> KnowledgeService:
    return KnowledgeService(
        source_root=None,
        guide_database=database,
        guide_database_mode=mode,  # type: ignore[arg-type]
        ffcafe=None,
        wiki=None,
        directory=None,
    )


class LegacyFallbackTest(unittest.TestCase):
    def test_missing_legacy_path_falls_back_to_bundled(self):
        with tempfile.TemporaryDirectory() as root:
            resolved = assets.resolve_guide_database(None, data_root=Path(root))
            self.assertEqual(resolved.mode, "bundled")
            self.assertEqual(resolved.path, assets.bundled_database_path())
            self.assertTrue(resolved.path.is_file())

    def test_existing_legacy_path_still_wins(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / "ffxiv" / "knowledge" / assets.BUNDLED_DATABASE_NAME
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            resolved = assets.resolve_guide_database(None, data_root=Path(root))
            self.assertEqual(resolved.mode, "legacy")
            self.assertEqual(resolved.path, legacy)

    def test_missing_legacy_reports_no_build_hint(self):
        """The failure must name the real location, not a stale build directory."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            missing = root_path / "ffxiv" / "knowledge" / "guide.sqlite3"
            service = _service(missing, "legacy")
            payload = json.loads(str(service._database_error()))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "knowledge_database_missing")
            message = payload["error"]["message"]
            self.assertIn("guide.sqlite3", message)
            self.assertIn(str(assets.bundled_database_path()), message)
            self.assertNotIn("obsolete-materials", message)
            self.assertNotIn("knowledge_build", message)
            self.assertNotIn("knowledge_build_required", json.dumps(payload))

    def test_missing_legacy_offers_actionable_suggestions(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "ffxiv" / "knowledge" / "guide.sqlite3"
            payload = json.loads(str(_service(missing, "legacy")._database_error()))
            self.assertTrue(payload["suggestions"])
            self.assertTrue(any("bundled" in item for item in payload["suggestions"]))

    def test_bundled_mode_success_path_has_no_error(self):
        service = _service(assets.bundled_database_path(), "bundled")
        self.assertIsNone(service._database_error())

    def test_bundled_mode_missing_points_at_real_location(self):
        with tempfile.TemporaryDirectory() as root:
            moved = Path(root) / "moved" / "nanobot" / "games" / "ffxiv" / "data"
            with patch.object(assets, "bundled_data_dir", return_value=moved):
                service = _service(moved / "guide.sqlite3", "bundled")
                payload = json.loads(str(service._database_error()))
            self.assertEqual(payload["error"]["code"], "bundled_database_missing")
            self.assertIn("guide.sqlite3", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
