"""Tests for AnthropicBackend (direct Anthropic Messages API).

Two layers, mirroring ``test_openai_backend.py``:

  - **Live** (artifact-driven): real Anthropic API call gated on key
    presence. Skipped with explicit reason when no key is on disk.
  - **Error mapping** (offline): patch the anthropic SDK's HTTP-call
    layer to raise specific exceptions; assert our wrapping code
    surfaces them as ``OverloadError``. We're testing **our** mapping
    logic, not the SDK — `feedback_no_fake_backends.md` forbids fake
    PromptBackend doubles, not patching upstream SDKs.
"""
from __future__ import annotations

import importlib.util

import pytest

from sqrlly.runtime.executor.backends.anthropic import (
    _MODEL_ALIASES,
    AnthropicBackend,
    _resolve_model,
)
from sqrlly.runtime.executor.backends.factory import _resolve_anthropic_key
from sqrlly.runtime.result import OverloadError


# ---------------------------------------------------------------------
# Live Anthropic API tests (gated on key availability)
# ---------------------------------------------------------------------

ANTHROPIC_KEY = _resolve_anthropic_key()
_ANTHROPIC_SDK_INSTALLED = importlib.util.find_spec("anthropic") is not None
LIVE_REASON = (
    "Anthropic API key not available "
    "(set ANTHROPIC_API_KEY in the environment; see .env.example)"
)
_SDK_REASON = "anthropic SDK not installed (uv sync --extra anthropic)"


@pytest.mark.live
@pytest.mark.skipif(ANTHROPIC_KEY is None, reason=LIVE_REASON)
@pytest.mark.skipif(not _ANTHROPIC_SDK_INSTALLED, reason=_SDK_REASON)
class TestAnthropicLive:
    """Real network calls to Anthropic — proves the wire-level path
    works end-to-end, including model-alias resolution."""

    async def test_send_prompt_returns_text(self, tmp_path):
        backend = AnthropicBackend(api_key=ANTHROPIC_KEY)
        try:
            result = await backend.send_prompt(
                prompt="Reply with the single word: pong",
                model="haiku",  # cheapest tier; alias resolves to vendor ID
                workdir=str(tmp_path),
                timeout=30.0,
            )
        finally:
            await backend.close()

        assert result.success is True
        assert result.output  # non-empty
        assert "pong" in result.output.lower()

    async def test_alias_table_resolves_in_live_catalog(self, tmp_path):
        """Drift check: every vendor ID in ``_MODEL_ALIASES`` must still
        appear in the live Anthropic ``models.list()`` catalog. If
        Anthropic deprecates a model we have hardcoded as a generic
        family alias (e.g. ``"sonnet" -> "claude-sonnet-4-6"``), this
        fails with a clear remediation message: update the table to
        the current headline ID for that family.

        Same general pattern applies to any other backend with a model
        alias table — for now Anthropic is the only one (the OpenAI
        backend passes model strings through verbatim).
        """
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        try:
            catalog = await client.models.list()
        finally:
            await client.close()
        catalog_ids = {m.id for m in catalog.data}

        stale: dict[str, str] = {}
        for alias, vendor_id in _MODEL_ALIASES.items():
            if vendor_id not in catalog_ids:
                stale[alias] = vendor_id

        assert not stale, (
            f"_MODEL_ALIASES has stale vendor IDs no longer in the live "
            f"Anthropic catalog: {stale}. Update "
            f"`runtime/executor/backends/anthropic.py::_MODEL_ALIASES` "
            f"to the current headline IDs. Live catalog ids include: "
            f"{sorted(i for i in catalog_ids if 'claude' in i.lower())}"
        )

    async def test_close_is_idempotent(self, tmp_path):
        backend = AnthropicBackend(api_key=ANTHROPIC_KEY)
        await backend.send_prompt(
            prompt="say hi", model="haiku",
            workdir=str(tmp_path), timeout=30.0,
        )
        await backend.close()
        await backend.close()  # second close must not raise


