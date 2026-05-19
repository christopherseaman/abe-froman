"""Unit tests for the named-preset resolution + auto-detect.

Covers `runtime/executor/preset.py` and the new
`create_backend_from_preset` factory entry.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.backends.factory import create_backend_from_preset
from sqrlly.runtime.executor.preset import (
    _AUTO_PRESET_NAME,
    auto_detect_default_preset,
    build_preset_registry,
    resolve_preset_name,
)
from sqrlly.schema.models import Execute, Node, Preset, Settings


def _preset(transport="api", provider="anthropic", model="sonnet", default=False, base_url=None):
    return Preset(
        transport=transport, provider=provider, model=model,
        default=default, base_url=base_url,
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
# auto_detect_default_preset
# ---------------------------------------------------------------------------


class TestAutoDetectDefaultPreset:
    def test_anthropic_key_wins(self, monkeypatch, tmp_path):
        # Walk up from a clean tmp_path so .env discovery doesn't pick
        # up the developer's real keys.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        p = auto_detect_default_preset()
        assert p.transport == "api"
        assert p.provider == "anthropic"
        assert p.model == "sonnet"
        assert p.default is True

    def test_deepseek_after_no_anthropic(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
        p = auto_detect_default_preset()
        assert p.transport == "api"
        assert p.provider == "deepseek"
        assert p.model == "deepseek-v4-flash"

    def test_acp_after_no_keys(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        # Synthesize an `npx` shim on PATH so shutil.which finds something.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "npx").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "npx").chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        p = auto_detect_default_preset()
        assert p.transport == "acp"
        assert p.provider == "anthropic"

    def test_no_keys_no_npx_raises(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("PATH", "/nonexistent")
        with pytest.raises(RuntimeError, match="No preset declared"):
            auto_detect_default_preset()


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

    def test_empty_presets_auto_detects(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        settings = Settings()
        registry = build_preset_registry(settings)
        assert set(registry) == {_AUTO_PRESET_NAME}
        assert registry[_AUTO_PRESET_NAME].default is True
        assert registry[_AUTO_PRESET_NAME].provider == "anthropic"

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
    def test_api_anthropic_requires_key(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        preset = _preset(transport="api", provider="anthropic")
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            create_backend_from_preset(preset)

    def test_api_anthropic_with_key_returns_backend(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from sqrlly.runtime.executor.backends.anthropic import AnthropicBackend
        preset = _preset(transport="api", provider="anthropic", model="opus")
        backend = create_backend_from_preset(preset)
        assert isinstance(backend, AnthropicBackend)

    def test_api_deepseek_requires_key(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        preset = _preset(transport="api", provider="deepseek")
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            create_backend_from_preset(preset)

    def test_api_custom_requires_base_url(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CUSTOM_API_KEY", "sk-custom-test")
        monkeypatch.delenv("CUSTOM_API_BASE_URL", raising=False)
        preset = _preset(transport="api", provider="custom")
        with pytest.raises(ValueError, match="base_url"):
            create_backend_from_preset(preset)

    def test_api_custom_uses_preset_base_url(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CUSTOM_API_KEY", "sk-custom-test")
        monkeypatch.delenv("CUSTOM_API_BASE_URL", raising=False)
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend
        preset = _preset(
            transport="api", provider="custom",
            model="local-model", base_url="https://my-endpoint/v1",
        )
        backend = create_backend_from_preset(preset)
        assert isinstance(backend, OpenAIBackend)

    def test_acp_returns_acp_backend(self):
        from sqrlly.runtime.executor.backends.acp import ACPBackend
        preset = _preset(transport="acp", provider="anthropic", model="sonnet")
        backend = create_backend_from_preset(preset)
        assert isinstance(backend, ACPBackend)
