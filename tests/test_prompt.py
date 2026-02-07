"""Tests for PromptExecutor, template rendering, and StubBackend."""

import pytest

from abe_froman.executor.base import PhaseResult
from abe_froman.executor.prompt import PromptExecutor, render_template, resolve_model
from abe_froman.executor.prompt_backend import PromptBackend, PromptBackendResult
from abe_froman.executor.backends.stub import StubBackend
from abe_froman.schema.models import Phase, Settings


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_substitutes_known_variables(self):
        result = render_template("Hello {{name}}", {"name": "world"})
        assert result == "Hello world"

    def test_leaves_unknown_variables_intact(self):
        result = render_template("Hello {{name}}", {})
        assert result == "Hello {{name}}"

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

    def __init__(self, response: str = "backend-output", structured: dict | None = None):
        self._response = response
        self._structured = structured
        self.calls: list[tuple[str, str, str]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str
    ) -> PromptBackendResult:
        self.calls.append((prompt, model, workdir))
        return PromptBackendResult(output=self._response, structured_output=self._structured)

    async def close(self) -> None:
        pass


class ErrorBackend:
    """Backend that always raises."""

    async def send_prompt(self, prompt: str, model: str, workdir: str) -> PromptBackendResult:
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
        assert backend.calls[0] == ("Hello world", "sonnet", str(tmp_path))

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
    async def test_json_parsed_when_output_schema_set(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        backend = MemoryBackend(response='{"score": 0.95}')
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            output_schema={"type": "object"},
        )
        result = await executor.execute(phase, {})

        assert result.structured_output == {"score": 0.95}

    @pytest.mark.asyncio
    async def test_non_json_output_with_schema_leaves_structured_none(self, tmp_path):
        prompt_file = tmp_path / "t.md"
        prompt_file.write_text("prompt")

        backend = MemoryBackend(response="not json")
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(),
            workdir=str(tmp_path),
        )
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            output_schema={"type": "object"},
        )
        result = await executor.execute(phase, {})

        assert result.success is True
        assert result.structured_output is None

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
