import asyncio

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.base import PhaseResult
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.schema.models import Phase, Settings

from helpers import cmd_phase, make_config
from mock_executor import MockExecutor


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestTimeoutSchema:
    def test_phase_timeout_field(self):
        p = Phase(id="a", name="A", timeout=30.0)
        assert p.timeout == 30.0

    def test_phase_timeout_defaults_none(self):
        p = Phase(id="a", name="A")
        assert p.timeout is None

    def test_settings_default_timeout(self):
        s = Settings(default_timeout=60.0)
        assert s.default_timeout == 60.0

    def test_settings_default_timeout_defaults_none(self):
        s = Settings()
        assert s.default_timeout is None

    def test_effective_timeout_phase_overrides_settings(self):
        s = Settings(default_timeout=60.0)
        p = Phase(id="a", name="A", timeout=10.0)
        assert p.effective_timeout(s) == 10.0

    def test_effective_timeout_falls_back_to_settings(self):
        s = Settings(default_timeout=60.0)
        p = Phase(id="a", name="A")
        assert p.effective_timeout(s) == 60.0

    def test_effective_timeout_both_none(self):
        s = Settings()
        p = Phase(id="a", name="A")
        assert p.effective_timeout(s) is None


# ---------------------------------------------------------------------------
# Integration tests — command phases with real subprocesses
# ---------------------------------------------------------------------------


class TestTimeoutCommandPhase:
    @pytest.mark.asyncio
    async def test_timeout_kills_hung_phase(self, tmp_path):
        config = make_config(
            [
                {
                    "id": "slow",
                    "name": "Slow",
                    "execution": {"type": "command", "command": "sleep", "args": ["10"]},
                    "timeout": 0.5,
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "slow" in result["failed_phases"]
        assert any("timed out" in e["error"] for e in result["errors"])

    @pytest.mark.asyncio
    async def test_no_timeout_allows_completion(self, tmp_path):
        config = make_config(
            [cmd_phase("fast", output="hello")],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "fast" in result["completed_phases"]
        assert result["phase_outputs"]["fast"] == "hello"

    @pytest.mark.asyncio
    async def test_default_timeout_from_settings(self, tmp_path):
        config = make_config(
            [
                {
                    "id": "slow",
                    "name": "Slow",
                    "execution": {"type": "command", "command": "sleep", "args": ["10"]},
                },
            ],
            default_timeout=0.5,
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "slow" in result["failed_phases"]
        assert any("timed out" in e["error"] for e in result["errors"])


# ---------------------------------------------------------------------------
# Integration tests — gate validator timeout
# ---------------------------------------------------------------------------


class TestTimeoutGateValidator:
    @pytest.mark.asyncio
    async def test_timeout_on_gate_validator(self, tmp_path):
        slow_validator = tmp_path / "slow_gate.py"
        slow_validator.write_text("import time; time.sleep(10); print('1.0')")

        config = make_config(
            [
                {
                    "id": "gated",
                    "name": "Gated",
                    "execution": {"type": "command", "command": "echo", "args": ["-n", "ok"]},
                    "quality_gate": {
                        "validator": str(slow_validator),
                        "threshold": 0.8,
                        "blocking": True,
                        "max_retries": 0,
                    },
                    "timeout": 0.5,
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "gated" in result["failed_phases"]
        assert any("gate timed out" in e["error"].lower() for e in result["errors"])


# ---------------------------------------------------------------------------
# Integration tests — mock executor (slow prompt phases)
# ---------------------------------------------------------------------------


class SlowMockExecutor:
    """Mock executor that sleeps before returning."""

    def __init__(self, delay: float):
        self._delay = delay

    async def execute(self, phase, context):
        await asyncio.sleep(self._delay)
        return PhaseResult(success=True, output=f"[slow-mock] {phase.id}")


class TestTimeoutPromptPhase:
    @pytest.mark.asyncio
    async def test_timeout_on_slow_executor(self, tmp_path):
        config = make_config(
            [
                {
                    "id": "slow_prompt",
                    "name": "Slow Prompt",
                    "execution": {"type": "gate_only"},
                    "timeout": 0.3,
                },
            ]
        )
        slow_executor = SlowMockExecutor(delay=5.0)
        graph = build_workflow_graph(config, slow_executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "slow_prompt" in result["failed_phases"]
        assert any("timed out" in e["error"] for e in result["errors"])

    @pytest.mark.asyncio
    async def test_fast_executor_completes_within_timeout(self, tmp_path):
        config = make_config(
            [
                {
                    "id": "fast_prompt",
                    "name": "Fast Prompt",
                    "execution": {"type": "gate_only"},
                    "timeout": 5.0,
                },
            ]
        )
        fast_executor = SlowMockExecutor(delay=0.01)
        graph = build_workflow_graph(config, fast_executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "fast_prompt" in result["completed_phases"]


# ---------------------------------------------------------------------------
# Integration tests — subphase timeout inheritance
# ---------------------------------------------------------------------------


class SelectiveSlowExecutor:
    """Executor that is fast for parent phases, slow for subphases."""

    def __init__(self, slow_delay: float):
        self._slow_delay = slow_delay

    async def execute(self, phase, context):
        if "::" in phase.id:
            await asyncio.sleep(self._slow_delay)
        return PhaseResult(
            success=True,
            output=f"[mock] {phase.id}",
        )


class TestSubphaseTimeout:
    @pytest.mark.asyncio
    async def test_subphase_inherits_parent_timeout(self, tmp_path):
        import json

        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps([{"id": "item1", "name": "Item 1"}]))

        prompt = tmp_path / "template.md"
        prompt.write_text("Process {{name}}")

        config = make_config(
            [
                {
                    "id": "parent",
                    "name": "Parent",
                    "execution": {"type": "command", "command": "echo", "args": ["-n", "ok"]},
                    "timeout": 0.3,
                    "dynamic_subphases": {
                        "enabled": True,
                        "manifest_path": "manifest.json",
                        "template": {"prompt_file": "template.md"},
                    },
                },
            ]
        )
        executor = SelectiveSlowExecutor(slow_delay=5.0)
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        subphase_id = "parent::item1"
        assert subphase_id in result["failed_phases"]
        assert any("timed out" in e["error"] for e in result["errors"])
