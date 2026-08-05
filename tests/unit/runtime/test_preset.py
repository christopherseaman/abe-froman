"""Unit tests for named-preset resolution.

Covers `runtime/executor/preset.py` and the `create_backend_from_preset`
factory entry.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.backends.factory import create_backend_from_preset
from sqrlly.runtime.executor.preset import (
    build_preset_registry,
    resolve_preset_name,
)
from sqrlly.schema.models import Execute, Node, LlmPreset, Settings


def _preset(transport="acp", provider="anthropic", model="sonnet", default=False):
    return LlmPreset(
        transport=transport, provider=provider, model=model, default=default,
    )


def _node(id_="n1", params=None):
    return Node(
        id=id_, name=id_,
        execute=Execute(url="prompt.md", params=params or {}),
    )


# ---------------------------------------------------------------------------
# resolve_preset_name
# ---------------------------------------------------------------------------


class TestResolvePresetName:
    def test_params_preset_wins_over_default(self):
        settings = Settings(presets={
            "cheap": _preset(model="haiku"),
            "smart": _preset(model="opus", default=True),
        })
        node = _node(params={"preset": "cheap"})
        assert resolve_preset_name(node, settings) == "cheap"

    def test_falls_through_to_default(self):
        settings = Settings(presets={
            "cheap": _preset(model="haiku"),
            "smart": _preset(model="opus", default=True),
        })
        node = _node(params={})
        assert resolve_preset_name(node, settings) == "smart"

    def test_unknown_params_preset_raises(self):
        settings = Settings(presets={
            "smart": _preset(default=True),
        })
        node = _node(params={"preset": "nonexistent"})
        with pytest.raises(ValueError, match="not found in settings.presets"):
            resolve_preset_name(node, settings)

    def test_no_presets_no_default_raises(self):
        # Defensive — schema validator already blocks this path, but the
        # resolver must not silently fall through if it ever happens.
        settings = Settings()
        node = _node()
        with pytest.raises(ValueError, match="no preset reference and no default"):
            resolve_preset_name(node, settings)


# ---------------------------------------------------------------------------
# build_preset_registry
# ---------------------------------------------------------------------------


class TestBuildPresetRegistry:
    def test_passthrough_when_no_cli_override(self):
        settings = Settings(presets={
            "cheap": _preset(model="haiku"),
            "smart": _preset(model="opus", default=True),
        })
        registry = build_preset_registry(settings)
        assert set(registry) == {"cheap", "smart"}
        assert registry["smart"].default is True
        assert registry["cheap"].default is False
        # NEW dict — should not be the same instance.
        assert registry is not settings.presets

    def test_cli_override_flips_default(self):
        settings = Settings(presets={
            "cheap": _preset(model="haiku"),
            "smart": _preset(model="opus", default=True),
        })
        registry = build_preset_registry(settings, cli_override="cheap")
        assert registry["cheap"].default is True
        assert registry["smart"].default is False

    def test_cli_override_unknown_raises(self):
        settings = Settings(presets={"smart": _preset(default=True)})
        with pytest.raises(ValueError, match="not found in settings.presets"):
            build_preset_registry(settings, cli_override="nonexistent")

    def test_empty_presets_returns_empty_dict(self):
        """sqrlly does not synthesize defaults from the environment;
        empty presets is valid for script-only workflows. LLM dispatch
        fails at the call site if needed."""
        registry = build_preset_registry(Settings())
        assert registry == {}

    def test_empty_presets_with_cli_override_raises(self):
        settings = Settings()
        with pytest.raises(ValueError, match="settings.presets is empty"):
            build_preset_registry(settings, cli_override="something")

    def test_no_mutation_of_input(self):
        settings = Settings(presets={
            "smart": _preset(model="opus", default=True),
            "cheap": _preset(model="haiku"),
        })
        original_default = settings.presets["smart"].default
        build_preset_registry(settings, cli_override="cheap")
        # Input unchanged.
        assert settings.presets["smart"].default == original_default
        assert settings.presets["cheap"].default is False


# ---------------------------------------------------------------------------
# create_backend_from_preset (factory dispatch — no real network)
# ---------------------------------------------------------------------------


class TestCreateBackendFromPreset:
    def test_acp_returns_acp_backend(self):
        from sqrlly.runtime.executor.backends.acp import ACPBackend
        preset = _preset(transport="acp", provider="anthropic", model="sonnet")
        backend = create_backend_from_preset(preset)
        assert isinstance(backend, ACPBackend)

    def test_cli_returns_cli_backend(self):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        preset = _preset(transport="cli", provider="anthropic", model="sonnet")
        backend = create_backend_from_preset(preset)
        assert isinstance(backend, CLIBackend)

    def test_codex_cli_uses_noninteractive_stdin_invocation(self):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        preset = _preset(
            transport="cli", provider="openai", model="gpt-5.6-luna",
        )
        backend = create_backend_from_preset(preset)
        assert isinstance(backend, CLIBackend)
        assert backend._argv_prefix == (
            "codex", "exec", "--skip-git-repo-check",
        )
        assert backend._prompt_arg == "-"

    def test_omitted_transport_and_provider_use_codex_cli(self):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        backend = create_backend_from_preset(LlmPreset(model="gpt-5.6-luna"))
        assert isinstance(backend, CLIBackend)
        assert backend._argv_prefix == (
            "codex", "exec", "--skip-git-repo-check",
        )

    def test_cli_threads_tool_permissions(self):
        """Factory passes the preset's tool fields into the cli backend."""
        preset = LlmPreset(
            transport="cli", provider="anthropic", model="sonnet",
            permission_mode="acceptEdits", allowed_tools=["Edit"],
            cli_args=["--add-dir", "."],
        )
        backend = create_backend_from_preset(preset)
        assert backend._tool_argv() == [
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Edit",
            "--add-dir", ".",
        ]

    def test_acp_threads_tool_permissions(self):
        """Factory passes the policy into the acp backend's callbacks."""
        preset = LlmPreset(
            transport="acp", provider="anthropic", model="sonnet",
            permission_mode="default", disallowed_tools=["Bash"],
        )
        backend = create_backend_from_preset(preset)
        assert backend._callbacks._permission_mode == "default"
        assert backend._callbacks._disallowed_tools == ["Bash"]

    def test_rejects_unknown_permission_mode(self):
        with pytest.raises(Exception):
            LlmPreset(
                transport="cli", provider="anthropic", model="x",
                permission_mode="bogus",
            )
