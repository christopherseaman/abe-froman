"""Tests for PromptExecutor (Stage 5b API) and template rendering.

Stage 5b removed the legacy ``PromptExecutor.execute(node, context)``
method. The class now exposes a narrow surface used by
``DispatchExecutor._dispatch_prompt``:

  - ``apply_preamble(template) -> str | ExecutionResult``
  - ``execute_rendered(rendered, model, workdir, timeout) -> ExecutionResult``
  - ``close()``

Module-level helpers ``resolve_model``, ``downgrade_model``,
``render_template`` are also covered here. End-to-end prompt-execution
flow (node → prompt file fetch → render → backend) lives behind
``DispatchExecutor`` and is exercised here through that entry point.
"""

import pytest

from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.executor.prompt import (
    PromptExecutor,
    downgrade_model,
    render_template,
    resolve_model,
)
from abe_froman.runtime.executor.backends.stub import StubBackend
from abe_froman.runtime.result import (
    ExecutionResult,
    OverloadError,
    PromptBackend,
)
from abe_froman.schema.models import Execute, Node, Settings


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_substitutes_known_variables(self):
        result = render_template("Hello {{name}}", {"name": "world"})
        assert result == "Hello world"

    def test_unknown_variables_render_empty(self):
        """Jinja2 default Undefined renders missing vars as empty string."""
        result = render_template("Hello {{name}}", {})
        assert result == "Hello "

    def test_multiple_variables(self):
        result = render_template(
            "{{greeting}} {{name}}!", {"greeting": "Hi", "name": "Claude"}
        )
        assert result == "Hi Claude!"

    def test_spaces_in_braces(self):
        result = render_template("{{ name }}", {"name": "world"})
        assert result == "world"

    def test_no_placeholders(self):
        result = render_template("plain text", {"name": "world"})
        assert result == "plain text"

    def test_repeated_variable(self):
        result = render_template("{{x}} and {{x}}", {"x": "yes"})
        assert result == "yes and yes"

    def test_non_string_value_converted(self):
        result = render_template("count={{n}}", {"n": 42})
        assert result == "count=42"

    def test_value_containing_braces(self):
        """Substituted value containing {{ }} must not trigger double-substitution."""
        result = render_template("{{x}}", {"x": "{{y}}"})
        assert result == "{{y}}"

    def test_hyphenated_node_id_errors(self):
        """Jinja2 parses {{research-node}} as subtraction (research minus node).
        This is a known limitation — prompt templates must use underscores for
        node IDs that need substitution. Documented in CLAUDE.md."""
        from jinja2 import UndefinedError

        with pytest.raises((UndefinedError, TypeError)):
            render_template("{{research-node}}", {})


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_node_model_takes_priority(self):
        node = Node(
            id="p1", name="P1", model="opus",
            execute=Execute(url="t.md"),
        )
        settings = Settings(default_model="sonnet")
        assert resolve_model(node, settings) == "opus"

    def test_falls_back_to_settings_default(self):
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        settings = Settings(default_model="sonnet")
        assert resolve_model(node, settings) == "sonnet"


# ---------------------------------------------------------------------------
# In-test backend doubles (Protocol implementations, not unittest.mock)
# ---------------------------------------------------------------------------


class MemoryBackend:
    """Test backend that records calls and returns configurable responses."""

    def __init__(
        self,
        response: str = "backend-output",
        structured: dict | None = None,
    ):
        self._response = response
        self._structured = structured
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        return ExecutionResult(
            output=self._response,
            structured_output=self._structured,
        )

    async def close(self) -> None:
        pass


class ErrorBackend:
    """Backend that always raises."""

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        raise RuntimeError("connection failed")

    async def close(self) -> None:
        pass


class OverloadThenSucceedBackend:
    """Raises OverloadError until ``fail_count`` is reached, then succeeds.

    Records every send_prompt call (including the overloaded ones) so
    the test can assert on the model sequence the executor walked.
    """

    def __init__(self, fail_count: int = 1, response: str = "ok"):
        self._fail_count = fail_count
        self._response = response
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        if len(self.calls) <= self._fail_count:
            raise OverloadError("overloaded")
        return ExecutionResult(output=self._response)

    async def close(self) -> None:
        pass


