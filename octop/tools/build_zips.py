#!/usr/bin/env python3
"""Build the installable ZIP (one plugin root, one plugin.yaml).

Layout requirement: the archive contains exactly one directory that holds
``plugin.yaml``::

    ponytail-octop/plugin.yaml
    ponytail-octop/main.py
    ponytail-octop/_vendor/ponytail/...
    ponytail-octop/BUILD          # build label: adapter commit + timestamp

The plugin version is always the upstream version (no suffix), so adapter-only
rebuilds are told apart by the ``BUILD`` file instead of the version number.

Usage::

    python3 octop/tools/build_zips.py --out /tmp/octop-dist
    python3 octop/tools/build_zips.py --out /tmp/octop-dist --build-label 8705231-20261030T0130
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OCTOP_DIR = REPO_ROOT / "octop"
PLUGIN_DIR = OCTOP_DIR / "plugin"
ROOT_NAME = "ponytail-octop"

#: Never ship caches or byte-code inside the archive.
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", "BUILD"}


def plugin_version() -> str:
    for line in (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("plugin.yaml has no version")


def default_label() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        sha = "unknown"
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M")
    return f"{sha or 'unknown'}-{stamp}"


def build(out_dir: Path, label: str) -> Path:
    version = plugin_version()
    archive = out_dir / f"{ROOT_NAME}-{version}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / ROOT_NAME
        files = [
            path
            for path in PLUGIN_DIR.rglob("*")
            if path.is_file() and not (EXCLUDE_PARTS & set(path.parts))
        ]
        for path in files:
            target = staging / path.relative_to(PLUGIN_DIR)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (staging / "BUILD").write_text(label + "\n", encoding="utf-8")
        names = sorted(p for p in staging.rglob("*") if p.is_file())
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in names:
                zf.write(path, (Path(ROOT_NAME) / path.relative_to(staging)).as_posix())

    with zipfile.ZipFile(archive) as zf:
        entries = zf.namelist()
    roots = {name.split("/")[0] for name in entries}
    manifests = [name for name in entries if name.endswith("plugin.yaml")]
    if len(roots) != 1 or len(manifests) != 1 or manifests[0].count("/") != 1:
        raise SystemExit(f"invalid archive layout for {archive}: roots={roots} manifests={manifests}")
    print(f"{archive.name}: {len(entries)} files, root={roots.pop()}, version={version}, build={label}")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path("/tmp") / "octop-dist"))
    parser.add_argument("--build-label", default=None, help="default: <git-sha7>-<UTC timestamp>")
    args = parser.parse_args(argv)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    build(out_dir, args.build_label or default_label())
    return 0


if __name__ == "__main__":
    sys.exit(main())
