"""Pure-stdlib tests: vendored copy, mode precedence, content equality, errors.

Run::

    python3 -m unittest discover -s octop/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
OCTOP_DIR = TESTS_DIR.parent
PLUGIN_DIR = OCTOP_DIR / "plugin"
FIXTURES = TESTS_DIR / "fixtures"
LEVELS = ("off", "lite", "full", "ultra")
SKILLS = (
    "ponytail",
    "ponytail-audit",
    "ponytail-debt",
    "ponytail-gain",
    "ponytail-help",
    "ponytail-review",
)

sys.path.insert(0, str(PLUGIN_DIR))
import ponytail_runtime as rt  # noqa: E402  (path set above)


class VendorTests(unittest.TestCase):
    def test_vendored_copy_matches_upstream_checksums(self) -> None:
        report = rt.doctor()
        self.assertTrue(report["vendor"]["ok"], report["vendor"])
        self.assertTrue(report["checksums"]["ok"], report["checksums"])
        self.assertGreater(report["checksums"]["checked"], 10)

    def test_single_plugin_ships_one_vendor_copy(self) -> None:
        """One plugin package => exactly one vendored upstream copy on disk."""
        self.assertTrue((PLUGIN_DIR / "_vendor" / "ponytail" / "__init__.py").is_file())
        siblings = [
            path for path in OCTOP_DIR.iterdir() if path.is_dir() and (path / "plugin.yaml").is_file()
        ]
        self.assertEqual([path.name for path in siblings], ["plugin"], "only one plugin directory")


class RulesEqualityTests(unittest.TestCase):
    """The acceptance baseline: plugin output must equal pristine upstream output."""

    def test_four_levels_are_byte_identical(self) -> None:
        for level in LEVELS:
            expected = (FIXTURES / f"rules_{level}.txt").read_text(encoding="utf-8")
            self.assertEqual(rt.rules_for(level), expected, f"level={level}")

    def test_off_is_empty_string(self) -> None:
        self.assertEqual(rt.rules_for("off"), "")

    def test_level_is_case_insensitive(self) -> None:
        self.assertEqual(rt.rules_for("ULTRA"), rt.rules_for("ultra"))

    def test_invalid_level_raises_classified_error(self) -> None:
        with self.assertRaises(rt.InvalidLevel) as caught:
            rt.rules_for("extreme")
        self.assertEqual(caught.exception.kind, "invalid_argument")
        self.assertEqual(caught.exception.details["valid"], list(LEVELS))


class SkillEqualityTests(unittest.TestCase):
    def test_all_skills_all_levels_are_byte_identical(self) -> None:
        for skill in SKILLS:
            for level in LEVELS:
                expected = (FIXTURES / f"skill_{skill}_{level}.txt").read_text(encoding="utf-8")
                self.assertEqual(rt.skill_body(skill, level), expected, f"{skill}/{level}")

    def test_skill_body_keeps_full_content_after_frontmatter(self) -> None:
        body = rt.skill_body("ponytail-review", "full")
        self.assertNotIn("argument-hint:", body.splitlines()[0] if body else "")
        self.assertIn("Review", body)
        self.assertGreater(len(body), 3000, "review skill should not be truncated")

    def test_unknown_skill_lists_alternatives(self) -> None:
        with self.assertRaises(rt.UnknownSkill) as caught:
            rt.skill_body("ponytail-nope")
        self.assertEqual(caught.exception.kind, "invalid_argument")
        self.assertIn("ponytail-review", caught.exception.details["available"])


class ModePrecedenceTests(unittest.TestCase):
    """env > config.json:defaultMode > built-in default, exactly like upstream."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_home = Path(self._tmp.name)
        patcher = mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(self.config_home)},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PONYTAIL_DEFAULT_MODE", None)

    def _write_config(self, payload: dict[str, object]) -> None:
        directory = self.config_home / "ponytail"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_builtin_default_is_full(self) -> None:
        self.assertEqual(rt.default_mode(), "full")
        self.assertEqual(rt.mode_source(), "default")

    def test_config_beats_default(self) -> None:
        self._write_config({"defaultMode": "ultra"})
        self.assertEqual(rt.default_mode(), "ultra")
        self.assertEqual(rt.mode_source(), "config")

    def test_env_beats_config(self) -> None:
        self._write_config({"defaultMode": "ultra"})
        os.environ["PONYTAIL_DEFAULT_MODE"] = "lite"
        self.assertEqual(rt.default_mode(), "lite")
        self.assertEqual(rt.mode_source(), "env")

    def test_invalid_config_value_falls_back(self) -> None:
        self._write_config({"defaultMode": "turbo"})
        self.assertEqual(rt.default_mode(), "full")
        self.assertEqual(rt.mode_source(), "default")

    def test_set_mode_persists_and_preserves_other_keys(self) -> None:
        self._write_config({"quietStartup": True})
        self.assertEqual(rt.set_mode("lite"), "lite")
        stored = json.loads((self.config_home / "ponytail" / "config.json").read_text())
        self.assertEqual(stored["defaultMode"], "lite")
        self.assertTrue(stored["quietStartup"])
        self.assertEqual(rt.default_mode(), "lite")

    def test_set_mode_rejects_invalid_level(self) -> None:
        with self.assertRaises(rt.InvalidLevel):
            rt.set_mode("turbo")

    def test_config_dir_is_upstream_layout(self) -> None:
        self.assertEqual(rt.config_path(), self.config_home / "ponytail" / "config.json")

    def test_unwritable_config_is_classified(self) -> None:
        # A regular file where the config directory should be -> mkdir fails.
        blocker = self.config_home / "ponytail"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(rt.ConfigUnwritable) as caught:
            rt.set_mode("full")
        self.assertEqual(caught.exception.kind, "config_unwritable")