# ---------------------------------------------------------------------
# Model alias resolution
# ---------------------------------------------------------------------

class TestModelResolution:
    """Generic shorthand → vendor ID; pass-through preserves explicit
    pins."""

    @pytest.mark.parametrize(
        "alias, expected",
        [
            ("sonnet", "claude-sonnet-4-6"),
            ("opus", "claude-opus-4-7"),
            ("haiku", "claude-haiku-4-5-20251001"),
        ],
    )
    def test_generic_aliases_resolve(self, alias, expected):
        assert _resolve_model(alias) == expected

    @pytest.mark.parametrize(
        "explicit",
        [
            "claude-sonnet-4-6-20250821",  # pinned snapshot
            "claude-opus-4-7",             # already vendor ID
            "claude-3-5-sonnet-20241022",  # legacy ID
            "custom-fine-tune-id",         # truly arbitrary
        ],
    )
    def test_unknown_strings_pass_through(self, explicit):
        assert _resolve_model(explicit) == explicit


# ---------------------------------------------------------------------
# Error mapping (offline) — patch upstream SDK to raise, assert
# our wrapping converts to OverloadError.
# ---------------------------------------------------------------------

class _StubError(Exception):
    """Carries a status_code attribute the way anthropic's HTTP errors
    do."""

    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


class _FakeMessages:
    """Stand-in for ``client.messages``. Raises whatever the test wires
    into ``raises_with``, or returns ``response`` on success."""

    def __init__(self, raises_with: Exception | None = None, response: object = None):
        self._exc = raises_with
        self._response = response

    async def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeClient:
    def __init__(self, raises_with: Exception | None = None, response: object = None):
        self.messages = _FakeMessages(raises_with=raises_with, response=response)

    async def close(self) -> None:
        pass


def _backend_with_fake_client(
    exc: Exception | None = None, response: object = None,
) -> AnthropicBackend:
    """Construct an AnthropicBackend whose ._client is already wired to
    a fake. Skips the lazy-init path entirely — no real anthropic
    package import required."""
    backend = AnthropicBackend(api_key="sk-fake")
    backend._client = _FakeClient(raises_with=exc, response=response)
    return backend


class TestErrorMapping:
    """Each transient-failure shape we care about must surface as
    ``OverloadError`` so the model-downgrade chain activates."""

    @pytest.mark.parametrize(
        "status_code", [429, 502, 503, 504, 529],
    )
    async def test_status_code_overload_maps_to_overload_error(
        self, status_code, tmp_path,
    ):
        backend = _backend_with_fake_client(_StubError(status_code))
        with pytest.raises(OverloadError):
            await backend.send_prompt(
                prompt="x", model="haiku",
                workdir=str(tmp_path), timeout=10.0,
            )

    async def test_400_status_does_not_map_to_overload(self, tmp_path):
        """Bad-request errors are real failures, not transient — they
        must NOT be downgraded; let them propagate."""
        backend = _backend_with_fake_client(_StubError(400))
        with pytest.raises(_StubError):
            await backend.send_prompt(
                prompt="x", model="haiku",
                workdir=str(tmp_path), timeout=10.0,
            )

    @pytest.mark.parametrize(
        "exc_class_name",
        [
            "RateLimitError",
            "APIConnectionError",
            "APITimeoutError",
            "InternalServerError",
        ],
    )
    async def test_exception_class_name_maps_to_overload(
        self, exc_class_name, tmp_path,
    ):
        """Anthropic SDK transient-failure classes (verified against
        anthropic 0.99.0) — name-based fallback covers cases where the
        exception doesn't carry a numeric ``status_code``."""
        exc_class = type(exc_class_name, (Exception,), {})
        backend = _backend_with_fake_client(exc_class("transient"))
        with pytest.raises(OverloadError):
            await backend.send_prompt(
                prompt="x", model="haiku",
                workdir=str(tmp_path), timeout=10.0,
            )

    async def test_unrelated_exception_propagates(self, tmp_path):
        """A ValueError from inside our coroutine should NOT become
        OverloadError — only transient API errors do."""
        backend = _backend_with_fake_client(ValueError("typo in prompt"))
        with pytest.raises(ValueError):
            await backend.send_prompt(
                prompt="x", model="haiku",
                workdir=str(tmp_path), timeout=10.0,
            )