class AlwaysOverloadBackend:
    """Raises OverloadError every call — exhausts the downgrade chain."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        raise OverloadError("overloaded")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# PromptExecutor.apply_preamble
# ---------------------------------------------------------------------------


class TestApplyPreamble:
    def test_no_preamble_returns_template_unchanged(self, tmp_path):
        executor = PromptExecutor(
            backend=MemoryBackend(),
            settings=Settings(),
            workdir=str(tmp_path),
        )
        result = executor.apply_preamble("body")
        assert result == "body"

    def test_preamble_prepended_with_separator(self, tmp_path):
        (tmp_path / "preamble.md").write_text("SHARED CONTEXT")
        executor = PromptExecutor(
            backend=MemoryBackend(),
            settings=Settings(preamble_file="preamble.md"),
            workdir=str(tmp_path),
        )
        result = executor.apply_preamble("Do the thing")
        assert result == "SHARED CONTEXT\n\nDo the thing"

    def test_missing_preamble_returns_error_result(self, tmp_path):
        executor = PromptExecutor(
            backend=MemoryBackend(),
            settings=Settings(preamble_file="missing.md"),
            workdir=str(tmp_path),
        )
        result = executor.apply_preamble("body")
        assert isinstance(result, ExecutionResult)
        assert result.success is False
        assert "Preamble file not found" in result.error

    def test_preamble_resolved_from_constructor_workdir(self, tmp_path):
        """Preamble lives with the config (constructor workdir), not any
        per-call worktree. The class has no per-call workdir for preamble."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "preamble.md").write_text("BASE PREAMBLE")
        executor = PromptExecutor(
            backend=MemoryBackend(),
            settings=Settings(preamble_file="preamble.md"),
            workdir=str(base),
        )
        result = executor.apply_preamble("X")
        assert result == "BASE PREAMBLE\n\nX"


# ---------------------------------------------------------------------------
# PromptExecutor.execute_rendered
# ---------------------------------------------------------------------------


class TestExecuteRendered:
    @pytest.mark.asyncio
    async def test_sends_rendered_prompt_to_backend(self, tmp_path):
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_model="sonnet"),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "rendered prompt", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is True
        assert result.output == "backend-output"
        assert backend.calls == [
            ("rendered prompt", "sonnet", str(tmp_path), None)
        ]

    @pytest.mark.asyncio
    async def test_model_argument_passed_through(self, tmp_path):
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_model="sonnet"),
            workdir=str(tmp_path),
        )
        await executor.execute_rendered(
            "x", "opus", str(tmp_path), timeout=None,
        )
        assert backend.calls[0][1] == "opus"

    @pytest.mark.asyncio
    async def test_timeout_threaded_to_backend(self, tmp_path):
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=15.5,
        )
        assert backend.calls[0][3] == 15.5

    @pytest.mark.asyncio
    async def test_timeout_none_passed_through(self, tmp_path):
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert backend.calls[0][3] is None

    @pytest.mark.asyncio
    async def test_per_call_workdir_forwarded_to_backend(self, tmp_path):
        """The workdir passed to execute_rendered is the one the backend
        sees — independent of the constructor workdir."""
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir="/should/not/leak",
        )
        await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert backend.calls[0][2] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_backend_error_returns_failure(self, tmp_path):
        executor = PromptExecutor(
            backend=ErrorBackend(),
            settings=Settings(),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert "connection failed" in result.error

    @pytest.mark.asyncio
    async def test_structured_output_passed_through(self, tmp_path):
        backend = MemoryBackend(response="text", structured={"key": "value"})
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is True
        assert result.structured_output == {"key": "value"}


# ---------------------------------------------------------------------------
# Overload → downgrade fallback (covers execute_rendered's retry loop)
# ---------------------------------------------------------------------------


class TestOverloadDowngrade:
    @pytest.mark.asyncio
    async def test_overload_downgrades_to_next_model(self, tmp_path):
        """First call raises OverloadError; executor downgrades and retries
        with the next model from the chain."""
        backend = OverloadThenSucceedBackend(fail_count=1, response="recovered")
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(
                model_downgrade_chain=["opus", "sonnet", "haiku"],
            ),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "opus", str(tmp_path), timeout=None,
        )
        assert result.success is True
        assert result.output == "recovered"
        # Two calls — first with opus, second with sonnet.
        assert [c[1] for c in backend.calls] == ["opus", "sonnet"]

    @pytest.mark.asyncio
    async def test_overload_walks_full_chain(self, tmp_path):
        """Overload at every step walks the entire chain before giving up."""
        backend = OverloadThenSucceedBackend(fail_count=2, response="haiku-ok")
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(
                model_downgrade_chain=["opus", "sonnet", "haiku"],
            ),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "opus", str(tmp_path), timeout=None,
        )
        assert result.success is True
        assert result.output == "haiku-ok"
        assert [c[1] for c in backend.calls] == ["opus", "sonnet", "haiku"]

    @pytest.mark.asyncio
    async def test_overload_exhausted_returns_failure(self, tmp_path):
        """OverloadError at every step exhausts chain → failure result
        naming the last model attempted."""
        backend = AlwaysOverloadBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(
                model_downgrade_chain=["opus", "sonnet", "haiku"],
            ),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "opus", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert "exhausted model chain" in result.error
        assert "haiku" in result.error
        assert [c[1] for c in backend.calls] == ["opus", "sonnet", "haiku"]

    @pytest.mark.asyncio
    async def test_unknown_starting_model_fails_immediately(self, tmp_path):
        """A starting model not in the chain → downgrade returns None →
        the executor cannot retry, surfaces the failure."""
        backend = AlwaysOverloadBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(
                model_downgrade_chain=["opus", "sonnet", "haiku"],
            ),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "gpt-4", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert "exhausted model chain" in result.error
        assert "gpt-4" in result.error
        # Backend was called exactly once with the unknown model.
        assert len(backend.calls) == 1
        assert backend.calls[0][1] == "gpt-4"


