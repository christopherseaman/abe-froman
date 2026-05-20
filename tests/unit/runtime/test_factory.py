"""Tests for backend factory thin wrappers.

The preset rework moved the dispatch logic into
``create_backend_from_preset`` (tested in ``test_preset.py``). What
remains in this module is the secret-resolver wrappers
``_resolve_anthropic_key`` and ``_resolve_deepseek_key``, which are
thin aliases over ``resolve_secret(...)`` — exercised here to pin
the provider-name binding and the unset-returns-None contract.

Full secret-resolution semantics (env > project-local .env > settings)
live in ``tests/unit/runtime/test_secrets.py``.
"""
from __future__ import annotations

import pytest


class TestResolveAnthropicKey:
    def test_env_var_returns_value(self, monkeypatch):
        from sqrlly.runtime.executor.backends.factory import (
            _resolve_anthropic_key,
        )
        from sqrlly.runtime.secrets import _reset_dotenv_cache

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        _reset_dotenv_cache()
        assert _resolve_anthropic_key() == "sk-from-env"

    def test_unset_returns_none(self, monkeypatch, tmp_path):
        from sqrlly.runtime.executor.backends.factory import (
            _resolve_anthropic_key,
        )
        from sqrlly.runtime.secrets import _reset_dotenv_cache

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        _reset_dotenv_cache()
        assert _resolve_anthropic_key() is None


class TestResolveDeepseekKey:
    def test_env_var_returns_value(self, monkeypatch):
        from sqrlly.runtime.executor.backends.factory import (
            _resolve_deepseek_key,
        )
        from sqrlly.runtime.secrets import _reset_dotenv_cache

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
        _reset_dotenv_cache()
        assert _resolve_deepseek_key() == "sk-from-env"

    def test_unset_returns_none(self, monkeypatch, tmp_path):
        from sqrlly.runtime.executor.backends.factory import (
            _resolve_deepseek_key,
        )
        from sqrlly.runtime.secrets import _reset_dotenv_cache

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        _reset_dotenv_cache()
        assert _resolve_deepseek_key() is None
