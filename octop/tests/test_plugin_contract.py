"""Plugin-contract tests: real ``ctx.tool`` + ``ctx.middleware`` registration.

Proves the merged package (one ``kind: tool`` plugin) registers **both** tools and
the always-on middleware, and that the middleware really injects upstream's text.

These need the Octop runtime (``octop_harness`` / ``langchain``), so they skip
cleanly under a plain ``python3``::

    /app/.venv/bin/python -m unittest discover -s octop/tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
OCTOP_DIR = TESTS_DIR.parent
PLUGIN_DIR = OCTOP_DIR / "plugin"

sys.path.insert(0, str(PLUGIN_DIR))

try:  # pragma: no cover - environment dependent
    from octop_harness.plugins import PluginContext
    from octop_harness.plugins.manifest import PluginManifest
    from octop_harness.plugins.registry import PluginRegistry

    HAS_OCTOP = True
except Exception:  # pragma: no cover
    PluginContext = None  # type: ignore[assignment]
    PluginManifest = None  # type: ignore[assignment]
    PluginRegistry = None  # type: ignore[assignment]
    HAS_OCTOP = False

try:  # pragma: no cover - environment dependent
    from langchain_core.messages import HumanMessage, SystemMessage

    HAS_LANGCHAIN = True
except Exception:  # pragma: no cover
    HAS_LANGCHAIN = False


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeRequest:
    """Minimal stand-in for ``langchain.agents.middleware.ModelRequest``."""

    messages: list[Any] = field(default_factory=list)
    system_message: Any = None

    def override(self, **kwargs: Any) -> "FakeRequest":
        return FakeRequest(
            messages=list(kwargs.get("messages", self.messages)),
            system_message=kwargs.get("system_message", self.system_message),
        )


def _system_text(request: FakeRequest) -> str:
    message = request.system_message
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


@unittest.skipUnless(HAS_OCTOP, "octop_harness not importable (run with the Octop venv python)")
class MergedRegistrationTests(unittest.TestCase):
    """One ``kind: tool`` plugin must end up with tools *and* middleware."""

    def setUp(self) -> None:
        self.module = _load(PLUGIN_DIR / "main.py", "ponytail_plugin_contract")
        manifest = PluginManifest.from_dict(
            {
                "id": "ponytail",
                "version": "5.3.3",
                "name": "ponytail",
                "kind": "tool",
                "entry": "main.py",
            }
        )
        self.ctx = PluginContext(manifest=manifest, source_path=PLUGIN_DIR)

    def test_setup_registers_six_tools(self) -> None:
        self.module.setup(self.ctx)
        names = [entry.name for entry in self.ctx._tools]  # noqa: SLF001
        self.assertEqual(
            names,
            [
                "ponytail_rules",
                "ponytail_mode",
                "ponytail_skill",
                "ponytail_command",
                "ponytail_shortcut_debt",
                "ponytail_doctor",
            ],
        )
        for entry in self.ctx._tools:  # noqa: SLF001
            self.assertTrue(entry.description, entry.name)

    def test_setup_also_registers_the_middleware(self) -> None:
        self.module.setup(self.ctx)
        middleware = self.ctx._middleware  # noqa: SLF001
        self.assertEqual(len(middleware), 1, "middleware must be registered too")
        self.assertIsInstance(middleware[0].instance, self.module.PonytailMiddleware)
        self.assertEqual(middleware[0].priority, self.module.PRIORITY)
        self.assertEqual(self.module.hook_state(), "compatibility")

    def test_registry_sees_both_surfaces(self) -> None:
        PluginRegistry().clear()
        self.addCleanup(PluginRegistry().clear)
        from octop_harness.plugins.context import register_loaded

        self.module.setup(self.ctx)
        register_loaded(self.ctx)
        registry = PluginRegistry()
        loaded = registry.get("ponytail")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.tools), 6, "tools survived the kind check bypass")
        self.assertEqual(len(loaded.middleware), 1, "middleware survived the kind check bypass")
        self.assertIn(
            "PonytailMiddleware",
            [type(item).__name__ for item in registry.build_middleware_chain()],
        )

    def test_every_tool_parameter_documents_itself(self) -> None:
        """The platform builds tools with ``parse_docstring=False``.

        Only ``Annotated[type, "..."]`` survives into the JSON schema, so every
        parameter must carry one — otherwise the model sees a bare ``string``
        and guesses. A missing description turns this test red.
        """
        from octop_harness.plugins.tools import _to_structured_tool  # noqa: PLC0415

        self.module.setup(self.ctx)
        seen: list[str] = []
        for entry in self.ctx._tools:  # noqa: SLF001
            tool = _to_structured_tool(entry)
            for name, spec in tool.args.items():
                seen.append(f"{entry.name}.{name}")
                self.assertTrue(
                    str(spec.get("description", "")).strip(),
                    f"{entry.name}.{name} has no schema description",
                )
        # 6 tools / 10 params: a new undocumented parameter must not slip in.
        self.assertEqual(
            sorted(seen),
            sorted(
                [
                    "ponytail_rules.level",
                    "ponytail_mode.action",
                    "ponytail_mode.level",
                    "ponytail_skill.name",
                    "ponytail_skill.level",
                    "ponytail_skill.include_prompt",
                    "ponytail_command.name",
                    "ponytail_command.command_args",
                    "ponytail_shortcut_debt.root",
                    "ponytail_shortcut_debt.limit",
                ]
            ),
        )

    def test_command_args_actually_reach_the_template(self) -> None:
        """``args`` was renamed to ``command_args``: pydantic turns a parameter
        named ``args`` into ``v__args`` (array), which the model could never
        fill — the upstream ``{{args}}`` substitution was dead."""
        from octop_harness.plugins.tools import _to_structured_tool  # noqa: PLC0415

        self.module.setup(self.ctx)
        entry = next(e for e in self.ctx._tools if e.name == "ponytail_command")  # noqa: SLF001
        tool = _to_structured_tool(entry)
        self.assertIn("command_args", tool.args)
        self.assertNotIn("v__args", tool.args)
        # 只有 ponytail.toml 的模板带 {{args}}（档位），其余 5 个没有占位符
        out = tool.invoke({"name": "ponytail", "command_args": "ultra"})
        self.assertIn("Switch to ponytail ultra mode", out)

    def test_every_tool_returns_text(self) -> None:
        self.module.setup(self.ctx)
        outputs = {
            "rules": self.module.ponytail_rules("lite"),
            "mode": self.module.ponytail_mode("get"),
            "skill": self.module.ponytail_skill("ponytail-debt"),
            "command": self.module.ponytail_command("ponytail", "ultra"),
            "doctor": self.module.ponytail_doctor(),
        }
        for name, output in outputs.items():
            self.assertIsInstance(output, str, name)
            self.assertTrue(output.strip(), name)
        self.assertIn("PONYTAIL MODE ACTIVE — level: lite", outputs["rules"])
        self.assertIn("auto_injection: compatibility", outputs["mode"])
        self.assertIn("status: ok", outputs["doctor"])

    def test_errors_are_structured_not_raised(self) -> None:
        self.module.setup(self.ctx)
        bad_level = self.module.ponytail_rules("extreme")
        self.assertIn("status: error", bad_level)
        self.assertIn("kind: invalid_argument", bad_level)
        self.assertIn("valid: off, lite, full, ultra", bad_level)

        bad_skill = self.module.ponytail_skill("nope")
        self.assertIn("kind: invalid_argument", bad_skill)
        self.assertIn("ponytail-review", bad_skill)

        bad_path = self.module.ponytail_shortcut_debt("/nonexistent/path")
        self.assertIn("kind: invalid_path", bad_path)


@unittest.skipUnless(
    HAS_OCTOP and HAS_LANGCHAIN,
    "octop_harness / langchain not importable (run with the Octop venv python)",
)
class HookInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.get("PONYTAIL_DEFAULT_MODE")
        os.environ.pop("PONYTAIL_DEFAULT_MODE", None)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("PONYTAIL_DEFAULT_MODE", None)
        else:
            os.environ["PONYTAIL_DEFAULT_MODE"] = self._old_env

    def _middleware(self):
        module = _load(PLUGIN_DIR / "main.py", "ponytail_plugin_hook")
        return module, module.PonytailMiddleware()

    def test_injects_full_ruleset_when_absent(self) -> None:
        module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "full"
        request = FakeRequest(messages=[HumanMessage(content="add a date picker")])
        result = middleware.augment(request)
        text = _system_text(result)
        self.assertIn("PONYTAIL MODE ACTIVE — level: full", text)
        self.assertTrue(text.endswith(module.rt.rules_for("full")))
        self.assertEqual(len(result.messages), 1)

    def test_skips_when_same_mode_already_present(self) -> None:
        _module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "full"
        request = FakeRequest(
            messages=[
                SystemMessage(content="PONYTAIL MODE ACTIVE — level: full\n\n# Ponytail"),
                HumanMessage(content="hi"),
            ]
        )
        self.assertIs(middleware.augment(request), request)

    def test_nothing_injected_when_off_and_clean_history(self) -> None:
        _module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "off"
        request = FakeRequest(messages=[HumanMessage(content="hello")])
        self.assertIs(middleware.augment(request), request)

    def test_off_switch_emits_cancellation_notice(self) -> None:
        module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "off"
        request = FakeRequest(
            messages=[
                SystemMessage(content="PONYTAIL MODE ACTIVE — level: ultra\n\n# Ponytail"),
                HumanMessage(content="stop ponytail"),
            ]
        )
        self.assertEqual(_system_text(middleware.augment(request)), module.OFF_NOTICE)

    def test_upstream_system_message_is_preserved(self) -> None:
        _module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "lite"
        request = FakeRequest(
            messages=[HumanMessage(content="hi")],
            system_message=SystemMessage(content="host prompt"),
        )
        text = _system_text(middleware.augment(request))
        self.assertIn("host prompt", text)
        self.assertIn("PONYTAIL MODE ACTIVE — level: lite", text)


if __name__ == "__main__":
    unittest.main()
