"""Tests for PromptExecutor, template rendering, and StubBackend."""

import pytest

from abe_froman.runtime.executor.prompt import (
    PromptExecutor,
    downgrade_model,
    render_template,
    resolve_model,
)
from abe_froman.runtime.result import PromptBackend
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.executor.backends.stub import StubBackend
from abe_froman.schema.models import Phase, Settings


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

    def test_hyphenated_phase_id_errors(self):
        """Jinja2 parses {{research-phase}} as subtraction (research minus phase).
        This is a known limitation — prompt templates must use underscores for
        phase IDs that need substitution. Documented in CLAUDE.md."""
        from jinja2 import UndefinedError

        with pytest.raises((UndefinedError, TypeError)):
            render_template("{{research-phase}}", {})


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_phase_model_takes_priority(self):
        phase = Phase(id="p1", name="P1", model="opus", prompt_file="t.md")
        settings = Settings(default_model="sonnet")
        assert resolve_model(phase, settings) == "opus"

    def test_falls_back_to_settings_default(self):
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        settings = Settings(default_model="sonnet")
        assert resolve_model(phase, settings) == "sonnet"


# ---------------------------------------------------------------------------
# In-memory test backend
# ---------------------------------------------------------------------------


class MemoryBackend:
    """Test backend that records calls and returns configurable responses."""

    def __init__(
        self,
        response: str = "backend-output",
        structured: dict | None = None,
        tokens: dict[str, int] | None = None,
    ):
        self._response = response
        self._structured = structured
        self._tokens = tokens
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        return ExecutionResult(
            output=self._response,
            structured_output=self._structured,
            tokens_used=self._tokens,
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


# ---------------------------------------------------------------------------
# PromptExecutor
# ---------------------------------------------------------------------------


class TestPromptExecutor:
    @pytest.mark.asyncio
    async def test_reads_prompt_file_and_sends_to_backend(self, tmp_path):
        prompt_file = tmp_path / "test.md"
        prompt_file.write_text("Hello {{name}}")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_model="sonnet"),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="test.md")
        result = await executor.execute(phase, {"name": "world"})

        assert result.success is True
        assert result.output == "backend-output"
        assert len(backend.calls) == 1
        assert backend.calls[0] == ("Hello world", "sonnet", str(tmp_path), None)

    @pytest.mark.asyncio
    async def test_per_call_workdir_overrides_prompt_file_resolution(self, tmp_path):
        """Per-call workdir changes where the prompt_file is read from AND
        is forwarded to the backend as the effective workdir."""
        base = tmp_path / "base"
        base.mkdir()
        override = tmp_path / "override"
        override.mkdir()
        (base / "p.md").write_text("BASE prompt")
        (override / "p.md").write_text("OVERRIDE prompt")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_model="sonnet"),
            workdir=str(base),
        )
        phase = Phase(id="p", name="P", prompt_file="p.md")
        result = await executor.execute(phase, {}, workdir=str(override))

        assert result.success is True
        assert len(backend.calls) == 1
        prompt, _model, seen_workdir, _timeout = backend.calls[0]
        assert prompt == "OVERRIDE prompt"
        assert seen_workdir == str(override)

    @pytest.mark.asyncio
    async def test_per_call_workdir_none_falls_back_to_constructor(self, tmp_path):
        (tmp_path / "p.md").write_text("base prompt")
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend, settings=Settings(default_model="sonnet"),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p", name="P", prompt_file="p.md")
        await executor.execute(phase, {}, workdir=None)
        assert backend.calls[0][2] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_preamble_still_resolved_from_base_workdir(self, tmp_path):
        """Preamble lives with the config; a per-phase worktree override must
        NOT relocate preamble resolution."""
        base = tmp_path / "base"
        base.mkdir()
        override = tmp_path / "override"
        override.mkdir()
        (base / "preamble.md").write_text("SHARED PREAMBLE")
        (override / "p.md").write_text("phase prompt")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_model="sonnet", preamble_file="preamble.md"),
            workdir=str(base),
        )
        phase = Phase(id="p", name="P", prompt_file="p.md")
        result = await executor.execute(phase, {}, workdir=str(override))
        assert result.success is True
        assert backend.calls[0][0].startswith("SHARED PREAMBLE")
        assert "phase prompt" in backend.calls[0][0]

    @pytest.mark.asyncio
    async def test_missing_prompt_file_returns_error(self, tmp_path):
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="missing.md")
        result = await executor.execute(phase, {})

        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_phase_model_passed_to_backend(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_model="sonnet"),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", model="opus", prompt_file="t.md")
        await executor.execute(phase, {})

        assert backend.calls[0][1] == "opus"

    @pytest.mark.asyncio
    async def test_phase_timeout_threaded_to_backend(self, tmp_path):
        """Per-phase timeout flows through effective_timeout → send_prompt."""
        (tmp_path / "t.md").write_text("prompt")
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_timeout=60.0),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md", timeout=15.5)
        await executor.execute(phase, {})
        assert backend.calls[0][3] == 15.5

    @pytest.mark.asyncio
    async def test_settings_default_timeout_threaded_to_backend(self, tmp_path):
        """Phase without timeout override falls back to settings default."""
        (tmp_path / "t.md").write_text("prompt")
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(default_timeout=90.0),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        await executor.execute(phase, {})
        assert backend.calls[0][3] == 90.0

    @pytest.mark.asyncio
    async def test_no_timeout_configured_passes_none(self, tmp_path):
        """Neither phase nor settings set → backend sees timeout=None."""
        (tmp_path / "t.md").write_text("prompt")
        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend, settings=Settings(), workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        await executor.execute(phase, {})
        assert backend.calls[0][3] is None

    @pytest.mark.asyncio
    async def test_backend_error_returns_failure(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        executor = PromptExecutor(
            backend=ErrorBackend(),
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})

        assert result.success is False
        assert "connection failed" in result.error

    @pytest.mark.asyncio
    async def test_structured_output_from_backend(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        backend = MemoryBackend(response="text", structured={"key": "value"})
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})

        assert result.structured_output == {"key": "value"}

    @pytest.mark.asyncio
    async def test_tokens_used_threaded_to_phase_result(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        backend = MemoryBackend(
            response="output",
            tokens={"input": 500, "output": 120},
        )
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})

        assert result.success is True
        assert result.tokens_used == {"input": 500, "output": 120}

    @pytest.mark.asyncio
    async def test_tokens_used_none_when_backend_returns_none(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        backend = MemoryBackend(response="output")
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})

        assert result.success is True
        assert result.tokens_used is None

    @pytest.mark.asyncio
    async def test_wrong_execution_type_returns_error(self):
        backend = MemoryBackend()
        executor = PromptExecutor(backend=backend, settings=Settings())
        phase = Phase(
            id="p1", name="P1",
            execution={"type": "command", "command": "echo"},
        )
        result = await executor.execute(phase, {})

        assert result.success is False
        assert "CommandExecution" in result.error


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


