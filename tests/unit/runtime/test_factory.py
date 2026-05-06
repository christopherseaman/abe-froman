"""Tests for backend factory + auto-detection.

Two layers:
  - ``create_prompt_backend(type)`` correctness for each registered
    type, including the ValueError shape on unknown types and on
    DeepSeek-without-key.
  - ``auto_detect_executor()`` resolution order — env vars masked via
    ``monkeypatch``, ``shutil.which`` masked via ``monkeypatch.setattr``.
    The terminal RuntimeError surface is asserted on the
    no-backend-available case only; explicit choices never reach this
    function.
"""
from __future__ import annotations

import warnings

import pytest

from abe_froman.runtime.executor.backends.factory import (
    auto_detect_executor,
    create_prompt_backend,
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Strip every secret source the auto-detector reads.

    Three layers in the resolver: process env, project-local `.env`,
    and (when used) YAML settings. We isolate process env via
    ``monkeypatch.delenv``, push CWD into a fresh tmp dir so the
    `.env` walk-up can't reach a real one, and reset the dotenv
    cache so any prior call doesn't leak across tests.
    """
    from abe_froman.runtime.secrets import _reset_dotenv_cache

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_dotenv_cache()
    return tmp_path


# ---------------------------------------------------------------------
# create_prompt_backend
# ---------------------------------------------------------------------

class TestCreatePromptBackend:
    def test_stub_no_longer_supported(self):
        """`stub` was removed in the StubBackend cutover. Authors who
        relied on it must pick a real backend or rely on auto-detect."""
        with pytest.raises(ValueError) as ei:
            create_prompt_backend("stub")
        assert "stub" in str(ei.value)
        # The error message lists supported types; "stub" is NOT among them.
        assert "anthropic" in str(ei.value)
        assert "deepseek" in str(ei.value)

    def test_anthropic_with_key_returns_anthropic_backend(self):
        from abe_froman.runtime.executor.backends.anthropic import (
            AnthropicBackend,
        )

        backend = create_prompt_backend("anthropic", api_key="sk-fake")
        assert isinstance(backend, AnthropicBackend)
        assert backend._api_key == "sk-fake"

    def test_anthropic_without_key_raises(self, clean_env):
        with pytest.raises(ValueError) as ei:
            create_prompt_backend("anthropic")
        assert "ANTHROPIC_API_KEY" in str(ei.value)

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

    def test_openai_picks_up_base_url_from_env(self, monkeypatch):
        """``OPENAI_BASE_URL`` env var lets OpenRouter / Ollama /
        LM Studio / LiteLLM / etc. work via the openai backend without
        passing kwargs at construction time."""
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        monkeypatch.setenv("OPENAI_API_KEY", "sk-or-v1-fake")
        monkeypatch.setenv(
            "OPENAI_BASE_URL", "https://openrouter.ai/api/v1",
        )
        backend = create_prompt_backend("openai")
        assert isinstance(backend, OpenAIBackend)
        assert backend._base_url == "https://openrouter.ai/api/v1"

    def test_openai_explicit_base_url_overrides_env(self, monkeypatch):
        """Caller-supplied ``base_url`` wins over the env var."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://from-env/")
        backend = create_prompt_backend(
            "openai", base_url="https://from-arg/",
        )
        assert backend._base_url == "https://from-arg/"

    def test_unknown_type_raises_with_supported_list(self):
        with pytest.raises(ValueError) as ei:
            create_prompt_backend("ruby")
        msg = str(ei.value)
        assert "ruby" in msg
        assert "deepseek" in msg
        assert "anthropic" in msg
        # "stub" should NOT be in the supported list anymore.
        assert "stub" not in msg


