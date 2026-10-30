"""Glue between the Octop plugins and the vendored upstream ponytail.

Design rules (enforced by tests):

* **Upstream files are never modified.** Everything here imports the vendored,
  byte-identical copy of ponytail's ``__init__.py`` (its Hermes plugin entry,
  pure stdlib) and calls its public helpers — ``build_injected_context``,
  ``_filter_skill_body_for_mode``, ``_normalize_runtime_mode``, ``_default_mode``.
* **Output is byte-identical to upstream.** ``rules_for(level)`` returns exactly
  what ``build_injected_context(level)`` returns; ``skill_body`` returns exactly
  what upstream's frontmatter strip + mode filter returns.
* **Errors are classified, never swallowed.** Every failure raises
  :class:`PonytailError` with a stable ``kind`` so tools can report
  "dependency missing" vs "bad path" vs "upstream failed" separately.

This module is duplicated verbatim into both plugins (``octop/tool`` and
``octop/hook``) because each Octop plugin directory is installed independently.
``octop/tests/test_runtime.py`` asserts the two copies stay identical.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent

#: Upstream release. The plugin version **is** the upstream version — never suffixed.
#: Written by ``octop/tools/sync_upstream.py`` from upstream's own ``plugin.yaml``.
UPSTREAM_VERSION = "5.3.3"
#: Upstream sync point (tip of upstream/main when the fork last merged).
PINNED_COMMIT = "568510c"
#: Newest upstream commit that touched the vendored files (provenance of the bytes).
CONTENT_COMMIT = "ca88b4f377fd94f780ac9035808dc4dc54e7a9cf"

#: Documented order of upstream's modes (upstream keeps them in a set).
LEVELS: tuple[str, ...] = ("off", "lite", "full", "ultra")

_SHORTCUT_RE = re.compile(r"(?://|#|/\*|<!--)\s*shortcut:\s*(?P<body>.+?)(?:\*/|-->)?\s*$")


class PonytailError(RuntimeError):
    """Base class for classified failures."""

    kind = "upstream_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class VendorMissing(PonytailError):
    """The vendored upstream copy is absent or unreadable."""

    kind = "dependency_missing"


class InvalidLevel(PonytailError):
    """Level is not one of off/lite/full/ultra."""

    kind = "invalid_argument"


class UnknownSkill(PonytailError):
    """Skill name is not shipped by upstream."""

    kind = "invalid_argument"


class UnknownCommand(PonytailError):
    """Command name is not shipped by upstream."""

    kind = "invalid_argument"


class ConfigUnwritable(PonytailError):
    """The ponytail config file cannot be written."""

    kind = "config_unwritable"


class InvalidPath(PonytailError):
    """User supplied a directory that does not exist or is unreadable."""

    kind = "invalid_path"


# ── vendor resolution ────────────────────────────────────────────────────────


def vendor_root() -> Path:
    """Locate the vendored upstream copy.

    ``PONYTAIL_VENDOR`` (a path to the directory holding ``__init__.py``) wins —
    that is how the test-suite points at a pristine checkout. Otherwise look in
    the plugin directory (installed layout) and one level up (fork layout).
    """
    candidates: list[Path] = []
    override = os.environ.get("PONYTAIL_VENDOR", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(PLUGIN_DIR / "_vendor" / "ponytail")
    candidates.append(PLUGIN_DIR.parent / "_vendor" / "ponytail")
    for candidate in candidates:
        if (candidate / "__init__.py").is_file():
            return candidate
    raise VendorMissing(
        "vendored ponytail copy not found",
        searched=[str(path) for path in candidates],
        hint="run `python3 octop/tools/sync_upstream.py` in the fork, or set PONYTAIL_VENDOR",
    )


_UPSTREAM: Any | None = None


def upstream() -> Any:
    """Import the vendored ``__init__.py`` as a module (never modifies it)."""
    global _UPSTREAM
    if _UPSTREAM is not None:
        return _UPSTREAM
    root = vendor_root()
    spec = importlib.util.spec_from_file_location("ponytail_upstream", root / "__init__.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise VendorMissing("cannot load vendored __init__.py", path=str(root))
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("ponytail_upstream", module)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # upstream code blew up: keep the distinction
        raise PonytailError(f"failed to import vendored upstream: {exc}", path=str(root)) from exc
    _UPSTREAM = module
    return module


# ── mode resolution (mirrors upstream precedence) ────────────────────────────


def config_dir() -> Path:
    """``<XDG_CONFIG_HOME|~/.config>/ponytail`` — upstream's own resolver."""
    return Path(upstream()._config_dir())


