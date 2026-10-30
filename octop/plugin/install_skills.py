#!/usr/bin/env python3
"""Copy ponytail's six SKILL.md into an agent workspace ``skills/`` directory.

Why: Octop lists every ``skills/<slug>/SKILL.md`` in the system prompt (name +
description), and a leading ``/<slug>`` in a user message is treated as an
explicit skill invoke. That is the closest equivalent of ponytail's native slash
commands — without it the model has to *think of* calling ``ponytail_skill``
before it ever sees the text.

Trade-off, stated plainly: this writes into the user's workspace. Every
directory we create carries a ``.ponytail-skill`` marker so ``--remove`` can
delete exactly what we added and nothing else.

Usage::

    python3 install_skills.py --workspace ~/agent-workspace
    python3 install_skills.py --workspace . --dry-run
    python3 install_skills.py --workspace . --remove
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 与插件一起分发：装完后脚本就在 <plugins>/ponytail/install_skills.py
VENDOR_SKILLS = Path(__file__).resolve().parent / "_vendor" / "ponytail" / "skills"
MARKER = ".ponytail-skill"
SKILL_PREFIX = "ponytail"


def vendored_skills() -> list[Path]:
    """The six ``ponytail*`` skill directories, sorted."""
    return sorted(p for p in VENDOR_SKILLS.glob(f"{SKILL_PREFIX}*") if (p / "SKILL.md").is_file())


def install(workspace: Path, *, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Copy missing skills; never overwrite. Returns ``(installed, skipped)``."""
    target_root = workspace / "skills"
    installed: list[str] = []
    skipped: list[str] = []
    for source in vendored_skills():
        target = target_root / source.name
        if target.exists():
            # 工作区已有的优先：用户可能自己改过，覆盖会毁掉他的改动。
            skipped.append(source.name)
            continue
        installed.append(source.name)
        if dry_run:
            continue
        target.mkdir(parents=True)
        shutil.copy2(source / "SKILL.md", target / "SKILL.md")
        (target / MARKER).write_text("installed by the ponytail Octop plugin\n", encoding="utf-8")
    return installed, skipped


def remove(workspace: Path, *, dry_run: bool = False) -> list[str]:
    """Delete only the directories we created (marker present)."""
    target_root = workspace / "skills"
    removed: list[str] = []
    if not target_root.is_dir():
        return removed
    for candidate in sorted(target_root.glob(f"{SKILL_PREFIX}*")):
        if not (candidate / MARKER).is_file():
            continue  # 不是我们装的（或用户改过），不动
        removed.append(candidate.name)
        if not dry_run:
            shutil.rmtree(candidate)
    return removed


def status(workspace: Path) -> tuple[int, int]:
    """``(found, expected)`` — how many of our six skills exist in *workspace*."""
    target_root = workspace / "skills"
    expected = len(vendored_skills())
    found = sum(1 for source in vendored_skills() if (target_root / source.name / "SKILL.md").is_file())
    return found, expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".", help="agent workspace (default: cwd)")
    parser.add_argument("--remove", action="store_true", help="remove the skills this tool installed")
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 2

    if args.remove:
        removed = remove(workspace, dry_run=args.dry_run)
        verb = "would remove" if args.dry_run else "removed"
        print(f"{verb}: {len(removed)}" + (f" ({', '.join(removed)})" if removed else ""))
        return 0

    installed, skipped = install(workspace, dry_run=args.dry_run)
    verb = "would install" if args.dry_run else "installed"
    print(f"{verb}: {len(installed)}" + (f" ({', '.join(installed)})" if installed else ""))
    if skipped:
        print(f"skipped (already present, not overwritten): {', '.join(skipped)}")
    print(f"target: {workspace / 'skills'}")
    print(f"undo:   python3 install_skills.py --workspace {workspace} --remove")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