class CommandTests(unittest.TestCase):
    def test_all_six_commands_are_readable(self) -> None:
        self.assertEqual(
            rt.command_names(),
            ["ponytail", "ponytail-audit", "ponytail-debt", "ponytail-gain", "ponytail-help", "ponytail-review"],
        )
        for name in rt.command_names():
            definition = rt.command_definition(name)
            self.assertTrue(definition["description"], name)
            self.assertTrue(definition["prompt"], name)

    def test_args_are_substituted(self) -> None:
        definition = rt.command_definition("ponytail", "ultra")
        self.assertIn("ultra", definition["prompt"])
        self.assertNotIn("{{args}}", definition["prompt"])

    def test_unknown_command_lists_alternatives(self) -> None:
        with self.assertRaises(rt.UnknownCommand) as caught:
            rt.command_definition("ponytail-nope")
        self.assertIn("ponytail-audit", caught.exception.details["available"])


class ShortcutDebtTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_finds_ceiling_and_upgrade_path(self) -> None:
        (self.root / "a.py").write_text(
            "x = 1  # shortcut: O(n^2) scan -> use an index when n > 1e5\n"
            "y = 2\n",
            encoding="utf-8",
        )
        (self.root / "b.dart").write_text(
            "// shortcut: 只支持单房间 -> 多房间时改成 map\n",
            encoding="utf-8",
        )
        items = rt.scan_shortcut_debt(str(self.root))
        self.assertEqual(len(items), 2)
        python_item = next(item for item in items if item["file"] == "a.py")
        self.assertEqual(python_item["line"], 1)
        self.assertIn("O(n^2)", python_item["ceiling"])
        self.assertIn("index", python_item["upgrade_path"])

    def test_ignores_skipped_dirs_and_unknown_suffixes(self) -> None:
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "x.js").write_text("// shortcut: nope\n", encoding="utf-8")
        (self.root / "notes.txt").write_text("shortcut: nope\n", encoding="utf-8")
        self.assertEqual(rt.scan_shortcut_debt(str(self.root)), [])

    def test_bad_path_is_classified(self) -> None:
        with self.assertRaises(rt.InvalidPath) as caught:
            rt.scan_shortcut_debt(str(self.root / "missing"))
        self.assertEqual(caught.exception.kind, "invalid_path")


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_pin_and_counts(self) -> None:
        report = rt.doctor()
        self.assertTrue(report["pinned_commit"].startswith(rt.PINNED_COMMIT))
        self.assertEqual(report["upstream_version"], rt.UPSTREAM_VERSION)
        self.assertEqual(report["skills"], 6)
        self.assertEqual(report["commands"], 6)
        self.assertTrue(report["vendor"]["ok"])

    def test_pin_constants_match_vendored_metadata(self) -> None:
        meta = rt.read_upstream_meta()
        self.assertEqual(meta["version"], rt.UPSTREAM_VERSION)
        self.assertTrue(meta["commit"].startswith(rt.PINNED_COMMIT))
        self.assertTrue(meta["content_commit"].startswith(rt.CONTENT_COMMIT[:7]))


class VersionPolicyTests(unittest.TestCase):
    """Plugin version == upstream version, generated — never hand-suffixed."""

    def _manifest_version(self) -> str:
        for line in (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
        self.fail("plugin.yaml has no version")

    def test_plugin_version_equals_upstream_version(self) -> None:
        version = self._manifest_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$", "no build/adaptation suffix allowed")
        self.assertEqual(version, rt.UPSTREAM_VERSION)
        self.assertEqual(version, rt.read_upstream_meta()["version"])

    def test_matches_upstream_manifest(self) -> None:
        upstream = ""
        for line in (OCTOP_DIR.parent / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                upstream = line.split(":", 1)[1].strip()
        self.assertEqual(self._manifest_version(), upstream, "sync regenerates this from upstream")

    def test_no_suffixed_versions_left_behind(self) -> None:
        offenders: list[str] = []
        pattern = re.compile(r"\b\d+\.\d+\.\d+\.[0-9]+\b")
        for path in sorted(OCTOP_DIR.rglob("*")):
            if not path.is_file() or path.suffix in {".zip", ".pyc"}:
                continue
            if "__pycache__" in path.parts or "fixtures" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(OCTOP_DIR)}:{number}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "version suffixes must be gone")

    def test_adapter_is_in_sync_with_upstream(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "sync_upstream", OCTOP_DIR / "tools" / "sync_upstream.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["sync_upstream"] = module
        spec.loader.exec_module(module)
        self.assertEqual(module.check(), [])


if __name__ == "__main__":
    unittest.main()
