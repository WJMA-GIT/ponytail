#!/usr/bin/env python3
"""Vendor upstream ponytail content into the Octop plugin (byte-identical).

Why vendoring: an Octop plugin directory is installed with ``shutil.copytree``
and must work without the rest of the fork checkout. We therefore ship a
**byte-identical copy** of the upstream files the adapter uses — never a patched
copy — and verify it with ``CHECKSUMS.sha256``.

Upstream files are read from this same repository root (the fork keeps upstream
trees untouched), so syncing needs no network access.

Single source of truth for the version: **upstream's own ``plugin.yaml``**.
Syncing rewrites the derived values so nothing is maintained by hand:

* ``octop/plugin/plugin.yaml`` ``version``      ← upstream ``plugin.yaml:version``
* ``octop/plugin/ponytail_runtime.py``          ← ``UPSTREAM_VERSION`` / ``PINNED_COMMIT`` / ``CONTENT_COMMIT``
* ``_vendor/ponytail/UPSTREAM_VERSION``         ← version + both commits

Usage::

    python3 octop/tools/sync_upstream.py            # write/refresh everything
    python3 octop/tools/sync_upstream.py --check    # verify, exit 1 on drift
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OCTOP_DIR = REPO_ROOT / "octop"
PLUGIN_DIR = OCTOP_DIR / "plugin"
VENDOR_NAME = Path("_vendor") / "ponytail"
RUNTIME_FILE = PLUGIN_DIR / "ponytail_runtime.py"
PLUGIN_MANIFEST = PLUGIN_DIR / "plugin.yaml"

#: Upstream files the adapter actually reads. Everything is copied verbatim.
#: Upstream's own ``plugin.yaml`` is deliberately NOT vendored: an Octop plugin
#: ZIP must contain exactly one ``plugin.yaml`` (the plugin root's), and the
#: upstream version is recorded in ``UPSTREAM_VERSION`` instead.
COPY_FILES = ("__init__.py", "LICENSE")
COPY_DIRS = ("skills", "commands")

#: Paths whose last upstream commit defines the recorded ``content_commit``.
PROVENANCE_PATHS = ("__init__.py", "skills", "commands", "LICENSE")

RUNTIME_TEMPLATE = '''UPSTREAM_VERSION = "{version}"
PINNED_COMMIT = "{pin}"
CONTENT_COMMIT = "{content}"
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upstream_version() -> str:
    """Read ``version`` from **upstream's** root ``plugin.yaml`` (never modified)."""
    for line in (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("upstream plugin.yaml has no version line")


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
    except OSError:  # git not installed
        return ""
    return result.stdout.strip()


def content_commit() -> str:
    """Newest upstream commit that touched the vendored files (empty if unknown)."""
    return _git("log", "-1", "--no-merges", "--format=%H", "--", *PROVENANCE_PATHS)


def pinned_commit(cli_value: str | None) -> str:
    """The upstream *sync point* (tip of upstream/main when the fork last merged).

    Derived, never hand-maintained: ``--commit`` if given, else the value already
    recorded in the runtime, else the content commit.
    """
    if cli_value and cli_value.strip():
        return cli_value.strip()
    if RUNTIME_FILE.is_file():
        match = re.search(r'PINNED_COMMIT = "([0-9a-f]+)"', RUNTIME_FILE.read_text(encoding="utf-8"))
        if match and match.group(1) not in {"", "unknown"}:
            return match.group(1)
    return content_commit() or "unknown"


def read_runtime_constants() -> dict[str, str]:
    text = RUNTIME_FILE.read_text(encoding="utf-8") if RUNTIME_FILE.is_file() else ""
    out: dict[str, str] = {}
    for key in ("UPSTREAM_VERSION", "PINNED_COMMIT", "CONTENT_COMMIT"):
        match = re.search(rf'{key} = "([^"]*)"', text)
        if match:
            out[key] = match.group(1)
    return out


def upstream_files() -> list[Path]:
    """Absolute paths of the upstream files to vendor, sorted for stable output."""
    files: list[Path] = []
    for name in COPY_FILES:
        candidate = REPO_ROOT / name
        if candidate.is_file():
            files.append(candidate)
    for name in COPY_DIRS:
        root = REPO_ROOT / name
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(files)


def manifest_for(vendor_root: Path) -> str:
    """Render ``CHECKSUMS.sha256`` for every file under ``vendor_root``."""
    lines = []
    for path in sorted(p for p in vendor_root.rglob("*") if p.is_file()):
        if path.name == "CHECKSUMS.sha256":
            continue
        lines.append(f"{sha256(path)}  {path.relative_to(vendor_root).as_posix()}")
    return "\n".join(lines) + "\n"


def rewrite_versions(version: str, pin: str, content: str) -> None:
    """Write the version into the plugin manifest and the runtime constants."""
    text = PLUGIN_MANIFEST.read_text(encoding="utf-8")
    text = re.sub(r"^version:.*$", f"version: {version}", text, count=1, flags=re.MULTILINE)
    PLUGIN_MANIFEST.write_text(text, encoding="utf-8")

    runtime = RUNTIME_FILE.read_text(encoding="utf-8")
    for key in ("UPSTREAM_VERSION", "PINNED_COMMIT", "CONTENT_COMMIT"):
        value = {"UPSTREAM_VERSION": version, "PINNED_COMMIT": pin, "CONTENT_COMMIT": content}[key]
        runtime = re.sub(rf'^{key} = "[^"]*"$', f'{key} = "{value}"', runtime, count=1, flags=re.MULTILINE)
    RUNTIME_FILE.write_text(runtime, encoding="utf-8")


def sync(version: str, pin: str, content: str) -> Path:
    vendor_root = PLUGIN_DIR / VENDOR_NAME
    if vendor_root.exists():
        shutil.rmtree(vendor_root)
    vendor_root.mkdir(parents=True)
    for source in upstream_files():
        destination = vendor_root / source.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (vendor_root / "UPSTREAM_VERSION").write_text(
        f"version: {version}\ncommit: {pin}\ncontent_commit: {content}\n", encoding="utf-8"
    )
    (vendor_root / "CHECKSUMS.sha256").write_text(manifest_for(vendor_root), encoding="utf-8")
    rewrite_versions(version, pin, content)
    return vendor_root


def check() -> list[str]:
    """Return every drift problem (vendor bytes, version, constants, provenance)."""
    problems: list[str] = []
    version = upstream_version()
    content = content_commit()

    vendor_root = PLUGIN_DIR / VENDOR_NAME
    if not (vendor_root / "__init__.py").is_file():
        return [f"vendored copy missing: {vendor_root.relative_to(REPO_ROOT)}"]

    expected = {source.relative_to(REPO_ROOT).as_posix(): sha256(source) for source in upstream_files()}
    actual = {
        path.relative_to(vendor_root).as_posix(): sha256(path)
        for path in vendor_root.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    }
    expected["UPSTREAM_VERSION"] = None  # generated by this script
    for relative, digest in sorted(expected.items()):
        if relative not in actual:
            problems.append(f"missing {relative}")
        elif digest is not None and actual[relative] != digest:
            problems.append(f"stale {relative} (upstream changed) — run sync_upstream.py")
    for relative in sorted(set(actual) - set(expected)):
        problems.append(f"extra {relative}")

    manifest = vendor_root / "CHECKSUMS.sha256"
    if not manifest.is_file():
        problems.append("CHECKSUMS.sha256 missing")
    elif manifest.read_text(encoding="utf-8") != manifest_for(vendor_root):
        problems.append("CHECKSUMS.sha256 out of date — run sync_upstream.py")

    recorded = read_runtime_constants()
    if recorded.get("UPSTREAM_VERSION") != version:
        problems.append(
            f"plugin version stale: runtime={recorded.get('UPSTREAM_VERSION')!r} upstream={version!r}"
        )
    manifest_version = ""
    for line in PLUGIN_MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            manifest_version = line.split(":", 1)[1].strip()
    if manifest_version != version:
        problems.append(f"plugin.yaml version={manifest_version!r} != upstream {version!r}")
    if content and recorded.get("CONTENT_COMMIT") not in (None, "", content):
        problems.append(
            "vendored content is behind upstream "
            f"(recorded {recorded.get('CONTENT_COMMIT')[:7]}, upstream {content[:7]}) — run sync_upstream.py"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    parser.add_argument(
        "--commit",
        default=None,
        help="record this upstream commit as the sync point (default: keep the recorded one)",
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = check()
        if problems:
            print("adapter is out of sync with upstream:")
            for problem in problems:
                print(f"  - {problem}")
            print("run: python3 octop/tools/sync_upstream.py")
            return 1
        print(f"in sync with upstream: ok (version={upstream_version()})")
        return 0

    version = upstream_version()
    pin = pinned_commit(args.commit)
    content = content_commit() or pin
    vendor_root = sync(version, pin, content)
    count = sum(1 for path in vendor_root.rglob("*") if path.is_file())
    print(f"synced {count} files -> {vendor_root.relative_to(REPO_ROOT)}")
    print(f"upstream version={version}")
    print(f"sync point={pin}")
    print(f"content commit={content}")
    print(f"wrote version {version} into {PLUGIN_MANIFEST.relative_to(REPO_ROOT)} and {RUNTIME_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
