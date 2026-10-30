#!/usr/bin/env python3
"""Install-and-call smoke check for the two ponytail Octop plugins.

Requires the Octop runtime (run with the Octop venv python, e.g.
``/app/.venv/bin/python octop/tools/verify_install.py``).

It loads the *installed* plugins from ``--plugins-dir`` (default
``~/.octop/plugins``), prints every registered tool's output, and proves the
hook middleware injects the upstream ruleset into a system message.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from octop_harness.plugins.loader import load_all
from octop_harness.plugins.registry import PluginRegistry


class _Request:
    """Minimal ModelRequest stand-in (same shape the middleware uses)."""

    def __init__(self, messages: list[Any], system_message: Any = None) -> None:
        self.messages = messages
        self.system_message = system_message

    def override(self, **kwargs: Any) -> "_Request":
        return _Request(
            messages=list(kwargs.get("messages", self.messages)),
            system_message=kwargs.get("system_message", self.system_message),
        )


def _text(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plugins-dir",
        default=str(Path.home() / ".octop" / "plugins"),
        help="where `octop plugin install` placed the plugins",
    )
    args = parser.parse_args()

    root = Path(args.plugins_dir).expanduser()
    print(f"plugins_dir: {root}")
    loaded = load_all(root)
    print(f"loaded: {[item.manifest.id for item in loaded]}")

    registry = PluginRegistry()
    tools = registry.all_tools()
    print(f"registered tools: {[tool.name for tool in tools]}\n")

    calls: list[tuple[str, dict[str, Any]]] = [
        ("ponytail_mode", {"action": "get"}),
        ("ponytail_rules", {"level": "lite"}),
        ("ponytail_skill", {"name": "ponytail-debt", "level": "full"}),
        ("ponytail_command", {"name": "ponytail", "command_args": "ultra"}),
        ("ponytail_shortcut_debt", {"root": str(root.parent.parent), "limit": 5}),
        ("ponytail_doctor", {}),
    ]
    by_name = {tool.name: tool for tool in tools}
    for name, kwargs in calls:
        tool = by_name.get(name)
        print(f"=== {name} {kwargs}")
        if tool is None:
            print("MISSING")
            continue
        result = tool.fn(**kwargs)
        text = result if isinstance(result, str) else repr(result)
        preview = text if len(text) <= 700 else text[:700] + f"\n… ({len(text)} chars total)"
        print(preview)
        print()

    print("=== hook middleware chain")
    chain = registry.build_middleware_chain()
    print(f"chain: {[type(item).__name__ for item in chain]}")
    ponytail = [item for item in chain if type(item).__name__ == "PonytailMiddleware"]
    if not ponytail:
        print("PonytailMiddleware NOT in chain")
        return 1
    instance = ponytail[0]
    module = sys.modules.get(type(instance).__module__)
    runtime = getattr(module, "rt", None)
    mode = runtime.default_mode() if runtime is not None else "?"
    expected = (runtime.rules_for(mode) if runtime is not None else "") or ""
    request = _Request(messages=[])
    injected = instance.augment(request)
    text = _text(injected.system_message)
    print(f"mode: {mode} (source={runtime.mode_source() if runtime else '?'})")
    print(f"injected_chars: {len(text)}")
    print(f"injected_head: {text.splitlines()[0] if text else '(none)'}")
    print(f"injected_head_is_upstream_rules: {bool(expected) and text.startswith(expected)}")
    print(f"injected_has_skills_block: {'PONYTAIL SKILLS' in text}")
    print(f"messages_untouched: {injected.messages == request.messages}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
