"""Octop tool plugin: ponytail (kind: tool).

Exposes upstream ponytail's content as *pullable* tools: the model asks for the
ruleset / a skill body / a command prompt / the shortcut debt of a repo.

Upstream files are never modified — this module imports the vendored copy of
ponytail's ``__init__.py`` through :mod:`ponytail_runtime`.

Every tool returns plain structured text. Failures come back as
``status: error`` + ``kind:`` + ``message:`` instead of a traceback, so the model
can tell "dependency missing" from "bad path" from "upstream blew up".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from octop_harness.plugins import PluginContext

# Load the shared runtime by path under a plugin-unique name: Octop loads each
# plugin as its own package, and both plugins ship an identical copy.
_PLUGIN_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ponytail_runtime_tool", _PLUGIN_DIR / "ponytail_runtime.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rt
_SPEC.loader.exec_module(rt)


def _fail(exc: rt.PonytailError) -> str:
    lines = ["status: error", f"kind: {exc.kind}", f"message: {exc.message}"]
    for key, value in exc.details.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _unexpected(exc: Exception) -> str:
    return _fail(rt.PonytailError(f"{type(exc).__name__}: {exc}"))


def ponytail_rules(level: str = "") -> str:
    """返回 ponytail 规则全文（与上游 build_injected_context 逐字一致）。

    Args:
        level: 档位，可选 `off` / `lite` / `full` / `ultra`；留空则用当前配置
            （环境变量 PONYTAIL_DEFAULT_MODE > config.json 的 defaultMode > full）。
    """
    try:
        return rt.rules_for(level or None)
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover - upstream surprise
        return _unexpected(exc)


def ponytail_mode(action: str = "get", level: str = "") -> str:
    """查看或切换 ponytail 档位（写入上游兼容的 config.json）。

    Args:
        action: `get` 查看当前档位与来源，`set` 切换档位。
        level: action=set 时必填，取值 `off` / `lite` / `full` / `ultra`。
    """
    try:
        normalized = (action or "get").strip().lower()
        if normalized == "get":
            return "\n".join(
                [
                    f"mode: {rt.default_mode()}",
                    f"source: {rt.mode_source()}",
                    f"levels: {', '.join(rt.LEVELS)}",
                    f"config: {rt.config_path()}",
                ]
            )
        if normalized != "set":
            raise rt.InvalidLevel(
                f"unknown action: {action!r}",
                valid=["get", "set"],
            )
        if not level.strip():
            raise rt.InvalidLevel("action=set requires a level", valid=list(rt.LEVELS))
        applied = rt.set_mode(level)
        return "\n".join(
            [
                f"mode: {applied}",
                "saved: yes",
                f"config: {rt.config_path()}",
                f"mode_source_after: {rt.mode_source()}",
                "note: 环境变量 PONYTAIL_DEFAULT_MODE 存在时会覆盖 config.json",
            ]
        )
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return _unexpected(exc)


def ponytail_skill(name: str, level: str = "", include_prompt: bool = False) -> str:
    """返回某个 ponytail 技能的正文（去 frontmatter、按档位过滤，与上游一致）。

    Args:
        name: 技能名，`ponytail` / `ponytail-review` / `ponytail-audit` /
            `ponytail-debt` / `ponytail-gain` / `ponytail-help`。
        level: 过滤所用档位，留空用当前配置。
        include_prompt: 为 true 时在正文前附上该技能对应的命令定义。
    """
    try:
        body = rt.skill_body((name or "").strip(), level or None)
        if not include_prompt:
            return body
        key = (name or "").strip()
        if key not in rt.command_names():
            return body
        definition = rt.command_definition(key)
        header = (
            f"command: {key}\n"
            f"description: {definition['description']}\n"
            f"prompt: {definition['prompt']}\n---\n"
        )
        return f"{header}{body}"
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return _unexpected(exc)


def ponytail_command(name: str, args: str = "") -> str:
    """返回 ponytail 斜杠命令的定义（描述 + 已替换 {{args}} 的提示词）。

    Args:
        name: 命令名，`ponytail` / `ponytail-review` / `ponytail-audit` /
            `ponytail-debt` / `ponytail-gain` / `ponytail-help`。
        args: 传给命令的参数（如要审阅的路径或补充说明），可为空。
    """
    try:
        definition = rt.command_definition((name or "").strip(), args)
        return "\n".join(
            [
                f"command: {definition['name']}",
                f"description: {definition['description']}",
                "prompt:",
                definition["prompt"],
            ]
        )
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return _unexpected(exc)


def ponytail_shortcut_debt(root: str = ".", limit: int = 50) -> str:
    """扫描仓库里所有 `shortcut:` 注释，列出上限与升级路径。

    Args:
        root: 要扫描的目录，默认当前工作目录。
        limit: 最多返回多少条，默认 50。
    """
    try:
        items = rt.scan_shortcut_debt(root or ".", int(limit))
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return _unexpected(exc)
    if not items:
        return "\n".join(
            [
                "status: ok",
                f"root: {root}",
                "found: 0",
                "hint: 没有找到 shortcut: 注释；上游要求在故意留坑处写 "
                "`shortcut: <上限> -> <升级路径>`",
            ]
        )
    lines = ["status: ok", f"root: {root}", f"found: {len(items)}"]
    for index, item in enumerate(items, start=1):
        lines.append(
            f"{index}. {item['file']}:{item['line']} | "
            f"ceiling={item['ceiling'] or '(未写)'} | "
            f"upgrade={item['upgrade_path'] or '(未写)'}"
        )
    return "\n".join(lines)


def ponytail_doctor() -> str:
    """自检：vendored 上游副本、校验和、技能/命令数量、配置路径与档位来源。"""
    try:
        report = rt.doctor()
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return _unexpected(exc)
    vendor: dict[str, Any] = report["vendor"]
    lines = [
        "status: ok" if vendor.get("ok") else "status: error",
        f"upstream_version: {report['upstream_version']}",
        f"pinned_commit: {report['pinned_commit']}",
    ]
    if not vendor.get("ok"):
        lines.append(f"vendor_kind: {vendor.get('kind')}")
        lines.append(f"vendor_message: {vendor.get('message')}")
        return "\n".join(lines)
    lines.append(f"vendor_path: {vendor['path']}")
    checks = report["checksums"]
    lines.append(f"checksums_ok: {'yes' if checks.get('ok') else 'no'}")
    if not checks.get("ok"):
        problems = checks.get("problems") or [checks.get("message", "unknown")]
        lines.append(f"checksums_problems: {', '.join(str(p) for p in problems)}")
    lines.append(f"skills: {report['skills']}")
    lines.append(f"commands: {report['commands']}")
    lines.append(f"config: {report['config_path']}")
    lines.append(f"mode: {report['mode']} (source={report['mode_source']})")
    return "\n".join(lines)


def setup(ctx: PluginContext) -> None:
    """Register every ponytail capability as an Octop tool."""
    ctx.tool(
        "ponytail_rules",
        ponytail_rules,
        description="取 ponytail 规则全文（与上游逐字一致）。开始写代码或评审前先把规则拉进上下文。",
    )
    ctx.tool(
        "ponytail_mode",
        ponytail_mode,
        description="查看或切换 ponytail 档位（off/lite/full/ultra），写入上游兼容的 config.json。",
    )
    ctx.tool(
        "ponytail_skill",
        ponytail_skill,
        description="取某个 ponytail 技能正文（review/audit/debt/gain/help/ponytail），可按档位过滤。",
    )
    ctx.tool(
        "ponytail_command",
        ponytail_command,
        description="取某个 ponytail 斜杠命令的定义（描述 + 已替换参数的命令提示词）。",
    )
    ctx.tool(
        "ponytail_shortcut_debt",
        ponytail_shortcut_debt,
        description="扫描仓库里的 shortcut: 注释，列出上限与升级路径（ponytail-debt 的数据面）。",
    )
    ctx.tool(
        "ponytail_doctor",
        ponytail_doctor,
        description="ponytail 插件自检：上游副本、校验和、技能/命令数量、配置与档位来源。",
    )
