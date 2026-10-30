#!/usr/bin/env python3
"""Keep ``octop-plugin/_vendor/ponytail`` equal to upstream's text, and guard the fork.

Runs inside ``.github/workflows/sync-upstream.yml`` after the fork merged
``upstream/main``. It only ever *reads* upstream files out of the merged tree and
writes into ``octop-plugin/`` — the directory this fork owns.

Subcommands (any failure exits non-zero with a readable message):

    upstream-version [--ref R]   print the version upstream declares (root ``plugin.yaml`` at R)
    adapter-version              print the version this fork's plugin declares
    guard-upstream  [--ref R]    abort if upstream's tree touches a fork-owned path
    guard-merge     --base SHA   abort if this merge touched a fork-owned path
    vendor          --pinned SHA copy the vendored files and refresh the metadata
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "octop-plugin"
VENDOR = PLUGIN / "_vendor" / "ponytail"
RUNTIME = PLUGIN / "ponytail_runtime.py"
MANIFEST = PLUGIN / "plugin.yaml"

#: Upstream paths vendored verbatim, relative to the upstream root.
VENDORED = ("LICENSE", "__init__.py", "commands", "skills")
#: Written by this script; never treated as stale.
GENERATED = ("UPSTREAM_VERSION", "CHECKSUMS.sha256")
#: Fork-owned paths (identified from the fork history: these exist only here, and
#: upstream owns every other path — including the rest of ``.github/``).
PROTECTED = ("octop-plugin/", ".github/workflows/sync-upstream.yml")

SEMVER = re.compile(r"\d+\.\d+\.\d+")


def git(*args: str) -> str:
    """Run git in the repo root; abort loudly on failure."""
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode()


def git_bytes(*args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def protected_hits(paths: list[str]) -> list[str]:
    """Paths a merge must not touch. Precise on purpose: ``.github/`` stays upstream's."""
    return sorted(
        p for p in paths if p == PROTECTED[1] or p.startswith(PROTECTED[0])
    )


def upstream_version(ref: str) -> str:
    match = re.search(r"(?m)^version:\s*(\d+\.\d+\.\d+)\s*$", git("show", f"{ref}:plugin.yaml"))
    if not match:
        sys.exit(f"{ref}:plugin.yaml has no pinnable X.Y.Z version")
    return match.group(1)


def adapter_version() -> str:
    """The version our adapter declares (``octop-plugin/plugin.yaml``, a fork-owned file)."""
    match = re.search(r"(?m)^version:\s*(\d+\.\d+\.\d+)\s*$", MANIFEST.read_text(encoding="utf-8"))
    if not match:
        sys.exit("octop-plugin/plugin.yaml has no pinnable X.Y.Z version")
    return match.group(1)


def vendored_paths(ref: str) -> list[str]:
    """Upstream's files for the vendored subset, at ``ref``."""
    return sorted(
        p for p in git("ls-tree", "-r", "--name-only", ref, "--", *VENDORED).splitlines() if p
    )


def write_checksums() -> None:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(VENDOR).as_posix()}"
        for path in sorted(VENDOR.rglob("*"))
        if path.is_file() and path.relative_to(VENDOR).as_posix() not in GENERATED[1:]
    ]
    (VENDOR / "CHECKSUMS.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_runtime(version: str, pinned: str, content_commit: str) -> None:
    text = RUNTIME.read_text(encoding="utf-8")
    new = text
    for name, value in (
        ("UPSTREAM_VERSION", version),
        ("PINNED_COMMIT", pinned),
        ("CONTENT_COMMIT", content_commit),
    ):
        new, count = re.subn(
            rf'(?m)^{name} = "[^"]*"', f'{name} = "{value}"', new
        )
        if count != 1:
            sys.exit(f"{RUNTIME.name}: expected exactly one {name} assignment, found {count}")
    if new != text:
        RUNTIME.write_text(new, encoding="utf-8")


def update_plugin_yaml(version: str) -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    new, count = re.subn(r"(?m)^version:\s*\d+\.\d+\.\d+\s*$", f"version: {version}", text)
    if count != 1:
        sys.exit(f"{MANIFEST.name}: expected exactly one version line, found {count}")
    new = re.sub(r"上游 v\d+\.\d+\.\d+", f"上游 v{version}", new)
    if new != text:
        MANIFEST.write_text(new, encoding="utf-8")


def cmd_vendor(pinned: str) -> None:
    """Mirror the vendored subset of the merged tree (HEAD) and refresh metadata."""
    version = upstream_version("HEAD")
    keep = set(vendored_paths("HEAD"))
    if not keep:
        sys.exit("HEAD has none of the vendored upstream paths")
    missing = [p for p in VENDORED if p not in keep and not any(k.startswith(f"{p}/") for k in keep)]
    if missing:
        sys.exit(f"merged tree is missing vendored upstream paths: {', '.join(missing)}")

    for rel in sorted(keep):
        dest = VENDOR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(git_bytes("cat-file", "blob", f"HEAD:{rel}"))

    # Deletions upstream made must reach the vendor copy, otherwise a removed
    # command/skill keeps shipping.
    for path in sorted(VENDOR.rglob("*"), reverse=True):
        rel = path.relative_to(VENDOR).as_posix()
        if path.is_file() and rel not in keep and rel not in GENERATED:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    content_commit = git("log", "-1", "--format=%H", "HEAD", "--", *VENDORED).strip()
    (VENDOR / "UPSTREAM_VERSION").write_text(
        f"version: {version}\ncommit: {pinned}\ncontent_commit: {content_commit}\n",
        encoding="utf-8",
    )
    update_runtime(version, pinned, content_commit)
    update_plugin_yaml(version)
    write_checksums()
    print(f"vendor: version={version} pinned={pinned} content={content_commit[:12]} files={len(keep)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("upstream-version")
    p.add_argument("--ref", default="upstream/main")

    sub.add_parser("adapter-version")

    p = sub.add_parser("guard-upstream")
    p.add_argument("--ref", default="upstream/main")

    p = sub.add_parser("guard-merge")
    p.add_argument("--base", required=True)

    p = sub.add_parser("vendor")
    p.add_argument("--pinned", required=True)

    args = parser.parse_args()

    if args.cmd == "upstream-version":
        print(upstream_version(args.ref))
    elif args.cmd == "adapter-version":
        print(adapter_version())
    elif args.cmd == "guard-upstream":
        hits = protected_hits(git("ls-tree", "-r", "--name-only", args.ref).splitlines())
        if hits:
            sys.exit(
                f"abort: upstream tree touches fork-owned paths: {', '.join(hits)}"
            )
        print(f"guard-upstream: clean ({args.ref} has no fork-owned path)")
    elif args.cmd == "guard-merge":
        touched = git("diff", "--name-only", "--no-renames", args.base, "HEAD").splitlines()
        hits = protected_hits(touched)
        if hits:
            sys.exit(
                f"abort: this merge touched fork-owned paths: {', '.join(hits)}"
            )
        print(f"guard-merge: clean (merge {args.base[:7]}..HEAD touched {len(touched)} files, none ours)")
    elif args.cmd == "vendor":
        cmd_vendor(args.pinned)


if __name__ == "__main__":
    main()
