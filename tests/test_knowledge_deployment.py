import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.games.ffxiv import knowledge_assets as assets
from nanobot.games.ffxiv.knowledge import KnowledgeService


class KnowledgeDeploymentTest(unittest.TestCase):
    def test_missing_legacy_uses_bundled_and_searches(self):
        with tempfile.TemporaryDirectory() as root:
            resolved = assets.resolve_guide_database(None, data_root=Path(root))
            self.assertEqual(resolved.mode, "bundled")
            self.assertEqual(resolved.path, Path(assets.__file__).parent / "data/guide.sqlite3")
            service = KnowledgeService(source_root=Path(root), guide_database=resolved.path,
                                       guide_database_mode=resolved.mode,
                                       ffcafe=None, wiki=None, directory=None)
            result = json.loads(asyncio.run(service.execute(action="guide", query="白魔法师")))
            self.assertTrue(result["ok"], result)

    def test_existing_legacy_and_explicit_custom_keep_priority(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            legacy = root / "ffxiv/knowledge/guide.sqlite3"
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            self.assertEqual(assets.resolve_guide_database(None, data_root=root).path, legacy)
            self.assertEqual(assets.resolve_guide_database(None, data_root=root).mode, "legacy")
            custom = root / "custom.sqlite3"
            resolved = assets.resolve_guide_database(str(custom), data_root=root)
            self.assertEqual(resolved.path, custom)
            self.assertEqual(resolved.mode, "custom")

    def test_missing_bundled_reports_deployment_path(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with patch.object(assets, "bundled_data_dir", return_value=root / "moved/nanobot/games/ffxiv/data"):
                resolved = assets.resolve_guide_database(None, data_root=root)
                service = KnowledgeService(source_root=root / "obsolete-materials",
                                           guide_database=resolved.path,
                                           guide_database_mode=resolved.mode,
                                           ffcafe=None, wiki=None, directory=None)
                result = str(service._database_error())
                self.assertIn("bundled_database_missing", result)
                self.assertIn("guide.sqlite3", result)
                self.assertNotIn("obsolete-materials", result)
                self.assertNotIn("knowledge_build", result)


if __name__ == "__main__":
    unittest.main()