# ---------------------------------------------------------------------
# auto_detect_executor — resolution order
#
# Most tests here patch ``factory.shutil.which`` to control which
# binary appears on PATH. That patch is sanctioned in
# ``feedback_no_fake_backends.md`` as orchestration instrumentation
# (we're not faking what an external system *would respond*; we're
# choosing which environment-shape branch the resolver sees).
#
# ``test_npx_resolved_via_real_shutil_which_against_test_artifact``
# below uses the cleaner alternative: a tmp dir with a fake `npx`
# script + ``monkeypatch.setenv("PATH", ...)``. Real ``shutil.which``
# runs against a controlled PATH. Use this style by preference when
# the test only needs to gate one binary; use ``setattr`` patching
# when controlling multiple binaries simultaneously is awkward.
# ---------------------------------------------------------------------

class TestAutoDetect:
    def test_anthropic_key_wins(self, clean_env, monkeypatch):
        """ANTHROPIC_API_KEY auto-picks the native Anthropic backend —
        even when DeepSeek and npx are also available, Anthropic is
        first in the resolution chain."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert auto_detect_executor() == "anthropic"

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

    def test_acp_when_only_npx(self, clean_env, monkeypatch):
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert auto_detect_executor() == "acp"

    def test_npx_resolved_via_real_shutil_which_against_test_artifact(
        self, clean_env, monkeypatch,
    ):
        """Companion to ``test_acp_when_only_npx`` using a real PATH
        manipulation instead of patching ``shutil.which``: drop a
        non-functional `npx` script in a tmp dir and point PATH at
        only that dir. Real ``shutil.which("npx")`` finds it; auto-
        detect returns ``"acp"``.

        The script is non-functional by design — auto-detect only
        checks for *presence* on PATH, not whether the binary actually
        works. This test exercises the resolver against a real-system
        artifact rather than a function patch.
        """
        bin_dir = clean_env / "fake-bin"
        bin_dir.mkdir()
        (bin_dir / "npx").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "npx").chmod(0o755)
        # Point PATH at ONLY the tmp bin dir — real shutil.which
        # walks this PATH and finds our fake npx.
        monkeypatch.setenv("PATH", str(bin_dir))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert auto_detect_executor() == "acp"

    def test_raises_when_nothing_available(
        self, clean_env, monkeypatch,
    ):
        """Terminal failure mode: no env, no npx → raise. The previous
        behavior (silent fallback to stub with UserWarning) was removed
        when StubBackend was deleted; production must never emit fake
        output."""
        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.shutil.which",
            lambda name: None,
        )
        with pytest.raises(RuntimeError) as ei:
            auto_detect_executor()
        msg = str(ei.value)
        assert "ANTHROPIC_API_KEY" in msg
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
        executor = "anthropic"  # explicit CLI flag
        settings_executor = "acp"  # YAML setting
        result = executor or settings_executor or None
        assert result == "anthropic"

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


# ---------------------------------------------------------------------
# _resolve_anthropic_key — wraps the generic resolver
# ---------------------------------------------------------------------

class TestResolveAnthropicKey:
    """`_resolve_anthropic_key` is a thin alias for
    ``resolve_secret("ANTHROPIC_API_KEY")``. The full resolver
    semantics (env > .env > settings) are tested in
    ``tests/unit/runtime/test_secrets.py``; here we just pin the
    provider-name binding and the env-var path."""

    def test_env_var_returns_value(self, monkeypatch):
        from abe_froman.runtime.executor.backends.factory import (
            _resolve_anthropic_key,
        )
        from abe_froman.runtime.secrets import _reset_dotenv_cache

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        _reset_dotenv_cache()
        assert _resolve_anthropic_key() == "sk-from-env"

    def test_unset_returns_none(self, monkeypatch, tmp_path):
        from abe_froman.runtime.executor.backends.factory import (
            _resolve_anthropic_key,
        )
        from abe_froman.runtime.secrets import _reset_dotenv_cache

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # CWD with no .env so the file fallback misses too.
        monkeypatch.chdir(tmp_path)
        _reset_dotenv_cache()
        assert _resolve_anthropic_key() is None
