"""Tests for OpenAIBackend (DeepSeek + generic OpenAI-compatible).

Two layers:
  - **Live** (artifact-driven): real DeepSeek API call gated on key
    presence. Produces actual model output; skipped with explicit
    reason when no key is on disk.
  - **Error mapping** (offline): patch the openai SDK's HTTP-call
    layer to raise specific exceptions; assert our wrapping code
    surfaces them as ``OverloadError``. We're testing **our** mapping
    logic, not the SDK — `feedback_no_fake_backends.md` forbids fake
    PromptBackend doubles, not patching upstream SDKs.
"""
from __future__ import annotations

import importlib.util

import pytest

from abe_froman.runtime.executor.backends.factory import (
    DEEPSEEK_BASE_URL,
    _resolve_deepseek_key,
)
from abe_froman.runtime.executor.backends.openai import OpenAIBackend
from abe_froman.runtime.result import OverloadError


# ---------------------------------------------------------------------
# Live DeepSeek API tests (gated on key availability)
# ---------------------------------------------------------------------

DEEPSEEK_KEY = _resolve_deepseek_key()
_OPENAI_SDK_INSTALLED = importlib.util.find_spec("openai") is not None
LIVE_REASON = (
    "DeepSeek API key not available "
    "(set DEEPSEEK_API_KEY in the environment; see .env.example)"
)
_SDK_REASON = "openai SDK not installed (uv sync --extra openai)"


@pytest.mark.live
@pytest.mark.skipif(DEEPSEEK_KEY is None, reason=LIVE_REASON)
@pytest.mark.skipif(not _OPENAI_SDK_INSTALLED, reason=_SDK_REASON)
class TestDeepSeekLive:
    """Real network calls to DeepSeek — proves the wire-level path
    works end-to-end."""

    async def test_send_prompt_returns_text(self, tmp_path):
        backend = OpenAIBackend(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)
        try:
            result = await backend.send_prompt(
                prompt="Reply with the single word: pong",
                model="deepseek-v4-flash",
                workdir=str(tmp_path),
                timeout=30.0,
            )
        finally:
            await backend.close()

        assert result.success is True
        assert result.output  # non-empty
        # Loose check — model may be polite/verbose but must mention 'pong'.
        assert "pong" in result.output.lower()

    async def test_close_is_idempotent(self, tmp_path):
        backend = OpenAIBackend(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)
        await backend.send_prompt(
            prompt="say hi", model="deepseek-v4-flash",
            workdir=str(tmp_path), timeout=30.0,
        )
        await backend.close()
        await backend.close()  # second close must not raise

    async def test_pinned_model_resolves_in_live_catalog(self):
        """Drift check: the model ID pinned by tests in this file
        (``deepseek-v4-flash``) must still appear in the live DeepSeek
        ``models.list()`` catalog. If DeepSeek retires it, this fails
        with a clear remediation message rather than a confusing 404
        from the next live test that tries to use it.

        Symmetric to ``test_alias_table_resolves_in_live_catalog`` for
        Anthropic. ``OpenAIBackend`` doesn't carry a model-alias table
        (DeepSeek model names are short enough that authors use them
        verbatim), so the drift target is the pinned ID inside this
        test file rather than a runtime dictionary.
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)
        try:
            catalog = await client.models.list()
        finally:
            await client.close()
        catalog_ids = {m.id for m in catalog.data}

        pinned = "deepseek-v4-flash"
        assert pinned in catalog_ids, (
            f"DeepSeek catalog no longer lists {pinned!r}. Update the "
            f"model parameter throughout `tests/unit/runtime/"
            f"test_openai_backend.py::TestDeepSeekLive` and "
            f"`tests/e2e/test_live_backend_roundtrip.py` to a current ID. "
            f"Live catalog ids: {sorted(catalog_ids)}"
        )


# ---------------------------------------------------------------------
# Error mapping (offline) — patch upstream SDK to raise, assert
# our wrapping converts to OverloadError.
# ---------------------------------------------------------------------

class _StubError(Exception):
    """Carries a status_code attribute the way openai's HTTP errors do."""

    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