def config_path() -> Path:
    return config_dir() / "config.json"


def normalize_level(level: str | None) -> str | None:
    """Upstream's normalisation: only off/lite/full/ultra survive, case-insensitive."""
    return upstream()._normalize_runtime_mode(level)


def default_mode() -> str:
    """env ``PONYTAIL_DEFAULT_MODE`` > ``config.json:defaultMode`` > built-in ``full``."""
    return upstream()._default_mode()


def mode_source() -> str:
    """Where :func:`default_mode` came from — ``env``, ``config`` or ``default``."""
    if normalize_level(os.environ.get("PONYTAIL_DEFAULT_MODE")):
        return "env"
    try:
        data = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "default"
    return "config" if normalize_level(data.get("defaultMode")) else "default"


def set_mode(level: str) -> str:
    """Persist ``defaultMode`` in upstream's config file, preserving other keys."""
    normalized = normalize_level(level)
    if normalized is None:
        raise InvalidLevel(
            f"unknown level: {level!r}",
            valid=list(LEVELS),
        )
    path = config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ConfigUnwritable(f"cannot parse existing config: {exc}", path=str(path)) from exc
        if isinstance(loaded, dict):
            data = loaded
    data["defaultMode"] = normalized
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise ConfigUnwritable(f"cannot write config: {exc}", path=str(path)) from exc
    return normalized


# ── upstream content ────────────────────────────────────────────────────────


def rules_for(level: str | None = None) -> str:
    """Exact ``build_injected_context`` output for ``level`` (empty string for off)."""
    if level is not None and normalize_level(level) is None:
        raise InvalidLevel(f"unknown level: {level!r}", valid=list(LEVELS))
    return upstream().build_injected_context(level)


def skill_names() -> list[str]:
    """Upstream skills: ``ponytail`` plus every ``SKILL_COMMANDS`` entry."""
    names = {"ponytail", *upstream().SKILL_COMMANDS.keys()}
    return sorted(names)


def _skill_path(name: str) -> Path:
    root = vendor_root()
    path = root / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise UnknownSkill(f"unknown skill: {name!r}", available=skill_names())
    return path


def skill_summaries() -> list[dict[str, str]]:
    """``[{name, description}]`` straight from each SKILL.md frontmatter.

    ``description`` is the frontmatter text **up to its first sentence** — the
    remainder is upstream's "Use for …" traffic hints, which the injected block
    states more cheaply in one line per skill. Everything here is verbatim
    upstream wording, so it cannot drift from the SKILL.md files.
    """
    summaries: list[dict[str, str]] = []
    root = vendor_root() / "skills"
    for name in skill_names():
        path = root / name / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PonytailError(f"cannot read skill {name!r}: {exc}") from exc
        front = text.split("---")[1] if text.startswith("---") else ""
        match = re.search(r"description:\s*>?-?\s*((?:.|\n)*?)(?=\n[a-z]+:|\Z)", front)
        description = " ".join(match.group(1).split()) if match else ""
        sentences = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)
        summaries.append({"name": name, "description": sentences[0]})
    return summaries


def skill_body(name: str, level: str | None = None) -> str:
    """Frontmatter-stripped, mode-filtered SKILL.md body (upstream's own filter)."""
    if level is not None and normalize_level(level) is None:
        raise InvalidLevel(f"unknown level: {level!r}", valid=list(LEVELS))
    raw = _skill_path(name).read_text(encoding="utf-8")
    effective = level or default_mode()
    return upstream()._filter_skill_body_for_mode(raw, effective)


def skill_frontmatter(name: str) -> dict[str, str]:
    """Minimal frontmatter parse (name/description/license) for display only."""
    raw = _skill_path(name).read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---", raw, re.DOTALL)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def command_names() -> list[str]:
    root = vendor_root() / "commands"
    return sorted(path.stem for path in root.glob("*.toml")) if root.is_dir() else []


