#!/usr/bin/env python3
"""Focused checks for the CI helpers: guard precision, version agreement, asset shape.

    python3 octop-plugin/ci/test_ci.py
"""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import build_zips
import sync_upstream

PLUGIN_YAML = "id: ponytail\nversion: 5.3.3\nkind: skill\nentry: main.py\n"
UPSTREAM_META = "version: 5.3.3\ncommit: abc1234\ncontent_commit: " + "d" * 40 + "\n"
RUNTIME = 'UPSTREAM_VERSION = "5.3.3"\nPINNED_COMMIT = "abc1234"\n'


class GuardTest(unittest.TestCase):
    def test_only_fork_owned_paths_are_protected(self) -> None:
        touched = [
            "octop-plugin/main.py",
            "octop-plugin/_vendor/ponytail/skills/ponytail/SKILL.md",
            ".github/workflows/sync-upstream.yml",
            # upstream owns these: a merge touching them is normal, not a guard hit
            ".github/workflows/test.yml",
            ".github/workflows/publish.yml",
            ".github/CONTRIBUTING.md",
            "docs/octop.md",
            "octop/README.md",
        ]
        self.assertEqual(
            sync_upstream.protected_hits(touched),
            [
                ".github/workflows/sync-upstream.yml",
                "octop-plugin/_vendor/ponytail/skills/ponytail/SKILL.md",
                "octop-plugin/main.py",
            ],
        )

    def test_clean_merge_passes(self) -> None:
        self.assertEqual(sync_upstream.protected_hits(["README.md", "skills/ponytail/SKILL.md"]), [])


class VersionTest(unittest.TestCase):
    def test_agreeing_versions_pass(self) -> None:
        self.assertEqual(build_zips.version_problems(PLUGIN_YAML, UPSTREAM_META, RUNTIME), [])

    def test_mismatch_and_suffixed_version_fail(self) -> None:
        problems = build_zips.version_problems(
            PLUGIN_YAML,
            UPSTREAM_META.replace("5.3.3", "5.3.4"),
            RUNTIME.replace('"5.3.3"', '"5.3.3-octop"'),
        )
        self.assertTrue(any("not a pinned X.Y.Z" in p for p in problems), problems)
        self.assertTrue(any("version mismatch" in p for p in problems), problems)


class AssetTest(unittest.TestCase):
    def _check(self, names: list[str]) -> list[str]:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name in names:
                zf.writestr(name, "")
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "ponytail-octop-0.0.0.zip"
            asset.write_bytes(buffer.getvalue())
            return build_zips.asset_problems(asset)

    def test_missing_entries_are_reported(self) -> None:
        problems = self._check(list(build_zips.REQUIRED_ENTRIES[:1]))
        self.assertTrue(problems and all("lacks" in p for p in problems), problems)

    def test_unsafe_entry_is_reported(self) -> None:
        self.assertEqual(
            self._check([*build_zips.REQUIRED_ENTRIES, "../evil.py"]),
            ["ponytail-octop-0.0.0.zip: unsafe entry ../evil.py"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
