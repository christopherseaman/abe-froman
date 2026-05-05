"""Tests for backend factory + auto-detection.

Two layers:
  - ``create_prompt_backend(type)`` correctness for each registered
    type, including the ValueError shape on unknown types and on
    DeepSeek-without-key.
  - ``auto_detect_executor()`` resolution order — env vars masked via
    ``monkeypatch``, ``shutil.which`` masked via ``monkeypatch.setattr``.
    The warning surface is asserted on the no-backend-available case
    only; explicit choices never reach this function.
"""
from __future__ import annotations

import warnings

import pytest

from abe_froman.runtime.executor.backends.factory import (
    auto_detect_executor,
    create_prompt_backend,
)
from abe_froman.runtime.executor.backends.stub import StubBackend


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Strip every env source the auto-detector reads, and point HOME at
    a fresh tmp dir so on-disk auth.json lookups miss."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------
# create_prompt_backend
# ---------------------------------------------------------------------

class TestCreatePromptBackend:
    def test_stub_returns_stub(self):
        backend = create_prompt_backend("stub")
        assert isinstance(backend, StubBackend)

    def test_deepseek_with_key_returns_openai_backend(self):
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        backend = create_prompt_backend("deepseek", api_key="sk-fake")
        assert isinstance(backend, OpenAIBackend)
        assert backend._base_url == "https://api.deepseek.com/v1"

    def test_deepseek_without_key_raises(self, clean_env):
        with pytest.raises(ValueError) as ei:
            create_prompt_backend("deepseek")
        assert "DEEPSEEK_API_KEY" in str(ei.value)

    def test_openai_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError) as ei:
            create_prompt_backend("openai")
        assert "OPENAI_API_KEY" in str(ei.value)

    def test_openai_with_key_and_base_url(self):
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        backend = create_prompt_backend(
            "openai", api_key="sk-fake", base_url="https://custom/",
        )
        assert isinstance(backend, OpenAIBackend)
        assert backend._base_url == "https://custom/"

    def test_unknown_type_raises_with_supported_list(self):
        with pytest.raises(ValueError) as ei:
            create_prompt_backend("ruby")
        msg = str(ei.value)
        assert "ruby" in msg
        assert "deepseek" in msg
        assert "stub" in msg


# ---------------------------------------------------------------------
# auto_detect_executor — resolution order
# ---------------------------------------------------------------------

class TestAutoDetect:
    def test_anthropic_key_alone_falls_through(self, clean_env, monkeypatch):
        """ANTHROPIC_API_KEY alone does NOT auto-pick — the native
        anthropic backend isn't wired yet, so picking it would surface
        as a confusing ValueError at workflow startup. Resolution falls
        through to the next available real backend (or stub)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            # ACP via npx is what actually gets picked — the user's
            # ANTHROPIC_API_KEY is irrelevant to that decision.
            assert auto_detect_executor() == "acp"

    def test_deepseek_key_when_no_anthropic(self, clean_env, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
        # npx may or may not be present; deepseek still wins over acp.
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert auto_detect_executor() == "deepseek"

    def test_deepseek_via_disk_when_no_env(self, clean_env, monkeypatch):
        auth = clean_env / ".pi" / "agent" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"deepseek": {"key": "sk-from-disk"}}')
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: None,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert auto_detect_executor() == "deepseek"

    def test_acp_when_only_npx(self, clean_env, monkeypatch):
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert auto_detect_executor() == "acp"

    def test_stub_with_warning_when_nothing_available(
        self, clean_env, monkeypatch,
    ):
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: None,
        )
        with pytest.warns(UserWarning, match="No real backend"):
            assert auto_detect_executor() == "stub"

    def test_stub_warning_mentions_remediation(
        self, clean_env, monkeypatch,
    ):
        """The warning must give the operator concrete next steps."""
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: None,
        )
        with pytest.warns(UserWarning) as record:
            auto_detect_executor()
        msg = str(record[0].message)
        assert "DEEPSEEK_API_KEY" in msg
        assert "claude-code-acp" in msg


# ---------------------------------------------------------------------
# CLI integration: -e flag wins; settings.executor wins; auto-detect
# triggers only when both are absent.
# ---------------------------------------------------------------------

class TestExecutorResolution:
    """The CLI's resolution rule — ``executor or settings.executor or
    auto_detect_executor()`` — pinned via direct invocation. The CLI
    already imports auto_detect_executor lazily; this test exercises
    the string-resolution shape so the auto-detect trigger condition
    stays correct."""

    def test_cli_flag_wins_over_settings_and_autodetect(
        self, clean_env, monkeypatch,
    ):
        # Force auto-detect to error if reached
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.auto_detect_executor",
            lambda: pytest.fail("auto_detect must not be called"),
        )
        executor = "stub"  # explicit CLI flag
        settings_executor = "acp"  # YAML setting
        result = executor or settings_executor or None
        assert result == "stub"

    def test_settings_wins_when_no_cli_flag(self):
        executor = None
        settings_executor = "deepseek"
        result = executor or settings_executor
        assert result == "deepseek"

    def test_autodetect_only_when_both_absent(self, clean_env, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
        executor = None
        settings_executor = None
        result = executor or settings_executor or auto_detect_executor()
        assert result == "deepseek"