def command_definition(name: str, args: str = "") -> dict[str, str]:
    """Parsed ``commands/<name>.toml`` with ``{{args}}`` substituted."""
    path = vendor_root() / "commands" / f"{name}.toml"
    if not path.is_file():
        raise UnknownCommand(f"unknown command: {name!r}", available=command_names())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PonytailError(f"cannot read command {name!r}: {exc}", path=str(path)) from exc
    prompt = str(data.get("prompt", ""))
    return {
        "name": name,
        "description": str(data.get("description", "")),
        "prompt": prompt.replace("{{args}}", args.strip()),
    }


# ── shortcut: debt scan ─────────────────────────────────────────────────────


def scan_shortcut_debt(root: str = ".", limit: int = 50) -> list[dict[str, Any]]:
    """Find ``shortcut:`` comments — upstream's ``ponytail-debt`` contract.

    Upstream expects a ``shortcut:`` comment to name its ceiling and the upgrade
    path; the scanner keeps the raw text plus a best-effort split on ``->``/``;``.
    """
    base = Path(root).expanduser()
    if not base.is_dir():
        raise InvalidPath(f"not a directory: {root!r}", path=str(base))
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".gradle"}
    suffix_ok = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".dart", ".go", ".rs", ".java", ".kt",
        ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".sh", ".mjs", ".cjs",
    }
    found: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if len(found) >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in suffix_ok:
            continue
        if skip_dirs & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            match = _SHORTCUT_RE.search(line)
            if not match:
                continue
            body = match.group("body").strip()
            ceiling, _, upgrade = body.partition("->")
            found.append(
                {
                    "file": str(path.relative_to(base)),
                    "line": number,
                    "ceiling": ceiling.strip(),
                    "upgrade_path": upgrade.strip(),
                    "raw": body,
                }
            )
            if len(found) >= limit:
                break
    return found


# ── doctor ──────────────────────────────────────────────────────────────────


def doctor() -> dict[str, Any]:
    """Self-check: vendor, checksums, content counts, config and mode source."""
    report: dict[str, Any] = {
        "pinned_commit": PINNED_COMMIT,
        "upstream_version": UPSTREAM_VERSION,
    }
    try:
        root = vendor_root()
    except PonytailError as exc:
        report["vendor"] = {"ok": False, "kind": exc.kind, "message": exc.message}
        return report
    meta = read_upstream_meta()
    report["pinned_commit"] = meta.get("commit", PINNED_COMMIT)
    report["content_commit"] = meta.get("content_commit", CONTENT_COMMIT)
    report["upstream_version"] = meta.get("version", UPSTREAM_VERSION)
    report["vendor"] = {"ok": True, "path": str(root)}
    checksums = root / "CHECKSUMS.sha256"
    report["checksums"] = _verify_checksums(root, checksums)
    report["skills"] = len(skill_names())
    report["commands"] = len(command_names())
    report["config_path"] = str(config_path())
    report["mode"] = default_mode()
    report["mode_source"] = mode_source()
    return report


def read_build_label() -> str:
    """Package build label written by ``build_zips.py`` (empty for a dev checkout).

    The plugin version always equals the upstream version, so adapter-only fixes
    are told apart by this label (see octop/README.md).
    """
    try:
        text = (PLUGIN_DIR / "BUILD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.splitlines()[0] if text else ""


def read_upstream_meta() -> dict[str, str]:
    """Parse ``_vendor/ponytail/UPSTREAM_VERSION`` (written by sync_upstream.py)."""
    try:
        path = vendor_root() / "UPSTREAM_VERSION"
    except PonytailError:
        return {}
    meta: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip():
                meta[key.strip()] = value.strip()
    except OSError:
        return {}
    return meta


def _verify_checksums(root: Path, manifest: Path) -> dict[str, Any]:
    if not manifest.is_file():
        return {"ok": False, "kind": "dependency_missing", "message": f"missing {manifest.name}"}
    import hashlib

    bad: list[str] = []
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, relative = line.partition("  ")
        target = root / relative
        if not target.is_file():
            bad.append(f"missing:{relative}")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        checked += 1
        if actual != digest:
            bad.append(f"mismatch:{relative}")
    return {"ok": not bad, "checked": checked, "problems": bad}
