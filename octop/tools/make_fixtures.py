#!/usr/bin/env python3
"""Generate behaviour fixtures from the **pristine** upstream ``__init__.py``.

The fixtures are the acceptance baseline: they are produced by the unmodified
upstream code in this checkout (repo root, never touched by the adapter) and the
tests assert the plugin returns byte-identical output.

Usage::

    python3 octop/tools/make_fixtures.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OCTOP_DIR = REPO_ROOT / "octop"
FIXTURE_DIR = OCTOP_DIR / "tests" / "fixtures"
LEVELS = ("off", "lite", "full", "ultra")
SKILLS = (
    "ponytail",
    "ponytail-audit",
    "ponytail-debt",
    "ponytail-gain",
    "ponytail-help",
    "ponytail-review",
)


def load_upstream():
    spec = importlib.util.spec_from_file_location("ponytail_pristine", REPO_ROOT / "__init__.py")
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load upstream __init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    upstream = load_upstream()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    for level in LEVELS:
        (FIXTURE_DIR / f"rules_{level}.txt").write_text(
            upstream.build_injected_context(level), encoding="utf-8"
        )

    for skill in SKILLS:
        raw = (REPO_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        for level in LEVELS:
            body = upstream._filter_skill_body_for_mode(raw, level)
            (FIXTURE_DIR / f"skill_{skill}_{level}.txt").write_text(body, encoding="utf-8")

    written = sorted(FIXTURE_DIR.glob("*.txt"))
    print(f"wrote {len(written)} fixtures to {FIXTURE_DIR.relative_to(REPO_ROOT)}")
    print(f"upstream SKILL_COMMANDS={sorted(upstream.SKILL_COMMANDS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