class _FakeChatCompletions:
    """Stand-in for ``client.chat.completions``. Raises whatever the
    test wires into ``raises_with``."""

    def __init__(self, raises_with: Exception):
        self._exc = raises_with

    async def create(self, **kwargs):
        raise self._exc


class _FakeClient:
    def __init__(self, raises_with: Exception):
        self.chat = type(
            "_Chat", (), {"completions": _FakeChatCompletions(raises_with)},
        )()

    async def close(self) -> None:
        pass


def _backend_with_fake_client(exc: Exception) -> OpenAIBackend:
    """Construct an OpenAIBackend whose ._client is already wired to
    a fake that raises on completion creation. Skips the lazy-init
    path entirely."""
    backend = OpenAIBackend(api_key="sk-fake", base_url="http://test")
    backend._client = _FakeClient(exc)
    return backend


class TestErrorMapping:
    """Each transient-failure shape we care about must surface as
    ``OverloadError`` so the model-downgrade chain activates."""

    @pytest.mark.parametrize(
        "status_code", [429, 502, 503, 504, 529],
    )
    async def test_status_code_overload_maps_to_overload_error(self, status_code, tmp_path):
        backend = _backend_with_fake_client(_StubError(status_code))
        with pytest.raises(OverloadError):
            await backend.send_prompt(
                prompt="x", model="deepseek-v4-flash",
                workdir=str(tmp_path), timeout=10.0,
            )

    async def test_400_status_does_not_map_to_overload(self, tmp_path):
        """Bad-request errors are real failures, not transient — they
        must NOT be downgraded; let them propagate."""
        backend = _backend_with_fake_client(_StubError(400))
        with pytest.raises(_StubError):
            await backend.send_prompt(
                prompt="x", model="deepseek-v4-flash",
                workdir=str(tmp_path), timeout=10.0,
            )

    async def test_rate_limit_error_by_name_maps_to_overload(self, tmp_path):
        """Some openai SDK errors don't carry status_code — name-based
        fallback covers the common transient classes."""

        class RateLimitError(Exception):
            pass

        backend = _backend_with_fake_client(RateLimitError("rate limited"))
        with pytest.raises(OverloadError):
            await backend.send_prompt(
                prompt="x", model="deepseek-v4-flash",
                workdir=str(tmp_path), timeout=10.0,
            )

    async def test_api_connection_error_by_name_maps_to_overload(self, tmp_path):
        class APIConnectionError(Exception):
            pass

        backend = _backend_with_fake_client(APIConnectionError("DNS fail"))
        with pytest.raises(OverloadError):
            await backend.send_prompt(
                prompt="x", model="deepseek-v4-flash",
                workdir=str(tmp_path), timeout=10.0,
            )

    async def test_unrelated_exception_propagates(self, tmp_path):
        """A ValueError from inside our coroutine should NOT become
        OverloadError — only transient API errors do."""
        backend = _backend_with_fake_client(ValueError("typo in prompt"))
        with pytest.raises(ValueError):
            await backend.send_prompt(
                prompt="x", model="deepseek-v4-flash",
                workdir=str(tmp_path), timeout=10.0,
            )


class TestKeyResolution:
    """`_resolve_deepseek_key` is a thin wrapper for
    ``resolve_secret("DEEPSEEK_API_KEY")``. Full resolver semantics
    (yaml > env > .env, no machine-global keystore access) are
    pinned in ``tests/unit/runtime/test_secrets.py``; here we just
    exercise the env path."""

    def test_env_var_returns_value(self, monkeypatch):
        from abe_froman.runtime.secrets import _reset_dotenv_cache

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
        _reset_dotenv_cache()
        assert _resolve_deepseek_key() == "sk-from-env"

    def test_unset_returns_none(self, monkeypatch, tmp_path):
        from abe_froman.runtime.secrets import _reset_dotenv_cache

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        # CWD with no .env so the file fallback misses too.
        monkeypatch.chdir(tmp_path)
        _reset_dotenv_cache()
        assert _resolve_deepseek_key() is None
