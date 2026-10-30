"""Tests for ``octop/tools/install_skills.py`` (workspace skill install).

The script writes into a user workspace, so the three properties that matter are
covered explicitly: content is byte-identical to the vendored copy, existing
directories are never overwritten, and ``--remove`` only deletes what we added.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
VENDOR_SKILLS = PLUGIN_DIR / "_vendor" / "ponytail" / "skills"


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location(
        "install_skills", PLUGIN_DIR / "install_skills.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def test_installs_six_skills_byte_identical(self) -> None:
        installed, skipped = self.script.install(self.ws)
        self.assertEqual(len(installed), 6, installed)
        self.assertEqual(skipped, [])
        for name in installed:
            copied = (self.ws / "skills" / name / "SKILL.md").read_bytes()
            original = (VENDOR_SKILLS / name / "SKILL.md").read_bytes()
            self.assertEqual(copied, original, name)
            self.assertTrue((self.ws / "skills" / name / ".ponytail-skill").is_file())

    def test_never_overwrites_an_existing_skill(self) -> None:
        mine = self.ws / "skills" / "ponytail-review"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("user custom", encoding="utf-8")

        installed, skipped = self.script.install(self.ws)
        self.assertIn("ponytail-review", skipped)
        self.assertNotIn("ponytail-review", installed)
        self.assertEqual((mine / "SKILL.md").read_text(encoding="utf-8"), "user custom")

    def test_second_run_is_a_no_op(self) -> None:
        self.script.install(self.ws)
        installed, skipped = self.script.install(self.ws)
        self.assertEqual(installed, [])
        self.assertEqual(len(skipped), 6)

    def test_remove_deletes_only_our_directories(self) -> None:
        mine = self.ws / "skills" / "ponytail-review"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("user custom", encoding="utf-8")
        self.script.install(self.ws)

        removed = self.script.remove(self.ws)
        self.assertEqual(len(removed), 5, removed)
        self.assertFalse((self.ws / "skills" / "ponytail-help").exists())
        self.assertTrue((mine / "SKILL.md").is_file(), "用户的同名技能必须留着")

    def test_status_counts_installed_skills(self) -> None:
        self.assertEqual(self.script.status(self.ws), (0, 6))
        self.script.install(self.ws)
        self.assertEqual(self.script.status(self.ws), (6, 6))

    def test_dry_run_changes_nothing(self) -> None:
        installed, _ = self.script.install(self.ws, dry_run=True)
        self.assertEqual(len(installed), 6)
        self.assertFalse((self.ws / "skills").exists())


if __name__ == "__main__":
    unittest.main()
