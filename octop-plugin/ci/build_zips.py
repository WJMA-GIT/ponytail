#!/usr/bin/env python3
"""Verify the adapter package and build the release zip.

Run after ``sync_upstream.py vendor`` (and before anything is tagged or pushed):

    python3 octop-plugin/ci/build_zips.py

Checks, in order — any failure exits non-zero and nothing is published:

1. version consistency: ``plugin.yaml`` == ``_vendor/.../UPSTREAM_VERSION`` ==
   ``ponytail_runtime.UPSTREAM_VERSION`` (the plugin version *is* the upstream
   version, never suffixed);
2. vendor integrity: the vendored bytes equal upstream's text in the merged tree
   (no missing, stale or hand-edited file) and ``CHECKSUMS.sha256`` agrees;
3. package shape: manifest fields, entry file, vendor entry point;
4. the zip itself: expected entries present, no absolute/escaping path.

Writes ``octop-plugin/BUILD`` (the label ``ponytail_doctor`` reports) and
``dist/ponytail-octop-<version>.zip``.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "octop-plugin"
VENDOR = PLUGIN / "_vendor" / "ponytail"
RUNTIME = PLUGIN / "ponytail_runtime.py"
MANIFEST = PLUGIN / "plugin.yaml"
DIST = ROOT / "dist"

#: Upstream paths vendored verbatim (kept in sync with sync_upstream.py).
VENDORED = ("LICENSE", "__init__.py", "commands", "skills")
#: Not shipped inside a plugin: build tooling and byte-compiled leftovers.
SKIP_DIRS = {"ci", "__pycache__"}

VERSION = re.compile(r"\d+\.\d+\.\d+")
REQUIRED_ENTRIES = (
    "ponytail/plugin.yaml",
    "ponytail/main.py",
    "ponytail/ponytail_runtime.py",
    "ponytail/_vendor/ponytail/__init__.py",
    "ponytail/_vendor/ponytail/UPSTREAM_VERSION",
)


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode()


def git_bytes(*args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def metadata(text: str) -> dict[str, str]:
    """Parse the ``key: value`` lines of an ``UPSTREAM_VERSION`` file."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            pairs[key.strip()] = value.strip()
    return pairs


def version_problems(plugin_yaml: str, upstream_meta: str, runtime_src: str) -> list[str]:
    """All three version sources must agree and be a pinned X.Y.Z."""
    problems: list[str] = []
    declared = re.search(r"(?m)^version:\s*(\S+)\s*$", plugin_yaml)
    vendored = metadata(upstream_meta).get("version", "")
    constant = re.search(r'(?m)^UPSTREAM_VERSION = "([^"]*)"', runtime_src)
    sources = {
        "octop-plugin/plugin.yaml": declared.group(1) if declared else "",
        "octop-plugin/_vendor/ponytail/UPSTREAM_VERSION": vendored,
        "octop-plugin/ponytail_runtime.py": constant.group(1) if constant else "",
    }
    for where, value in sources.items():
        if not VERSION.fullmatch(value):
            problems.append(f"{where}: version is not a pinned X.Y.Z ({value!r})")
    if len(set(sources.values())) > 1:
        problems.append(
            "version mismatch: " + ", ".join(f"{k}={v}" for k, v in sources.items())
        )
    return problems


