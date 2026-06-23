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
from sqrlly.runtime.executor.backends.cli import CLIBackend
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


def _two_preset_settings():
    """cli (default) + acp (declared but referenced by no node)."""
    return Settings(presets={
        "cli": LlmPreset(
            transport="cli", provider="anthropic",
            model="sonnet", default=True,
        ),
        "acp": LlmPreset(
            transport="acp", provider="anthropic", model="sonnet",
        ),
    })


class TestLazyBackendBuild:
    """Backends are materialized lazily — a declared-but-unused preset
    never constructs its backend, so an optional dependency (e.g. the
    ``acp`` package) is only imported when a node actually dispatches to
    that preset. Regression for the `uv tool install sqrlly` crash where
    an unused `acp` preset's backend was built eagerly at startup,
    raising ModuleNotFoundError for the absent `acp` package."""

    def test_unused_preset_builder_not_invoked(self):
        built: list[str] = []

        def cli_builder():
            built.append("cli")
            return CLIBackend()

        def acp_builder():
            built.append("acp")
            raise AssertionError("unused preset backend must not be built")

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={"cli": cli_builder, "acp": acp_builder},
            settings=s,
        )
        # A node with no params.preset resolves to the default (cli).
        executor = d._resolve_prompt_executor(_node(), s)
        assert executor is not None
        assert built == ["cli"]  # acp_builder never called

    def test_builder_invoked_once_then_cached(self):
        calls: list[int] = []

        def cli_builder():
            calls.append(1)
            return CLIBackend()

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={
                "cli": cli_builder,
                "acp": lambda: ACPBackend(),
            },
            settings=s,
        )
        first = d._resolve_prompt_executor(_node("a"), s)
        second = d._resolve_prompt_executor(_node("b"), s)
        assert first is second        # cached PromptExecutor
        assert calls == [1]           # backend built exactly once

    def test_get_backend_builds_only_default(self):
        built: list[str] = []

        def cli_builder():
            built.append("cli")
            return CLIBackend()

        def acp_builder():
            built.append("acp")
            raise AssertionError("non-default preset must not be built")

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={"cli": cli_builder, "acp": acp_builder},
            settings=s,
        )
        backend = d.get_backend()
        assert isinstance(backend, CLIBackend)
        assert built == ["cli"]

    @pytest.mark.asyncio
    async def test_close_skips_unbuilt_backends(self):
        def acp_builder():
            raise AssertionError("unbuilt preset must not be built by close()")

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={
                "cli": lambda: CLIBackend(),
                "acp": acp_builder,
            },
            settings=s,
        )
        d._resolve_prompt_executor(_node(), s)  # builds cli only
        await d.close()  # must not touch the unbuilt acp builder

    def test_prebuilt_backends_still_supported(self):
        """Embedders/tests passing already-built backends keep working;
        identity is preserved (wrapped as a constant builder)."""
        backend = ACPBackend()
        s = Settings(presets={
            "default": LlmPreset(
                transport="acp", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        d = DispatchExecutor(prompt_backends={"default": backend}, settings=s)
        assert d.get_backend() is backend


class TestAcpMissingDepError:
    """When the `acp` optional dependency is absent, building an acp
    backend must raise an actionable error, not a bare ModuleNotFoundError."""

    def test_build_acp_missing_dep_raises_clear_error(self, monkeypatch):
        import sys
        from sqrlly.runtime.executor.backends import factory

        # Environment-shape instrumentation (sanctioned, like patching
        # shutil.which): simulate the `acp` package being absent. Evict the
        # backend module so its top-level `from acp import ...` re-runs, and
        # block `acp` so that import fails. Not faking what acp returns —
        # simulating its absence.
        monkeypatch.delitem(
            sys.modules, "sqrlly.runtime.executor.backends.acp", raising=False,
        )
        monkeypatch.setitem(sys.modules, "acp", None)
        preset = LlmPreset(transport="acp", provider="anthropic", model="opus")
        with pytest.raises(RuntimeError, match=r"pip install sqrlly\[acp\]"):
            factory.create_backend_from_preset(preset)