# ---------------------------------------------------------------------------
# MemoryBackend satisfies Protocol
# ---------------------------------------------------------------------------


class TestMemoryBackendProtocol:
    def test_satisfies_protocol(self):
        assert isinstance(MemoryBackend(), PromptBackend)


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
# Preamble injection
# ---------------------------------------------------------------------------


class TestPreambleInjection:
    @pytest.mark.asyncio
    async def test_preamble_prepended_to_prompt(self, tmp_path):
        (tmp_path / "preamble.md").write_text("SHARED CONTEXT")
        (tmp_path / "prompt.md").write_text("Do the thing")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(preamble_file="preamble.md"),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="prompt.md")
        result = await executor.execute(phase, {})

        assert result.success is True
        assert backend.calls[0][0] == "SHARED CONTEXT\n\nDo the thing"

    @pytest.mark.asyncio
    async def test_preamble_with_template_variables(self, tmp_path):
        (tmp_path / "preamble.md").write_text("Preamble text")
        (tmp_path / "prompt.md").write_text("Use {{dep}} here")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(preamble_file="preamble.md"),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="prompt.md")
        result = await executor.execute(phase, {"dep": "injected"})

        assert result.success is True
        assert backend.calls[0][0] == "Preamble text\n\nUse injected here"

    @pytest.mark.asyncio
    async def test_preamble_file_not_found_returns_error(self, tmp_path):
        (tmp_path / "prompt.md").write_text("prompt")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(preamble_file="missing_preamble.md"),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="prompt.md")
        result = await executor.execute(phase, {})

        assert result.success is False
        assert "Preamble file not found" in result.error

    @pytest.mark.asyncio
    async def test_no_preamble_setting_unchanged_behavior(self, tmp_path):
        (tmp_path / "prompt.md").write_text("Just the prompt")

        backend = MemoryBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(id="p1", name="P1", prompt_file="prompt.md")
        result = await executor.execute(phase, {})

        assert result.success is True
        assert backend.calls[0][0] == "Just the prompt"

    def test_preamble_in_config_yaml(self):
        from abe_froman.schema.models import WorkflowConfig

        config = WorkflowConfig(
            name="test",
            version="1.0",
            phases=[Phase(id="p1", name="P1", prompt_file="t.md")],
            settings=Settings(preamble_file="preamble.md"),
        )
        assert config.settings.preamble_file == "preamble.md"


