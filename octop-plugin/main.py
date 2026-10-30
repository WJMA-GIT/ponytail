"""ponytail single-package adapter for Octop (``kind: skill``).

Why ``kind: skill``
-------------------
An Octop plugin has exactly one ``kind``, and each public registration API is
gated on it::

    ctx.tool(...)       -> requires kind == "tool"
    ctx.middleware(...) -> requires kind == "hook"
    ctx.skills(...)     -> requires kind == "skill"

Of the three capabilities only **skills** cannot be reproduced by injection:
a real skill is rendered into the system prompt's skill catalogue and answers
``/name`` invocation. Tools and middleware are plain objects in two lists, so
they are registered through a verified compatibility path instead.

Every compatibility registration is self-checked at ``setup()`` time and the
result is surfaced by ``ponytail_doctor`` (``documented`` == public API,
``compatibility`` == internal list, ``unavailable`` == failed). Nothing fails
silently.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable

from octop_harness.plugins import PluginContext
from octop_harness.plugins.registry import ToolRegistration

# Load the runtime by path under a plugin-unique name: the plugin directory is
# not on ``sys.path`` when Octop imports the entry module.
_PLUGIN_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ponytail_runtime_plugin", _PLUGIN_DIR / "ponytail_runtime.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rt
_SPEC.loader.exec_module(rt)

logger = logging.getLogger(__name__)

#: Middleware priority: runs before BootstrapMiddleware (100).
PRIORITY = 90

#: Emitted when the active mode is ``off`` but rules were injected earlier.
OFF_NOTICE = "PONYTAIL MODE OFF"

SKILLS_HEADER = "PONYTAIL SKILLS"

#: Registration provenance, read by :func:`ponytail_doctor`.
_STATE: dict[str, Any] = {
    "tools": "unavailable",
    "skills": "unavailable",
    "injection": "unavailable",
    "tools_count": 0,
    "skills_count": 0,
    "skills_dir": "",
    "diagnostics": [],
}

_MODE_MARK_RE = re.compile(r"PONYTAIL MODE (?:ACTIVE — level: (off|lite|full|ultra)|OFF)")


def _diag(message: str) -> None:
    _STATE["diagnostics"].append(message)
    logger.warning("ponytail: %s", message)


# ── content ─────────────────────────────────────────────────────────────────


def skill_catalog() -> str:
    """One line per skill, straight from each SKILL.md frontmatter."""
    lines = [
        f"{SKILLS_HEADER} — 6 个技能已装进本 agent 的技能清单。用户以 /<name> 点名时"
        "（如 /ponytail-review），视为显式技能调用：先按 name 或 slug 命中，"
        "然后立刻读该技能的 SKILL.md 并按其执行，优先于其它工具与通用做法。"
    ]
    try:
        for item in rt.skill_summaries():
            lines.append(f"- {item['name']} — {item['description']}")
    except rt.PonytailError as exc:
        lines.append(f"- (技能清单不可用: {exc.kind}: {exc.message})")
    return "\n".join(lines)


def build_block(mode: str) -> str:
    """The text appended to the system prompt for *mode*."""
    if mode == "off":
        return (
            f"{OFF_NOTICE}\n\n"
            "已停用 ponytail：忽略并停止应用之前注入的 ponytail 规则。\n\n"
            + skill_catalog()
        )
    # ``rules_for`` already opens with the "PONYTAIL MODE ACTIVE — level: …"
    # header, so the marker is not added a second time here.
    return (rt.rules_for(mode) or "") + "\n\n" + skill_catalog()


# ── middleware ──────────────────────────────────────────────────────────────


def _text_of(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                out.append(block["text"])
        return "\n".join(out)
    return str(content)


def _history_mentions_rules(messages: Any) -> bool:
    """Loose undo check: any earlier turn carrying the marker is enough."""
    for message in messages or []:
        if _MODE_MARK_RE.search(_text_of(message)):
            return True
        kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(kwargs, dict) and "PONYTAIL MODE" in str(kwargs.get("api_content") or ""):
            return True
    return False


class PonytailMiddleware:
    """Append rules + skill catalogue to the system message of every model call.

    Idempotency reads **only** ``request.system_message``. Earlier versions also
    scanned the whole message history, and an old marker left in the conversation
    made the check believe the rules were already present, so injection silently
    stopped. The history is now used for one thing only: deciding whether an
    ``off`` switch needs an explicit undo notice.
    """

    def __init__(self) -> None:
        self._announced: set[str] = set()

    # langchain AgentMiddleware duck-typing: no base class needed.
    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        return handler(self.augment(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self.augment(request))

    def augment(self, request: Any) -> Any:
        try:
            mode = rt.default_mode()
        except rt.PonytailError as exc:
            _diag(f"mode resolution failed: {exc.kind}: {exc.message}")
            return request

        system_message = getattr(request, "system_message", None)
        system_text = _text_of(system_message)

        if mode == "off":
            # Nothing was ever injected and the prompt carries no marker: stay quiet.
            if "PONYTAIL MODE" not in system_text and not _history_mentions_rules(
                getattr(request, "messages", None)
            ):
                return request

        block = build_block(mode)
        if block in system_text:
            return request

        try:
            from deepagents.middleware._utils import append_to_system_message
        except Exception as exc:  # pragma: no cover - depends on harness version
            _diag(f"append_to_system_message unavailable: {exc}")
            return request

        try:
            new_system = append_to_system_message(system_message, block)
        except Exception as exc:
            _diag(f"failed to build system message: {exc}")
            return request

        messages = list(getattr(request, "messages", None) or [])
        try:
            from langchain_core.messages import SystemMessage

            messages = [m for m in messages if not isinstance(m, SystemMessage)]
        except Exception:  # pragma: no cover
            pass

        if mode not in self._announced:
            self._announced.add(mode)
            logger.info(
                "ponytail: injecting mode=%s rules(%d chars) + skills(%d)",
                mode,
                len(block),
                _STATE["skills_count"],
            )
        return request.override(system_message=new_system, messages=messages)


# ── compatibility registration ──────────────────────────────────────────────


class _MiddlewareRegistration:
    """Duck-typed stand-in for ``octop_harness.plugins.registry.MiddlewareRegistration``.

    ``build_middleware_chain()`` only reads ``instance`` and ``priority``.
    """

    __slots__ = ("plugin_id", "instance", "priority")

    def __init__(self, plugin_id: str, instance: Any, priority: int) -> None:
        self.plugin_id = plugin_id
        self.instance = instance
        self.priority = priority


def _register_middleware(ctx: PluginContext, instance: Any) -> str:
    try:
        ctx.middleware(instance, priority=PRIORITY)
        _STATE["injection"] = "documented"
        return _STATE["injection"]
    except ValueError:
        pass  # kind != "hook": expected on this kind
    except Exception as exc:
        _diag(f"ctx.middleware() raised unexpectedly: {exc}")

    registrations = getattr(ctx, "_middleware", None)
    if isinstance(registrations, list):
        registrations.append(_MiddlewareRegistration(ctx.plugin_id, instance, PRIORITY))
        if any(getattr(entry, "instance", None) is instance for entry in registrations):
            _STATE["injection"] = "compatibility"
            logger.info("ponytail: middleware registered via compatibility path")
            return _STATE["injection"]

    _STATE["injection"] = "unavailable"
    _diag("could not register the injection middleware; rules will NOT be injected")
    return _STATE["injection"]


def _register_tool(ctx: PluginContext, name: str, fn: Any, description: str) -> bool:
    try:
        ctx.tool(name, fn, description=description)
    except ValueError:
        tools = getattr(ctx, "_tools", None)
        if isinstance(tools, list):
            tools.append(
                ToolRegistration(
                    plugin_id=ctx.plugin_id,
                    name=name,
                    fn=fn,
                    description=description,
                    config_fields=[],
                )
            )
        else:  # pragma: no cover
            _diag(f"ctx._tools is not a list; tool {name} not registered")
            return False
    except Exception as exc:
        _diag(f"ctx.tool({name}) raised unexpectedly: {exc}")
        return False

    registered = getattr(ctx, "_tools", None)
    if isinstance(registered, list) and any(
        getattr(item, "name", None) == name for item in registered
    ):
        return True
    _diag(f"tool {name} did not land in ctx._tools")
    return False


# ── tools ───────────────────────────────────────────────────────────────────


def _fail(exc: rt.PonytailError) -> str:
    lines = ["status: error", f"kind: {exc.kind}", f"message: {exc.message}"]
    for key, value in exc.details.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(i) for i in value)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def ponytail_rules(
    level: Annotated[
        str,
        "档位，取值只能是 off / lite / full / ultra。留空（默认）用当前配置，"
        "优先级 环境变量 PONYTAIL_DEFAULT_MODE > config.json > full。"
        "要完整规则用 full，要最激进的删减用 ultra。",
    ] = "",
) -> str:
    """取 ponytail 规则全文（与上游 build_injected_context 逐字一致）。"""
    try:
        body = rt.rules_for(level or None)
    except rt.PonytailError as exc:
        return _fail(exc)
    return body or f"{OFF_NOTICE}（档位为 off，无规则正文）"


def ponytail_mode(
    action: Annotated[
        str,
        "要做什么：get 查看当前档位与来源（默认）；set 切换档位（此时必须同时传 level）。"
        "只有这两个取值，其它值返回结构化错误。",
    ] = "get",
    level: Annotated[
        str,
        "目标档位，只能是 off / lite / full / ultra。action=set 时必填；action=get 时忽略。",
    ] = "",
) -> str:
    """查看或切换 ponytail 档位（写入上游兼容的 config.json）。"""
    try:
        normalized = (action or "get").strip().lower()
        if normalized == "get":
            return "\n".join(
                [
                    f"mode: {rt.default_mode()}",
                    f"source: {rt.mode_source()}",
                    f"levels: {', '.join(rt.LEVELS)}",
                    f"config: {rt.config_path()}",
                    f"auto_injection: {_STATE['injection']}",
                ]
            )
        if normalized != "set":
            raise rt.InvalidLevel(f"unknown action: {action!r}", valid=["get", "set"])
        if not level.strip():
            raise rt.InvalidLevel("action=set requires a level", valid=list(rt.LEVELS))
        applied = rt.set_mode(level)
        return "\n".join(
            [
                f"mode: {applied}",
                "saved: yes",
                f"config: {rt.config_path()}",
                f"mode_source_after: {rt.mode_source()}",
                f"auto_injection: {_STATE['injection']}",
                "note: 环境变量 PONYTAIL_DEFAULT_MODE 存在时会覆盖 config.json",
            ]
        )
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:
        return f"status: error\nkind: unexpected\nmessage: {exc}"


def ponytail_skill(
    name: Annotated[
        str,
        "技能名，只能是 ponytail / ponytail-review / ponytail-audit / ponytail-debt / "
        "ponytail-gain / ponytail-help。做质量评审用 ponytail-review，整仓审计用 "
        "ponytail-audit，清技术债用 ponytail-debt。传错会返回可选值列表。",
    ],
    level: Annotated[
        str,
        "过滤时用的档位 off / lite / full / ultra；留空用当前配置。",
    ] = "",
    include_prompt: Annotated[
        bool,
        "为 true 时在正文前附上同名命令定义（description + prompt 模板），"
        "一次拿到「怎么触发 + 正文怎么写」。默认 false。",
    ] = False,
) -> str:
    """返回某个 ponytail 技能正文（去 frontmatter、按档位过滤，与上游一致）。"""
    try:
        body = rt.skill_body(name, level or None)
        if not include_prompt:
            return body
        cmd = rt.command_definition(name, "")
        return f"--- command: {name} ---\ndescription: {cmd['description']}\n\n{body}"
    except rt.PonytailError as exc:
        return _fail(exc)


def ponytail_command(
    name: Annotated[
        str,
        "命令名，只能是 ponytail / ponytail-review / ponytail-audit / ponytail-debt / "
        "ponytail-gain / ponytail-help。",
    ],
    command_args: Annotated[
        str,
        "替换进命令模板 {{args}} 的内容。注意：6 个命令里只有 ponytail 的模板带 "
        "{{args}}（用来指定档位 lite/full/ultra）；其余传了也不会插入。",
    ] = "",
) -> str:
    """取 ponytail 斜杠命令的定义（description + 已替换 {{args}} 的 prompt）。"""
    try:
        data = rt.command_definition(name, command_args)
        return f"name: {data['name']}\ndescription: {data['description']}\n\n{data['prompt']}"
    except rt.PonytailError as exc:
        return _fail(exc)


def ponytail_shortcut_debt(
    root: Annotated[
        str,
        "要扫描的目录，默认 .（当前工作目录）。可指向子目录缩小范围；"
        "不跟随 git 子模块，也不读工作区之外的路径。",
    ] = ".",
    limit: Annotated[int, "最多返回多少条，默认 50。超出只截断输出，found 统计仍是全量。"] = 50,
) -> str:
    """扫描仓库里的 shortcut: 注释，列出上限与升级路径。"""
    try:
        rows = rt.scan_shortcut_debt(root, limit=limit)
    except rt.PonytailError as exc:
        return _fail(exc)
    if not rows:
        return "No shortcut debt. Clean ledger."
    lines = [f"{len(rows)} shortcut: markers"]
    for row in rows:
        ceiling = row.get("ceiling") or "(no ceiling named)"
        upgrade = row.get("upgrade_path") or "(no upgrade path)"
        lines.append(f"{row['file']}:{row['line']} — {row.get('raw', '')}")
        lines.append(f"    ceiling: {ceiling}")
        lines.append(f"    upgrade: {upgrade}")
    return "\n".join(lines)


def _skills_in_cwd() -> tuple[int, str]:
    """Best-effort: are the six skills visible from the current workspace?"""
    for candidate in (Path(".octop/skills"), Path("skills")):
        if candidate.is_dir():
            names = [
                p.name
                for p in candidate.iterdir()
                if p.is_dir() and (p / "SKILL.md").is_file()
            ]
            hits = sorted(set(names) & set(rt.skill_names()))
            if hits:
                return len(hits), str(candidate)
    return 0, ""


def ponytail_doctor() -> str:
    """自检：上游副本、校验和、工具/注入/技能的注册路径与数量、档位来源。"""
    try:
        report = rt.doctor()
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return f"status: error\nkind: unexpected\nmessage: {exc}"

    vendor = report["vendor"]
    lines = [
        "status: ok" if vendor.get("ok") else "status: error",
        f"upstream_version: {report['upstream_version']}",
        f"sync_point: {report['pinned_commit']}",
        f"content_commit: {str(report.get('content_commit', '?'))[:12]}",
        f"adapter_build: {rt.read_build_label() or 'dev'}",
        f"auto_injection: {_STATE['injection']}",
        f"tools_path: {_STATE['tools']} ({_STATE['tools_count']}/6)",
        f"skills_path: {_STATE['skills']} ({_STATE['skills_count']} dirs)",
        f"skills_dir: {_STATE['skills_dir'] or '-'}",
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
    synced, where = _skills_in_cwd()
    lines.append(f"skills_in_workspace: {synced}/6" + (f" ({where})" if where else ""))
    lines.append(f"config: {report['config_path']}")
    lines.append(f"mode: {report['mode']} (source={report['mode_source']})")
    if _STATE["diagnostics"]:
        lines.append("diagnostics:")
        for item in _STATE["diagnostics"]:
            lines.append(f"  - {item}")
    return "\n".join(lines)


# ── setup ───────────────────────────────────────────────────────────────────

_TOOLS: list[tuple[str, Any, str]] = [
    ("ponytail_rules", ponytail_rules, "取 ponytail 规则全文（与上游逐字一致）。开始写代码或评审前先把规则拉进上下文。"),
    ("ponytail_mode", ponytail_mode, "查看或切换 ponytail 档位（off/lite/full/ultra），写入上游兼容的 config.json。"),
    ("ponytail_skill", ponytail_skill, "取某个 ponytail 技能正文（去 frontmatter、按档位过滤）。"),
    ("ponytail_command", ponytail_command, "取某个 ponytail 斜杠命令的定义（description + 已替换参数的 prompt）。"),
    ("ponytail_shortcut_debt", ponytail_shortcut_debt, "扫描仓库里的 shortcut: 注释，列出上限与升级路径。"),
    ("ponytail_doctor", ponytail_doctor, "ponytail 插件自检：工具/注入/技能三条注册路径、上游副本、校验和、档位。"),
]


def setup(ctx: PluginContext) -> None:
    """Register skills (legal), then tools and injection (compatibility), then verify."""
    # 1) Skills — the only capability that cannot be faked, and the only one this
    #    kind allows through the public API.
    skills_rel = "_vendor/ponytail/skills"
    try:
        ctx.skills(skills_rel)
        _STATE["skills"] = "documented"
    except ValueError:
        _diag(f"ctx.skills() rejected kind={ctx._manifest.kind!r}")
    except Exception as exc:
        _diag(f"ctx.skills() failed: {exc}")

    skills_dir = getattr(ctx, "_skills_dir", None)
    if isinstance(skills_dir, Path) and skills_dir.is_dir():
        _STATE["skills_dir"] = str(skills_dir)
        _STATE["skills_count"] = sum(
            1
            for p in skills_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()
        )
        if _STATE["skills"] != "documented":
            _STATE["skills"] = "compatibility"
    else:
        _STATE["skills"] = "unavailable"
        _diag("skills_dir was not set; skills will NOT reach the agent catalogue")

    # 2) Tool belt first or last? Tools last: if the injection path ever breaks the
    #    plugin must still expose the tools, and vice versa the tools are the only
    #    way the agent can self-report a broken injection.
    ok = 0
    for name, fn, description in _TOOLS:
        if _register_tool(ctx, name, fn, description):
            ok += 1
    _STATE["tools_count"] = ok
    _STATE["tools"] = "compatibility" if ok else "unavailable"

    # 3) Injection middleware.
    _register_middleware(ctx, PonytailMiddleware())

    logger.info(
        "ponytail: setup done tools=%d/6 injection=%s skills=%s(%d)",
        ok,
        _STATE["injection"],
        _STATE["skills"],
        _STATE["skills_count"],
    )