# ---------------------------------------------------------------------------
# downgrade_model
# ---------------------------------------------------------------------------


class TestDowngradeModel:
    CHAIN = ["opus", "sonnet", "haiku"]

    def test_downgrade_opus_to_sonnet(self):
        assert downgrade_model("opus", self.CHAIN) == "sonnet"

    def test_downgrade_sonnet_to_haiku(self):
        assert downgrade_model("sonnet", self.CHAIN) == "haiku"

    def test_downgrade_haiku_returns_none(self):
        assert downgrade_model("haiku", self.CHAIN) is None

    def test_downgrade_unknown_model_returns_none(self):
        assert downgrade_model("gpt-4", self.CHAIN) is None

    def test_custom_chain(self):
        assert downgrade_model("a", ["a", "b", "c"]) == "b"
        assert downgrade_model("c", ["a", "b", "c"]) is None


# ---------------------------------------------------------------------------
# StubBackend
# ---------------------------------------------------------------------------


class TestStubBackend:
    @pytest.mark.asyncio
    async def test_returns_stub_output(self):
        backend = StubBackend()
        result = await backend.send_prompt("hello world", "sonnet", ".")
        assert "prompt-stub" in result.output
        assert "model=sonnet" in result.output
        assert "prompt_length=11" in result.output

    @pytest.mark.asyncio
    async def test_satisfies_protocol(self):
        assert isinstance(StubBackend(), PromptBackend)


class TestMemoryBackendProtocol:
    def test_satisfies_protocol(self):
        assert isinstance(MemoryBackend(), PromptBackend)


# ---------------------------------------------------------------------------
# End-to-end prompt flow through DispatchExecutor (Stage 5b entry point)
#
# In Stage 5b, node → prompt-file → render → backend orchestration moved
# from PromptExecutor.execute() into DispatchExecutor._dispatch_prompt().
# These tests cover the full pipeline at that entry point.
# ---------------------------------------------------------------------------


