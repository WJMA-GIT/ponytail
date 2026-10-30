"""Octop hook plugin: ponytail auto-injection (kind: hook).

This is ponytail's *native* mechanism: upstream's Hermes plugin registers a
``pre_llm_call`` hook that puts the ruleset into the model context every turn.
In Octop that is a LangChain agent middleware appended to the compiled system
message, request-scoped (same pattern as ``BootstrapMiddleware``).

Behaviour is kept equivalent to upstream's ``_pre_llm_call``:

* no PONYTAIL marker in the conversation yet → inject the ruleset for the
  configured mode, and inject nothing when the mode is ``off``;
* the conversation already carries a marker for the current mode → skip
  (no duplicate ruleset);
* the conversation carries a marker for a *different* mode → inject, so an
  ``off`` switch also emits ``PONYTAIL MODE OFF`` to cancel the earlier rules.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from octop_harness.plugins import PluginContext

# Load the shared runtime by path under a plugin-unique name (see octop/tool/main.py).
_PLUGIN_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ponytail_runtime_hook", _PLUGIN_DIR / "ponytail_runtime.py"
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


def setup(ctx: PluginContext) -> None:
    """Register the auto-injection middleware."""
    ctx.middleware(PonytailMiddleware(), priority=PRIORITY)
