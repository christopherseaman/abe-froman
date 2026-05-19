"""Verify DispatchExecutor's preset-aware backend resolution.

Tests focus on _resolve_prompt_executor + the registry-shape construction:
given a registry of preset-keyed PromptExecutors, the dispatcher picks
the right one for a node based on params.preset vs the default flag.

No actual network calls — backends are constructed lazy (LazyClientMixin
defers client init until send_prompt). The tests inspect the returned
PromptExecutor's identity and per-call model resolution.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.backends.anthropic import AnthropicBackend
from sqrlly.runtime.executor.backends.openai import OpenAIBackend
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.executor.prompt import resolve_model
from sqrlly.schema.models import Execute, Node, Preset, Settings


def _node(id_="n1", model=None, params=None):
    return Node(
        id=id_, name=id_, model=model,
        execute=Execute(url="prompt.md", params=params or {}),
    )


def _settings_with_presets():
    return Settings(presets={
        "cheap": Preset(
            transport="api", provider="anthropic", model="haiku",
        ),
        "smart": Preset(
            transport="api", provider="anthropic", model="opus",
            default=True,
        ),
    })


class TestResolvePromptExecutor:
    def test_legacy_single_backend_path(self):
        backend = AnthropicBackend(api_key="sk-ant-fake")
        dispatcher = DispatchExecutor(
            prompt_backend=backend, settings=Settings(),
        )
        # Single-backend path: any node resolves to the same executor.
        executor = dispatcher._resolve_prompt_executor(_node(), Settings())
        assert executor is not None
        assert executor._backend is backend

    def test_multi_preset_uses_default(self):
        settings = _settings_with_presets()
        cheap_be = AnthropicBackend(api_key="sk-ant-cheap")
        smart_be = AnthropicBackend(api_key="sk-ant-smart")
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # Node without params.preset → uses default (smart).
        executor = dispatcher._resolve_prompt_executor(_node(), settings)
        assert executor._backend is smart_be

    def test_multi_preset_params_override(self):
        settings = _settings_with_presets()
        cheap_be = AnthropicBackend(api_key="sk-ant-cheap")
        smart_be = AnthropicBackend(api_key="sk-ant-smart")
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
            prompt_backends={
                "cheap": AnthropicBackend(api_key="sk-ant-cheap"),
            },
            settings=settings,
        )
        with pytest.raises(RuntimeError, match="no backend is registered"):
            dispatcher._resolve_prompt_executor(_node(), settings)

    def test_mutual_exclusive_constructor_args(self):
        with pytest.raises(ValueError, match="either prompt_backend"):
            DispatchExecutor(
                prompt_backend=AnthropicBackend(api_key="sk-ant"),
                prompt_backends={
                    "x": AnthropicBackend(api_key="sk-ant-x"),
                },
            )


class TestResolveModel:
    def test_legacy_default_model(self):
        """No presets, no node.model → settings.default_model."""
        node = _node()
        settings = Settings(default_model="opus")
        assert resolve_model(node, settings) == "opus"

    def test_legacy_node_override(self):
        """No presets, node.model set → node.model wins."""
        node = _node(model="haiku")
        settings = Settings(default_model="opus")
        assert resolve_model(node, settings) == "haiku"

    def test_preset_path_default_preset(self):
        """Presets declared, node references nothing → default preset's model."""
        node = _node()
        settings = _settings_with_presets()  # default=smart, model=opus
        assert resolve_model(node, settings) == "opus"

    def test_preset_path_params_preset(self):
        """params.preset selects a non-default preset."""
        node = _node(params={"preset": "cheap"})
        settings = _settings_with_presets()
        assert resolve_model(node, settings) == "haiku"

    def test_node_model_overrides_preset(self):
        """Node.model overrides preset.model — for foreman semaphore."""
        node = _node(model="explicit-pin", params={"preset": "cheap"})
        settings = _settings_with_presets()
        assert resolve_model(node, settings) == "explicit-pin"


class TestGetBackend:
    """get_backend() returns the right backend for LLM-gate dispatch."""

    def test_legacy_returns_the_single_backend(self):
        backend = AnthropicBackend(api_key="sk-ant")
        dispatcher = DispatchExecutor(prompt_backend=backend)
        assert dispatcher.get_backend() is backend

    def test_multi_preset_returns_default(self):
        settings = _settings_with_presets()
        cheap_be = AnthropicBackend(api_key="sk-ant-cheap")
        smart_be = AnthropicBackend(api_key="sk-ant-smart")
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # `smart` is flagged default — that backend goes to LLM gates.
        assert dispatcher.get_backend() is smart_be

    def test_no_backends_returns_none(self):
        assert DispatchExecutor().get_backend() is None