class TestDispatchPromptFlow:
    @pytest.mark.asyncio
    async def test_reads_prompt_file_renders_and_sends(self, tmp_path):
        """End-to-end: file fetched, Jinja rendered, backend called."""
        prompt = tmp_path / "test.md"
        prompt.write_text("Hello {{name}}")

        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(default_model="sonnet"),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(
            node, {"name": "world"}, workdir=str(tmp_path),
        )

        assert result.success is True
        assert result.output == "backend-output"
        assert len(backend.calls) == 1
        prompt_sent, model, seen_workdir, timeout = backend.calls[0]
        assert prompt_sent == "Hello world"
        assert model == "sonnet"
        assert seen_workdir == str(tmp_path)
        assert timeout is None

    @pytest.mark.asyncio
    async def test_node_model_overrides_settings_default(self, tmp_path):
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(default_model="sonnet"),
        )
        node = Node(
            id="p1", name="P1", model="opus",
            execute=Execute(url=f"file://{prompt}"),
        )
        await executor.execute(node, {}, workdir=str(tmp_path))
        assert backend.calls[0][1] == "opus"

    @pytest.mark.asyncio
    async def test_params_model_overrides_node_model(self, tmp_path):
        """PromptParams.model is the highest-priority model selector."""
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(default_model="sonnet"),
        )
        node = Node(
            id="p1", name="P1", model="opus",
            execute=Execute(url=f"file://{prompt}", params={"model": "haiku"}),
        )
        await executor.execute(node, {}, workdir=str(tmp_path))
        assert backend.calls[0][1] == "haiku"

    @pytest.mark.asyncio
    async def test_node_timeout_threaded_to_backend(self, tmp_path):
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(default_timeout=60.0),
        )
        node = Node(
            id="p1", name="P1", timeout=15.5,
            execute=Execute(url=f"file://{prompt}"),
        )
        await executor.execute(node, {}, workdir=str(tmp_path))
        assert backend.calls[0][3] == 15.5

    @pytest.mark.asyncio
    async def test_settings_default_timeout_used_when_node_unset(self, tmp_path):
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(default_timeout=90.0),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        await executor.execute(node, {}, workdir=str(tmp_path))
        assert backend.calls[0][3] == 90.0

    @pytest.mark.asyncio
    async def test_no_timeout_configured_passes_none(self, tmp_path):
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        await executor.execute(node, {}, workdir=str(tmp_path))
        assert backend.calls[0][3] is None

    @pytest.mark.asyncio
    async def test_missing_prompt_file_returns_error(self, tmp_path):
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=MemoryBackend(),
            settings=Settings(),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{tmp_path}/missing.md"),
        )
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "Failed to fetch prompt" in result.error

    @pytest.mark.asyncio
    async def test_backend_error_surfaces_as_failure(self, tmp_path):
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=ErrorBackend(),
            settings=Settings(),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "connection failed" in result.error

    @pytest.mark.asyncio
    async def test_structured_output_propagates(self, tmp_path):
        prompt = tmp_path / "t.md"
        prompt.write_text("prompt")
        backend = MemoryBackend(response="text", structured={"key": "value"})
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.structured_output == {"key": "value"}


# ---------------------------------------------------------------------------
# Preamble injection through DispatchExecutor (end-to-end)
# ---------------------------------------------------------------------------


class TestPreambleInjection:
    @pytest.mark.asyncio
    async def test_preamble_prepended_to_prompt(self, tmp_path):
        (tmp_path / "preamble.md").write_text("SHARED CONTEXT")
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Do the thing")

        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(preamble_file="preamble.md"),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(node, {}, workdir=str(tmp_path))

        assert result.success is True
        assert backend.calls[0][0] == "SHARED CONTEXT\n\nDo the thing"

    @pytest.mark.asyncio
    async def test_preamble_with_template_variables(self, tmp_path):
        (tmp_path / "preamble.md").write_text("Preamble text")
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Use {{dep}} here")

        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(preamble_file="preamble.md"),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(
            node, {"dep": "injected"}, workdir=str(tmp_path),
        )

        assert result.success is True
        assert backend.calls[0][0] == "Preamble text\n\nUse injected here"

    @pytest.mark.asyncio
    async def test_preamble_file_not_found_returns_error(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("prompt")

        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(preamble_file="missing_preamble.md"),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(node, {}, workdir=str(tmp_path))

        assert result.success is False
        assert "Preamble file not found" in result.error

    @pytest.mark.asyncio
    async def test_no_preamble_setting_unchanged_behavior(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Just the prompt")

        backend = MemoryBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(),
        )
        node = Node(
            id="p1", name="P1",
            execute=Execute(url=f"file://{prompt}"),
        )
        result = await executor.execute(node, {}, workdir=str(tmp_path))

        assert result.success is True
        assert backend.calls[0][0] == "Just the prompt"

