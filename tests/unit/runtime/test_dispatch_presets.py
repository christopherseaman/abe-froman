"""Verify DispatchExecutor's preset-aware backend resolution.

Tests focus on _resolve_prompt_executor + the registry-shape construction:
given a registry of preset-keyed PromptExecutors, the dispatcher picks
the right one for a node based on params.preset vs the default flag.

No actual network calls — ACPBackend defers process spawn until the
first ``send_prompt``; construction is offline-safe. The tests inspect
the returned PromptExecutor's identity and per-call model resolution.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.backends.acp import ACPBackend
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.executor.prompt import resolve_model
from sqrlly.schema.models import Execute, Node, LlmPreset, Settings


def _node(id_="n1", params=None):
    return Node(
        id=id_, name=id_,
        execute=Execute(url="prompt.md", params=params or {}),
    )


def _settings_with_presets():
    return Settings(presets={
        "cheap": LlmPreset(
            transport="acp", provider="anthropic", model="haiku",
        ),
        "smart": LlmPreset(
            transport="acp", provider="anthropic", model="opus",
            default=True,
        ),
    })


class TestResolvePromptExecutor:
    def test_single_preset_via_registry(self):
        """One-entry registry: node resolves to that backend via default preset."""
        from sqrlly.schema.models import LlmPreset
        backend = ACPBackend()
        settings = Settings(presets={
            "default": LlmPreset(
                transport="acp", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        dispatcher = DispatchExecutor(
            prompt_backends={"default": backend}, settings=settings,
        )
        executor = dispatcher._resolve_prompt_executor(_node(), settings)
        assert executor is not None
        assert executor._backend is backend

    def test_multi_preset_uses_default(self):
        settings = _settings_with_presets()
        cheap_be = ACPBackend()
        smart_be = ACPBackend()
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # Node without params.preset → uses default (smart).
        executor = dispatcher._resolve_prompt_executor(_node(), settings)
        assert executor._backend is smart_be

    def test_multi_preset_params_override(self):
        settings = _settings_with_presets()
        cheap_be = ACPBackend()
        smart_be = ACPBackend()
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # Node with params.preset=cheap → uses cheap.
        node = _node(params={"preset": "cheap"})
        executor = dispatcher._resolve_prompt_executor(node, settings)
        assert executor._backend is cheap_be

    def test_no_backends_returns_none(self):
        dispatcher = DispatchExecutor(settings=Settings())
        assert dispatcher._resolve_prompt_executor(_node(), Settings()) is None

    def test_missing_backend_for_preset_raises(self):
        settings = _settings_with_presets()
        # Registry missing the 'smart' backend that settings declares as default.
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": ACPBackend()},
            settings=settings,
        )
        with pytest.raises(RuntimeError, match="no backend is registered"):
            dispatcher._resolve_prompt_executor(_node(), settings)

class TestResolveModel:
    def test_no_presets_returns_none(self):
        """Pure-script workflow has no presets → resolve_model returns None
        so foreman skips per-model semaphore selection."""
        assert resolve_model(_node(), Settings()) is None

    def test_default_preset_model(self):
        """Presets declared, node references nothing → default preset's model."""
        node = _node()
        settings = _settings_with_presets()  # default=smart, model=opus
        assert resolve_model(node, settings) == "opus"

    def test_params_preset_selects_model(self):
        """params.preset selects a non-default preset."""
        node = _node(params={"preset": "cheap"})
        settings = _settings_with_presets()
        assert resolve_model(node, settings) == "haiku"


class TestGetBackend:
    """get_backend() returns the right backend for LLM-gate dispatch."""

    def test_single_preset_returns_the_only_backend(self):
        """One-entry registry: get_backend() returns it."""
        from sqrlly.schema.models import LlmPreset
        backend = ACPBackend()
        settings = Settings(presets={
            "default": LlmPreset(
                transport="acp", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        dispatcher = DispatchExecutor(
            prompt_backends={"default": backend}, settings=settings,
        )
        assert dispatcher.get_backend() is backend

    def test_multi_preset_returns_default(self):
        settings = _settings_with_presets()
        cheap_be = ACPBackend()
        smart_be = ACPBackend()
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # `smart` is flagged default — that backend goes to LLM gates.
        assert dispatcher.get_backend() is smart_be

    def test_no_backends_returns_none(self):
        assert DispatchExecutor().get_backend() is None


class TestFactoryThreadsAcpEnv:
    """ACPBackend construction is offline-safe (spawn deferred to first
    send_prompt), so we can assert env wiring without launching the adapter."""

    def test_build_acp_threads_env(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        backend = create_backend_from_preset(LlmPreset(
            transport="acp", provider="anthropic", model="opus",
            env={"CLAUDE_CODE_EFFORT_LEVEL": "max"},
        ))
        assert backend._env == {"CLAUDE_CODE_EFFORT_LEVEL": "max"}

    def test_build_acp_default_env_is_empty(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        backend = create_backend_from_preset(LlmPreset(
            transport="acp", provider="anthropic", model="opus",
        ))
        # Default empty env is stored as {} (threaded through unchanged).
        assert backend._env == {}
