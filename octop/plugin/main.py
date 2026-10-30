"""Octop plugin: ponytail — tools **and** always-on rules injection (kind: tool).

Two capabilities, one plugin:

* **tools** — ``ponytail_rules`` / ``ponytail_mode`` / ``ponytail_skill`` /
  ``ponytail_command`` / ``ponytail_shortcut_debt`` / ``ponytail_doctor``.
* **middleware** — ``PonytailMiddleware`` appends the ruleset to the system
  message of every model call, which is ponytail's native behaviour (upstream
  registers a ``pre_llm_call`` hook).

Why one plugin can carry both
-----------------------------
``PluginContext.middleware()`` refuses to run when ``manifest.kind != "hook"``
(``octop_harness/plugins/context.py:63-64``) — and ``tool()`` likewise refuses
when kind != ``tool`` (``context.py:47-48``). Nothing downstream filters by kind:
``PluginContext.to_loaded()`` copies ``_tools`` / ``_middleware`` verbatim
(``context.py:152-160``), ``PluginRegistry.all_tools()`` (``registry.py:126-130``)
and ``build_middleware_chain()`` (``registry.py:146-154``) only read those lists.

So the plugin registers tools through the documented API, and for the middleware
it tries the documented call first (works if a future Octop relaxes the guard)
and otherwise falls back to the same registration object appended to the
context's own list. The fallback is verified immediately, logged, and reported by
``ponytail_doctor`` so it can never fail silently.

Upstream files are never modified — this module imports the vendored copy of
ponytail's ``__init__.py`` through :mod:`ponytail_runtime`.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from octop_harness.plugins import PluginContext

# Load the shared runtime by path under a plugin-unique name.
_PLUGIN_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ponytail_runtime_plugin", _PLUGIN_DIR / "ponytail_runtime.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rt
_SPEC.loader.exec_module(rt)

logger = logging.getLogger(__name__)

#: Same marker upstream's ``_pre_llm_call`` scans for.
_MARKER = re.compile(r"PONYTAIL MODE (?:ACTIVE — level: ([a-z]+)|OFF)")

#: Upstream sends this when the mode is off but rules were injected earlier.
OFF_NOTICE = "PONYTAIL MODE OFF"

#: Runs before BootstrapMiddleware (priority 100) so onboarding text lands last.
PRIORITY = 90

#: How the middleware got registered; surfaced by ``ponytail_doctor``.
_HOOK_STATE = "not-registered"


# ── middleware ──────────────────────────────────────────────────────────────


def _message_texts(message: Any) -> list[str]:
    texts: list[str] = []
    content = getattr(message, "content", "")
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
    kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(kwargs, dict):
        api_content = kwargs.get("api_content")
        if isinstance(api_content, str):
            texts.append(api_content)
    return texts


def injected_mode(messages: Any) -> str | None:
    """Return the last ponytail level already present in ``messages`` (or None)."""
    for message in reversed(list(messages or [])):
        for text in _message_texts(message):
            matches = _MARKER.findall(text)
            if matches:
                return matches[-1] or "off"
    return None


class PonytailMiddleware(AgentMiddleware[Any, Any]):
    """Append the ponytail ruleset to the system message of every model call."""

    def __init__(self, *, mixin: bool = True) -> None:
        super().__init__()
        self._announced: set[str] = set()
        self._enabled = mixin

    # ── model-call hooks ────────────────────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self.augment(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self.augment(request))

    # ── context injection (upstream-equivalent) ─────────────────────────────

    def context_for(self, mode: str) -> str | None:
        """Ruleset for ``mode``; ``None`` when the vendor is unusable."""
        try:
            return rt.rules_for(mode) or OFF_NOTICE
        except rt.PonytailError as exc:
            if mode not in self._announced:
                self._announced.add(mode)
                logger.warning("ponytail: vendored content unavailable (%s: %s)", exc.kind, exc.message)
            return None

    def augment(self, request: ModelRequest) -> ModelRequest:
        if not self._enabled:
            return request
        try:
            mode = rt.default_mode()
        except rt.PonytailError as exc:
            if "mode" not in self._announced:
                self._announced.add("mode")
                logger.warning("ponytail: mode resolution failed (%s: %s)", exc.kind, exc.message)
            return request
        seen = injected_mode(getattr(request, "messages", None))
        if seen == mode:
            return request
        if seen is None and mode == "off":
            return request
        context = self.context_for(mode)
        if not context:
            return request
        if mode not in self._announced:
            self._announced.add(mode)
            logger.info("ponytail: injecting ruleset for mode=%s (%d chars)", mode, len(context))
        messages = [
            message
            for message in (getattr(request, "messages", None) or [])
            if not isinstance(message, SystemMessage)
        ]
        return request.override(
            system_message=append_to_system_message(request.system_message, context),
            messages=messages,
        )


# ── registration ────────────────────────────────────────────────────────────


@dataclass
class _MiddlewareRegistration:
    """Duck-typed stand-in for ``octop_harness.plugins.registry.MiddlewareRegistration``.

    ``build_middleware_chain()`` only reads ``instance`` and ``priority``, so the
    fallback path does not need the private class at all.
    """

    plugin_id: str
    instance: Any
    priority: int = 100


def _middleware_registration(plugin_id: str, instance: Any) -> Any:
    try:  # prefer the real class when it is importable
        from octop_harness.plugins.registry import (  # type: ignore[attr-defined]
            MiddlewareRegistration,
        )

        return MiddlewareRegistration(plugin_id=plugin_id, instance=instance, priority=PRIORITY)
    except Exception:  # pragma: no cover - defensive
        return _MiddlewareRegistration(plugin_id=plugin_id, instance=instance, priority=PRIORITY)


def register_middleware(ctx: PluginContext, instance: Any) -> str:
    """Register ``instance`` as agent middleware; return how it was done.

    Returns one of ``"documented"`` (``ctx.middleware`` accepted it),
    ``"compatibility"`` (appended to the context's registration list) or
    ``"unavailable"`` (neither worked — logged, and reported by the doctor tool).
    """
    global _HOOK_STATE
    try:
        ctx.middleware(instance, priority=PRIORITY)
        _HOOK_STATE = "documented"
        return _HOOK_STATE
    except ValueError:
        pass  # kind != hook: expected on Octop <= current
    except Exception as exc:  # pragma: no cover - unexpected API change
        logger.error("ponytail: ctx.middleware() failed unexpectedly: %s", exc)

    registrations = getattr(ctx, "_middleware", None)
    if isinstance(registrations, list):
        registrations.append(_middleware_registration(ctx.plugin_id, instance))
        if any(getattr(entry, "instance", None) is instance for entry in registrations):
            _HOOK_STATE = "compatibility"
            logger.info(
                "ponytail: middleware registered through the compatibility path "
                "(kind=%s; ctx.middleware() refuses non-hook plugins)",
                getattr(getattr(ctx, "_manifest", None), "kind", "?"),
            )
            return _HOOK_STATE
    _HOOK_STATE = "unavailable"
    logger.error(
        "ponytail: could not register the rules-injection middleware; "
        "tools still work, automatic injection is OFF (see ponytail_doctor)"
    )
    return _HOOK_STATE


def hook_state() -> str:
    """How the middleware was registered (``documented`` / ``compatibility`` / …)."""
    return _HOOK_STATE


# ── tools ───────────────────────────────────────────────────────────────────


def _fail(exc: rt.PonytailError) -> str:
    lines = ["status: error", f"kind: {exc.kind}", f"message: {exc.message}"]
    for key, value in exc.details.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _unexpected(exc: Exception) -> str:
    return _fail(rt.PonytailError(f"{type(exc).__name__}: {exc}"))


def ponytail_rules(
    level: Annotated[
        str,
        "档位，取值只能是 off / lite / full / ultra。留空（默认）用当前配置，"
        "优先级 环境变量 PONYTAIL_DEFAULT_MODE > config.json 的 defaultMode > full。"
        "写代码或评审前要锁定强度就显式传 full 或 ultra；传其它值返回结构化错误。",
    ] = "",
) -> str:
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


def ponytail_mode(
    action: Annotated[
        str,
        "要做什么：get 查看当前档位与来源（默认）；set 切换档位（此时必须同时传 "
        "level）。只有这两个取值，其它值返回结构化错误而不是抛异常。",
    ] = "get",
    level: Annotated[
        str,
        "目标档位，取值只能是 off / lite / full / ultra。action=set 时必填；"
        "action=get 时忽略。写错会返回 kind=invalid_level 并列出合法值。",
    ] = "",
) -> str:
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
                    f"auto_injection: {hook_state()}",
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
                f"auto_injection: {hook_state()}",
                "note: 环境变量 PONYTAIL_DEFAULT_MODE 存在时会覆盖 config.json",
            ]
        )
    except rt.PonytailError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover
        return _unexpected(exc)


def ponytail_skill(
    name: Annotated[
        str,
        "技能名，取值只能是 ponytail / ponytail-review / ponytail-audit / "
        "ponytail-debt / ponytail-gain / ponytail-help。要做质量评审传 "
        "ponytail-review，安全与边界审查传 ponytail-audit，清技术债传 "
        "ponytail-debt。传错返回 kind=unknown_skill 并列出现有技能名。",
    ],
    level: Annotated[
        str,
        "过滤时用的档位，取值 off / lite / full / ultra；留空用当前配置"
        "（与 ponytail_mode get 显示的一致）。",
    ] = "",
    include_prompt: Annotated[
        bool,
        "为 true 时在技能正文前附上同名命令的定义（description + prompt），"
        "一次拿到「怎么触发 + 正文怎么写」；默认 false 只返回技能正文。",
    ] = False,
) -> str:
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


def ponytail_command(
    name: Annotated[
        str,
        "命令名，取值只能是 ponytail / ponytail-review / ponytail-audit / "
        "ponytail-debt / ponytail-gain / ponytail-help。它对应上游的斜杠命令 "
        "/<name>，传错返回 kind=unknown_command 并列出合法名。",
    ],
    # 不叫 args：pydantic 会把名为 args 的参数改名成 v__args（数组），
    # 模型既看不到说明也传不进来 —— 上游斜杠命令的 {{args}} 就断了。
    command_args: Annotated[
        str,
        "替换进命令模板 {{args}} 的内容。注意：6 个命令里只有 ponytail 的模板"
        "带 {{args}}（用来指定档位 lite/full/ultra），其余 5 个模板没有占位符，"
        "传了也不会插入——那些命令把要审的对象靠对话上下文表达。留空即替换成空串。"
        "拿到返回的 prompt 后应立即按它执行，而不是复述给用户。",
    ] = "",
) -> str:
    """返回 ponytail 斜杠命令的定义（描述 + 已替换 {{args}} 的提示词）。

    返回的是可执行的提示词本体：调用后应立刻按 prompt 的要求去做（例如真的去
    读代码并给 findings），不要只把它转述给用户。

    Args:
        name: 命令名，`ponytail` / `ponytail-review` / `ponytail-audit` /
            `ponytail-debt` / `ponytail-gain` / `ponytail-help`。
        command_args: 传给命令的参数（如要审阅的路径或补充说明），可为空。
    """
    try:
        definition = rt.command_definition((name or "").strip(), command_args)
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


def ponytail_shortcut_debt(
    root: Annotated[
        str,
        "要扫描的目录，默认 .（当前工作目录）。可以指向子目录缩小范围；"
        "不会跟随 git 子模块，也不读工作区之外的路径。",
    ] = ".",
    limit: Annotated[
        int,
        "最多返回多少条，默认 50。超出只截断输出，found 统计值仍是全量。",
    ] = 50,
) -> str:
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
    """自检：vendored 上游副本、校验和、技能/命令数量、配置路径、档位来源与注入状态。

    额外报告工作区 ``skills/`` 里是否已落地 6 个技能（落地后模型能在系统提示的
    技能清单里直接看到它们，也能用 /ponytail-review 点名）。
    """
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
        f"sync_point: {report['pinned_commit']}",
        f"content_commit: {str(report.get('content_commit', '?'))[:12]}",
        f"adapter_build: {rt.read_build_label() or 'dev'}",
        f"auto_injection: {hook_state()}",
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
    workspace_found, workspace_expected = _workspace_skills()
    lines.append(
        f"workspace_skills: {workspace_found}/{workspace_expected}"
        + ("" if workspace_found == workspace_expected else " (run install_skills.py --workspace .)")
    )
    lines.append(f"commands: {report['commands']}")
    lines.append(f"config: {report['config_path']}")
    lines.append(f"mode: {report['mode']} (source={report['mode_source']})")
    return "\n".join(lines)


def _workspace_skills() -> tuple[int, int]:
    """``(installed, expected)`` for the six skills under the agent workspace."""
    root = Path.cwd() / "skills"
    expected = rt.skill_names()
    found = sum(1 for name in expected if (root / name / "SKILL.md").is_file())
    return found, len(expected)


def setup(ctx: PluginContext) -> None:
    """Register the six tools and the rules-injection middleware."""
    # Middleware first: if it ever fails, the tools below still load.
    register_middleware(ctx, PonytailMiddleware())

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
        description="ponytail 插件自检：上游副本、校验和、技能/命令数量、配置、档位与注入状态。",
    )