# ---------------------------------------------------------------------
# Response shape handling — content-block list semantics
# ---------------------------------------------------------------------

class _FakeBlock:
    """Stand-in for the SDK's content block (TextBlock / ToolUseBlock).

    Real blocks expose ``.type`` and (for text) ``.text`` attributes.
    We mirror that minimal contract.
    """

    def __init__(self, type: str, text: str | None = None):
        self.type = type
        if text is not None:
            self.text = text


class _FakeResponse:
    def __init__(self, content: list):
        self.content = content


class TestResponseHandling:
    """The Messages API returns a list of content blocks; the backend
    must handle the empty-list, no-text-blocks, and multi-block cases
    distinctly."""

    async def test_text_block_returns_output(self, tmp_path):
        resp = _FakeResponse([_FakeBlock("text", "hello world")])
        backend = _backend_with_fake_client(response=resp)
        result = await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        assert result.success is True
        assert result.output == "hello world"

    async def test_multiple_text_blocks_concatenate(self, tmp_path):
        resp = _FakeResponse([
            _FakeBlock("text", "part one "),
            _FakeBlock("text", "part two"),
        ])
        backend = _backend_with_fake_client(response=resp)
        result = await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        assert result.success is True
        assert result.output == "part one part two"

    async def test_text_blocks_among_other_block_types(self, tmp_path):
        """Tool-use blocks coexisting with text blocks: only text is
        extracted."""
        resp = _FakeResponse([
            _FakeBlock("tool_use"),
            _FakeBlock("text", "the answer"),
            _FakeBlock("tool_use"),
        ])
        backend = _backend_with_fake_client(response=resp)
        result = await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        assert result.success is True
        assert result.output == "the answer"

    async def test_empty_content_returns_failure(self, tmp_path):
        """Empty content list — likely refusal/filter/truncation.
        Surfaced as ExecutionResult(success=False, error=...)."""
        resp = _FakeResponse([])
        backend = _backend_with_fake_client(response=resp)
        result = await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        assert result.success is False
        assert "no content blocks" in result.error

    async def test_no_text_blocks_returns_failure(self, tmp_path):
        """Content blocks present but none of type 'text' — also a
        failure (the prompt-mode contract requires text output)."""
        resp = _FakeResponse([_FakeBlock("tool_use")])
        backend = _backend_with_fake_client(response=resp)
        result = await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        assert result.success is False
        assert "non-empty text block" in result.error

    async def test_empty_text_block_returns_failure(self, tmp_path):
        """Asymmetry guard: a text block with `text=""` (refusal /
        truncation / filter edge case) must not silently succeed
        with empty output. Treat it like no text block at all."""
        resp = _FakeResponse([_FakeBlock("text", "")])
        backend = _backend_with_fake_client(response=resp)
        result = await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        assert result.success is False
        assert "non-empty text block" in result.error


# ---------------------------------------------------------------------
# Lifecycle: lazy init, idempotent close, missing-package error
# ---------------------------------------------------------------------

class TestLifecycle:
    async def test_close_without_send_is_noop(self):
        """Constructing then closing without ever sending must not
        error or attempt to import the SDK."""
        backend = AnthropicBackend(api_key="sk-fake")
        assert backend._client is None  # lazy: not constructed yet
        await backend.close()
        assert backend._client is None  # close didn't trigger init

    async def test_close_is_idempotent_with_fake_client(self, tmp_path):
        resp = _FakeResponse([_FakeBlock("text", "hi")])
        backend = _backend_with_fake_client(response=resp)
        await backend.send_prompt(
            prompt="x", model="haiku",
            workdir=str(tmp_path), timeout=10.0,
        )
        await backend.close()
        await backend.close()  # second close: client is None, no-op