def vendor_problems(head: str = "HEAD") -> list[str]:
    """The vendor copy must be byte-identical to upstream's text in ``head``."""
    problems: list[str] = []
    upstream = sorted(
        p for p in git("ls-tree", "-r", "--name-only", head, "--", *VENDORED).splitlines() if p
    )
    if not upstream:
        return [f"{head} has neither LICENSE/__init__.py/commands/skills"]
    for rel in upstream:
        local = VENDOR / rel
        if not local.is_file():
            problems.append(f"vendor missing {rel}")
        elif local.read_bytes() != git_bytes("cat-file", "blob", f"{head}:{rel}"):
            problems.append(f"vendor differs from {head}:{rel}")
    expected = set(upstream) | {"UPSTREAM_VERSION", "CHECKSUMS.sha256"}
    for path in sorted(VENDOR.rglob("*")):
        rel = path.relative_to(VENDOR).as_posix()
        if path.is_file() and rel not in expected:
            problems.append(f"vendor has stale {rel} (not in {head})")
    manifest = VENDOR / "CHECKSUMS.sha256"
    if not manifest.is_file():
        problems.append("vendor missing CHECKSUMS.sha256")
    else:
        listed = set()
        for line in manifest.read_text(encoding="utf-8").splitlines():
            digest, _, rel = line.partition("  ")
            listed.add(rel)
            target = VENDOR / rel
            if not target.is_file():
                problems.append(f"CHECKSUMS lists missing {rel}")
            elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                problems.append(f"CHECKSUMS mismatch {rel}")
        for rel in sorted(expected - {"CHECKSUMS.sha256"} - listed):
            problems.append(f"CHECKSUMS lacks {rel}")
    return problems


def package_problems() -> list[str]:
    text = MANIFEST.read_text(encoding="utf-8")
    fields = metadata(text)
    problems = [f"plugin.yaml lacks {key}" for key in ("id", "version", "kind", "entry") if not fields.get(key)]
    entry = PLUGIN / fields.get("entry", "missing")
    if not entry.is_file():
        problems.append(f"plugin.yaml entry {fields.get('entry')!r} does not exist")
    if fields.get("kind") not in (None, "skill"):
        problems.append(f"plugin.yaml kind is {fields.get('kind')!r}, expected 'skill'")
    return problems


def build(version: str, label: str) -> Path:
    (PLUGIN / "BUILD").write_text(label + "\n", encoding="utf-8")
    DIST.mkdir(exist_ok=True)
    asset = DIST / f"ponytail-octop-{version}.zip"
    files = sorted(
        p
        for p in PLUGIN.rglob("*")
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(PLUGIN).parts)
    )
    with zipfile.ZipFile(asset, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            rel = path.relative_to(PLUGIN).as_posix()
            info = zipfile.ZipInfo(f"ponytail/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return asset


def asset_problems(asset: Path) -> list[str]:
    with zipfile.ZipFile(asset) as zf:
        names = zf.namelist()
        if zf.testzip() is not None:
            return [f"{asset.name}: corrupt member"]
    problems = [f"{asset.name} lacks {name}" for name in REQUIRED_ENTRIES if name not in names]
    problems += [
        f"{asset.name}: unsafe entry {name}"
        for name in names
        if name.startswith("/") or ".." in name.split("/")
    ]
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验，不写 BUILD、不产出资产")
    args = parser.parse_args()

    problems = version_problems(
        MANIFEST.read_text(encoding="utf-8"),
        (VENDOR / "UPSTREAM_VERSION").read_text(encoding="utf-8"),
        RUNTIME.read_text(encoding="utf-8"),
    )
    problems += package_problems()
    problems += vendor_problems()
    if problems:
        sys.exit("package check failed:\n  " + "\n  ".join(problems))
    if args.check:
        print("check: ok（版本一致、vendor 与 HEAD 的上游原文一致、包结构完整）")
        return

    version = metadata(MANIFEST.read_text(encoding="utf-8"))["version"]
    if not VERSION.fullmatch(version):
        sys.exit(f"plugin.yaml version {version!r} is not a pinned X.Y.Z")
    label = f"{version}-ci.{metadata((VENDOR / 'UPSTREAM_VERSION').read_text(encoding='utf-8')).get('content_commit', 'unknown')[:7]}"
    asset = build(version, label)
    problems = asset_problems(asset)
    if problems:
        sys.exit("asset check failed:\n  " + "\n  ".join(problems))

    print(f"version: {version}")
    print(f"build:   {label}")
    print(f"asset:   {asset.relative_to(ROOT).as_posix()}")
    print(f"sha256:  {hashlib.sha256(asset.read_bytes()).hexdigest()}")
    print(f"size:    {asset.stat().st_size} bytes")


if __name__ == "__main__":
    main()
