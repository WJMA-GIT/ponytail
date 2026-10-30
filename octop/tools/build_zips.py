#!/usr/bin/env python3
"""Build the two installable ZIPs (one plugin root each).

Layout requirement: the archive contains exactly one directory that holds
``plugin.yaml``::

    ponytail-octop-tool/plugin.yaml
    ponytail-octop-tool/main.py
    ponytail-octop-tool/_vendor/ponytail/...

Usage::

    python3 octop/tools/build_zips.py --out /tmp/octop-dist
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OCTOP_DIR = REPO_ROOT / "octop"

TARGETS = {
    "tool": "ponytail-octop-tool",
    "hook": "ponytail-octop-hook",
}

#: Never ship caches or byte-code inside the archive.
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache"}


def build(plugin: str, root_name: str, out_dir: Path) -> Path:
    source = OCTOP_DIR / plugin
    version = "0.0.0"
    for line in (source / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip()
            break
    archive = out_dir / f"{root_name}-{version}.zip"
    files = sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and not (EXCLUDE_PARTS & set(path.parts))
    )
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = Path(root_name) / path.relative_to(source)
            zf.write(path, arcname.as_posix())
    roots = {name.split("/")[0] for name in zipfile.ZipFile(archive).namelist()}
    manifests = [
        name for name in zipfile.ZipFile(archive).namelist() if name.endswith("plugin.yaml")
    ]
    if len(roots) != 1 or len(manifests) != 1 or manifests[0].count("/") != 1:
        raise SystemExit(f"invalid archive layout for {archive}: roots={roots} manifests={manifests}")
    print(f"{archive.name}: {len(files)} files, root={roots.pop()}, manifest={manifests[0]}")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path("/tmp") / "octop-dist"))
    args = parser.parse_args(argv)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for plugin, root_name in TARGETS.items():
        build(plugin, root_name, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
