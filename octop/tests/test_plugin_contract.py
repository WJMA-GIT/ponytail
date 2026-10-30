"""Plugin-contract tests: real ``ctx.tool`` registration + hook injection.

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
TOOL_DIR = OCTOP_DIR / "tool"
HOOK_DIR = OCTOP_DIR / "hook"

sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(HOOK_DIR))

try:  # pragma: no cover - environment dependent
    from octop_harness.plugins import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    HAS_OCTOP = True
except Exception:  # pragma: no cover
    PluginContext = None  # type: ignore[assignment]
    PluginManifest = None  # type: ignore[assignment]
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
class ToolRegistrationTests(unittest.TestCase):
    def _load_tool_plugin(self):
        return _load(TOOL_DIR / "main.py", "ponytail_tool_main")

    def test_setup_registers_six_tools(self) -> None:
        module = self._load_tool_plugin()
        manifest = PluginManifest.from_dict(
            {
                "id": "ponytail",
                "version": "5.3.3.1",
                "name": "ponytail",
                "kind": "tool",
                "entry": "main.py",
            }
        )
        ctx = PluginContext(manifest=manifest, source_path=TOOL_DIR)
        module.setup(ctx)
        registry = ctx._tools  # noqa: SLF001 - the public surface is write-only
        names = [entry.name for entry in registry]
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
        for entry in registry:
            self.assertTrue(entry.description, entry.name)

    def test_every_tool_returns_text(self) -> None:
        module = self._load_tool_plugin()
        outputs = {
            "rules": module.ponytail_rules("lite"),
            "mode": module.ponytail_mode("get"),
            "skill": module.ponytail_skill("ponytail-debt"),
            "command": module.ponytail_command("ponytail", "ultra"),
            "doctor": module.ponytail_doctor(),
        }
        for name, output in outputs.items():
            self.assertIsInstance(output, str, name)
            self.assertTrue(output.strip(), name)
        self.assertIn("PONYTAIL MODE ACTIVE — level: lite", outputs["rules"])
        self.assertIn("mode:", outputs["mode"])
        self.assertIn("status: ok", outputs["doctor"])

    def test_errors_are_structured_not_raised(self) -> None:
        module = self._load_tool_plugin()
        bad_level = module.ponytail_rules("extreme")
        self.assertIn("status: error", bad_level)
        self.assertIn("kind: invalid_argument", bad_level)
        self.assertIn("valid: off, lite, full, ultra", bad_level)

        bad_skill = module.ponytail_skill("nope")
        self.assertIn("kind: invalid_argument", bad_skill)
        self.assertIn("ponytail-review", bad_skill)

        bad_path = module.ponytail_shortcut_debt("/nonexistent/path")
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
        module = _load(HOOK_DIR / "main.py", "ponytail_hook_main")
        return module, module.PonytailMiddleware()

    def test_injects_full_ruleset_when_absent(self) -> None:
        module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "full"
        request = FakeRequest(messages=[HumanMessage(content="add a date picker")])
        result = middleware.augment(request)
        text = _system_text(result)
        self.assertIn("PONYTAIL MODE ACTIVE — level: full", text)
        # byte-identical tail to upstream's own output
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
        result = middleware.augment(request)
        self.assertIs(result, request)
        self.assertIs(result.system_message, None)

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
        result = middleware.augment(request)
        self.assertEqual(_system_text(result), module.OFF_NOTICE)

    def test_upstream_system_message_is_preserved(self) -> None:
        _module, middleware = self._middleware()
        os.environ["PONYTAIL_DEFAULT_MODE"] = "lite"
        request = FakeRequest(
            messages=[HumanMessage(content="hi")],
            system_message=SystemMessage(content="host prompt"),
        )
        result = middleware.augment(request)
        text = _system_text(result)
        self.assertIn("host prompt", text)
        self.assertIn("PONYTAIL MODE ACTIVE — level: lite", text)

    def test_setup_registers_middleware(self) -> None:
        module = _load(HOOK_DIR / "main.py", "ponytail_hook_main_register")
        manifest = PluginManifest.from_dict(
            {
                "id": "ponytail-hook",
                "version": "5.3.3.1",
                "name": "ponytail hook",
                "kind": "hook",
                "entry": "main.py",
            }
        )
        ctx = PluginContext(manifest=manifest, source_path=HOOK_DIR)
        module.setup(ctx)
        entries = ctx._middleware  # noqa: SLF001
        self.assertEqual(len(entries), 1)
        self.assertIsInstance(entries[0].instance, module.PonytailMiddleware)
        self.assertEqual(entries[0].priority, module.PRIORITY)


if __name__ == "__main__":
    unittest.main()
