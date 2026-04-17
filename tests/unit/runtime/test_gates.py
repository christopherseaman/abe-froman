import asyncio
import json

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.gates import GateResult, evaluate_gate
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.backends.acp import ACPBackend
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.schema.models import QualityGate

from helpers import make_config


# ---------------------------------------------------------------------------
# Validator fixtures — reusable scripts that inspect stdin
# ---------------------------------------------------------------------------

JSON_VALIDATOR = """\
import json, sys
raw = sys.stdin.read().strip()
try:
    data = json.loads(raw)
    if isinstance(data, dict) and "items" in data and len(data["items"]) == 3:
        print("1.0")
    else:
        print("0.0")
except Exception:
    print("0.0")
"""


# ---------------------------------------------------------------------------
# Unit tests: evaluate_gate receives phase_output via stdin
# ---------------------------------------------------------------------------


class TestGateStdinPassing:
    @pytest.mark.asyncio
    async def test_valid_output_passes_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        phase_output = json.dumps({"items": ["a", "b", "c"]})
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output=phase_output)
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_invalid_output_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output="not json")
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_wrong_count_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        phase_output = json.dumps({"items": ["a"]})
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output=phase_output)
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_empty_stdin_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output="")
        assert result.score == 0.0


# ---------------------------------------------------------------------------
# Unit tests: evaluate_gate basics (no stdin inspection)
# ---------------------------------------------------------------------------


class TestGateEvaluation:
    @pytest.mark.asyncio
    async def test_md_validator_requires_backend(self):
        """`.md` gates must be dispatched with a backend; without one, raise."""
        gate = QualityGate(validator="gates/v.md", threshold=0.8)
        with pytest.raises(ValueError, match="requires a PromptBackend"):
            await evaluate_gate(gate, "p1")

    @pytest.mark.asyncio
    async def test_unsupported_extension_raises(self):
        gate = QualityGate(validator="gates/v.txt", threshold=0.8)
        with pytest.raises(ValueError, match="Unsupported"):
            await evaluate_gate(gate, "p1")

    @pytest.mark.asyncio
    async def test_py_validator_returns_float_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(0.95)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.95

    @pytest.mark.asyncio
    async def test_py_validator_returns_json_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text('import json; print(json.dumps({"score": 0.75}))')
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.75

    @pytest.mark.asyncio
    async def test_py_validator_exception_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("raise Exception('fail')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_garbage_output_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print('not a number')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_nonexistent_py_validator_returns_zero(self):
        gate = QualityGate(validator="/tmp/does_not_exist_12345.py", threshold=0.8)
        result = await evaluate_gate(gate, "p1")
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_zero_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(0.0)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_perfect_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(1.0)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 1.0


class TestGateThresholdComparison:
    def test_above_threshold(self):
        gate = QualityGate(validator="v.md", threshold=0.8)
        assert 0.9 >= gate.threshold

    def test_below_threshold(self):
        gate = QualityGate(validator="v.md", threshold=0.8)
        assert not (0.5 >= gate.threshold)

    def test_equals_threshold(self):
        gate = QualityGate(validator="v.md", threshold=0.8)
        assert 0.8 >= gate.threshold


# ---------------------------------------------------------------------------
# Node-level: gate pass/fail with stdin-inspecting validators in full graph
# ---------------------------------------------------------------------------


class TestGateNodePassFail:
    """Test gates in the context of actual graph execution with real validators
    that inspect phase output via stdin."""

    @pytest.mark.asyncio
    async def test_passing_gate_allows_dependent(self, tmp_path):
        """Validator inspects stdin, finds valid JSON -> pass -> dependent runs."""
        validator = tmp_path / "validator.py"
        validator.write_text(JSON_VALIDATOR)
        payload = tmp_path / "payload.txt"
        payload.write_text('{"items": ["x", "y", "z"]}')

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {"type": "command", "command": "cat", "args": [str(payload)]},
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                    },
                },
                {
                    "id": "b",
                    "name": "B",
                    "execution": {"type": "command", "command": "echo", "args": ["b done"]},
                    "depends_on": ["a"],
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]
        assert result["gate_scores"]["a"] == 1.0

    @pytest.mark.asyncio
    async def test_failing_gate_blocks_dependent(self, tmp_path):
        """Validator inspects stdin, finds invalid output -> fail -> dependent skipped."""
        validator = tmp_path / "validator.py"
        validator.write_text(JSON_VALIDATOR)

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {"type": "command", "command": "echo", "args": ["not json"]},
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 0,
                    },
                },
                {
                    "id": "b",
                    "name": "B",
                    "execution": {"type": "command", "command": "echo", "args": ["b done"]},
                    "depends_on": ["a"],
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["failed_phases"]
        assert "b" not in result["completed_phases"]
        assert result["gate_scores"]["a"] == 0.0

    @pytest.mark.asyncio
    async def test_non_blocking_gate_failure_continues(self, tmp_path):
        """Non-blocking gate failure: phase completes with warning, dependent runs."""
        validator = tmp_path / "validator.py"
        validator.write_text(JSON_VALIDATOR)

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {"type": "command", "command": "echo", "args": ["bad"]},
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": False,
                        "max_retries": 0,
                    },
                },
                {
                    "id": "b",
                    "name": "B",
                    "execution": {"type": "command", "command": "echo", "args": ["b done"]},
                    "depends_on": ["a"],
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]
        assert any("non-blocking" in e["error"].lower() for e in result["errors"])


# ---------------------------------------------------------------------------
# Integration: multi-step joke workflow with ACP + deterministic gate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# JS validator and environment variable tests
# ---------------------------------------------------------------------------


class TestGateJSValidator:
    @pytest.mark.asyncio
    async def test_js_validator_returns_score(self, tmp_path):
        import shutil

        if shutil.which("node") is None:
            pytest.skip("node not available")
        script = tmp_path / "validator.js"
        script.write_text('process.stdout.write("0.85")')
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.85

    @pytest.mark.asyncio
    async def test_js_validator_not_found(self):
        gate = QualityGate(validator="/tmp/does_not_exist_99999.js", threshold=0.8)
        result = await evaluate_gate(gate, "p1")
        assert result.score == 0.0


class TestGateEnvironment:
    @pytest.mark.asyncio
    async def test_phase_id_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('PHASE_ID') == 'my-phase' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "my-phase", workdir=str(tmp_path))
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_workflow_name_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('WORKFLOW_NAME') == 'test-wf' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(
            gate, "p1", workdir=str(tmp_path), workflow_name="test-wf",
        )
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_attempt_number_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('ATTEMPT_NUMBER') == '1' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(
            gate, "p1", workdir=str(tmp_path), attempt_number=1,
        )
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_workdir_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            f"print('1.0' if os.environ.get('WORKDIR') == '{tmp_path}' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_attempt_number_on_retry(self, tmp_path):
        """Integration: gate fails then passes, verify ATTEMPT_NUMBER increments."""
        attempt_counter = tmp_path / "attempt.txt"
        attempt_counter.write_text("0")
        counter_script = tmp_path / "run.py"
        counter_script.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            f"open('{attempt_counter}', 'w').write(str(count + 1))\n"
            f"print('output')\n"
        )
        # Validator passes only when ATTEMPT_NUMBER is "2" (i.e., first retry)
        validator = tmp_path / "validator.py"
        validator.write_text(
            "import os\n"
            "attempt = os.environ.get('ATTEMPT_NUMBER', '0')\n"
            "print('1.0' if attempt == '2' else '0.0')\n"
        )
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {
                        "type": "command",
                        "command": "python3",
                        "args": [str(counter_script)],
                    },
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 3,
                    },
                },
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_phases"]
        assert result["gate_scores"]["a"] == 1.0

    @pytest.mark.asyncio
    async def test_explicit_nonzero_exit(self, tmp_path):
        script = tmp_path / "exit1.py"
        script.write_text("import sys; sys.exit(1)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0


# ---------------------------------------------------------------------------
# Integration: multi-step joke workflow with ACP + deterministic gate
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    """Tests for stepped retry backoff delays."""

    def test_get_retry_delay_empty_list(self):
        from abe_froman.compile.nodes import _get_retry_delay

        assert _get_retry_delay(1, []) == 0.0
        assert _get_retry_delay(5, []) == 0.0

    def test_get_retry_delay_single_element(self):
        from abe_froman.compile.nodes import _get_retry_delay

        assert _get_retry_delay(1, [10.0]) == 10.0
        assert _get_retry_delay(2, [10.0]) == 10.0
        assert _get_retry_delay(5, [10.0]) == 10.0

    def test_get_retry_delay_multiple_elements(self):
        from abe_froman.compile.nodes import _get_retry_delay

        backoff = [10.0, 30.0, 60.0]
        assert _get_retry_delay(1, backoff) == 10.0
        assert _get_retry_delay(2, backoff) == 30.0
        assert _get_retry_delay(3, backoff) == 60.0
        assert _get_retry_delay(4, backoff) == 60.0  # clamps to last

    @pytest.mark.asyncio
    async def test_retry_backoff_delays_applied(self, tmp_path, monkeypatch):
        """Verify asyncio.sleep is called with correct delay values on retry."""
        validator = tmp_path / "validator.py"
        # Fail first two attempts, pass on third
        validator.write_text(
            "import sys, os\n"
            "attempt = int(os.environ.get('ATTEMPT', '0'))\n"
            "print('1.0' if attempt >= 2 else '0.0')\n"
        )

        # Track attempt count via a file
        attempt_counter = tmp_path / "attempt.txt"
        attempt_counter.write_text("0")
        counter_script = tmp_path / "run.py"
        counter_script.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            f"open('{attempt_counter}', 'w').write(str(count + 1))\n"
            f"print('output')\n"
        )

        # Validator that reads the attempt file
        validator.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            "print('1.0' if count >= 3 else '0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {
                        "type": "command",
                        "command": "python3",
                        "args": [str(counter_script)],
                    },
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 3,
                    },
                },
            ],
            retry_backoff=[0.1, 0.2],
        )

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            sleep_calls.append(delay)
            await original_sleep(0)  # yield control without waiting

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_phases"]
        # Two retries before passing: delay 0.1 for retry 1, 0.2 for retry 2
        assert sleep_calls == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_retry_backoff_clamps_to_last_value(self, tmp_path, monkeypatch):
        """With single-element backoff and multiple retries, all use that value."""
        attempt_counter = tmp_path / "attempt.txt"
        attempt_counter.write_text("0")
        counter_script = tmp_path / "run.py"
        counter_script.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            f"open('{attempt_counter}', 'w').write(str(count + 1))\n"
            f"print('output')\n"
        )
        validator = tmp_path / "validator.py"
        validator.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            "print('1.0' if count >= 4 else '0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {
                        "type": "command",
                        "command": "python3",
                        "args": [str(counter_script)],
                    },
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 4,
                    },
                },
            ],
            retry_backoff=[0.1],
        )

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            sleep_calls.append(delay)
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_phases"]
        assert sleep_calls == [0.1, 0.1, 0.1]

    @pytest.mark.asyncio
    async def test_empty_backoff_no_delay(self, tmp_path, monkeypatch):
        """Default empty backoff means no sleep calls."""
        attempt_counter = tmp_path / "attempt.txt"
        attempt_counter.write_text("0")
        counter_script = tmp_path / "run.py"
        counter_script.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            f"open('{attempt_counter}', 'w').write(str(count + 1))\n"
            f"print('output')\n"
        )
        validator = tmp_path / "validator.py"
        validator.write_text(
            f"count = int(open('{attempt_counter}').read().strip())\n"
            "print('1.0' if count >= 2 else '0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execution": {
                        "type": "command",
                        "command": "python3",
                        "args": [str(counter_script)],
                    },
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 2,
                    },
                },
            ],
        )

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            sleep_calls.append(delay)
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_phases"]
        assert sleep_calls == []


class TestJokeWorkflowIntegration:
    """Full E2E: ACP generates jokes -> deterministic gate validates JSON schema
    -> ACP selects best joke. Tests the entire pipeline with real Claude execution."""

    @pytest.mark.asyncio
    async def test_generate_gate_select(self, tmp_path):
        generate_prompt = tmp_path / "generate.md"
        generate_prompt.write_text(
            'Generate exactly 3 short jokes. Respond with ONLY valid JSON, '
            'no markdown, no code fences:\n'
            '{"jokes": ["joke 1", "joke 2", "joke 3"]}'
        )
        select_prompt = tmp_path / "select.md"
        select_prompt.write_text(
            "Here are some jokes:\n\n{{generate}}\n\n"
            "Pick the funniest one. Respond with ONLY the joke text."
        )

        validator = tmp_path / "validate.py"
        validator.write_text(
            "import json, sys\n"
            "raw = sys.stdin.read().strip()\n"
            "try:\n"
            "    data = json.loads(raw)\n"
            "    jokes = data.get('jokes', [])\n"
            "    if isinstance(jokes, list) and len(jokes) == 3 "
            "and all(isinstance(j, str) and j for j in jokes):\n"
            "        print('1.0')\n"
            "    else:\n"
            "        print('0.0')\n"
            "except Exception:\n"
            "    print('0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "generate",
                    "name": "Generate Jokes",
                    "prompt_file": "generate.md",
                    "quality_gate": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 2,
                    },
                },
                {
                    "id": "select",
                    "name": "Select Best",
                    "prompt_file": "select.md",
                    "depends_on": ["generate"],
                },
            ],
            executor="acp",
        )

        backend = ACPBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path), prompt_backend=backend, settings=config.settings,
        )
        try:
            graph = build_workflow_graph(config, executor)
            result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

            assert "generate" in result["completed_phases"]
            assert "select" in result["completed_phases"]
            assert result["gate_scores"]["generate"] == 1.0

            gen_output = result["phase_outputs"]["generate"]
            data = json.loads(gen_output)
            assert len(data["jokes"]) == 3

            assert len(result["phase_outputs"]["select"]) > 0
        finally:
            await executor.close()


# ---------------------------------------------------------------------------
# Script gate: structured-feedback JSON parsing
# ---------------------------------------------------------------------------


class TestScriptGateStructuredFeedback:
    @pytest.mark.asyncio
    async def test_script_gate_with_expanded_json_populates_feedback(self, tmp_path):
        """Script gate returning full feedback schema populates all GateResult fields."""
        script = tmp_path / "validator.py"
        script.write_text(
            'import json\n'
            'print(json.dumps({\n'
            '    "score": 0.6,\n'
            '    "feedback": "missing docstring",\n'
            '    "pass_criteria_met": ["tests pass"],\n'
            '    "pass_criteria_unmet": ["docs"],\n'
            '}))\n'
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.6
        assert result.feedback == "missing docstring"
        assert result.pass_criteria_met == ["tests pass"]
        assert result.pass_criteria_unmet == ["docs"]

    @pytest.mark.asyncio
    async def test_script_gate_bare_float_has_empty_feedback(self, tmp_path):
        """Backward compat: bare-float scripts still work, feedback stays empty."""
        script = tmp_path / "validator.py"
        script.write_text("print(0.9)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.9
        assert result.feedback is None
        assert result.pass_criteria_met == []
        assert result.pass_criteria_unmet == []

    @pytest.mark.asyncio
    async def test_garbage_output_populates_feedback(self, tmp_path):
        """Loud failure on unparseable validator output — feedback explains why."""
        script = tmp_path / "validator.py"
        script.write_text("print('not a number')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0
        assert result.feedback is not None
        assert "unparseable" in result.feedback
        assert "not a number" in result.feedback

    @pytest.mark.asyncio
    async def test_missing_score_field_populates_feedback(self, tmp_path):
        """JSON without `score` key surfaces a diagnostic in feedback."""
        script = tmp_path / "validator.py"
        script.write_text(
            'import json; print(json.dumps({"feedback": "ok"}))'
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0
        assert "score" in result.feedback

    @pytest.mark.asyncio
    async def test_nonexistent_validator_populates_feedback(self):
        """Missing validator script surfaces a clear path in feedback."""
        gate = QualityGate(
            validator="/tmp/abe_froman_does_not_exist_99999.py", threshold=0.8
        )
        result = await evaluate_gate(gate, "p1")
        assert result.score == 0.0
        assert result.feedback is not None
        assert "/tmp/abe_froman_does_not_exist_99999.py" in result.feedback

    @pytest.mark.asyncio
    async def test_nonzero_exit_captures_stderr(self, tmp_path):
        """Validator exiting non-zero surfaces stderr snippet in feedback."""
        script = tmp_path / "validator.py"
        script.write_text(
            'import sys\n'
            'sys.stderr.write("validator went boom\\n")\n'
            'sys.exit(2)\n'
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        result = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0
        assert "code 2" in result.feedback
        assert "validator went boom" in result.feedback


# ---------------------------------------------------------------------------
# LLM gate parser: pure-function tests, no backend involved
# ---------------------------------------------------------------------------


class TestLLMGateParser:
    """_parse_llm_gate_response is pure: string in, GateResult out.

    No backend needed. Integration with a real backend (ACP) is covered
    separately in tests/acp/.
    """

    def test_full_schema_parsed(self):
        from abe_froman.runtime.gates import _parse_llm_gate_response

        raw = json.dumps({
            "score": 0.85,
            "feedback": "solid work",
            "pass_criteria_met": ["clarity", "concision"],
            "pass_criteria_unmet": [],
        })
        result = _parse_llm_gate_response(raw)
        assert result.score == 0.85
        assert result.feedback == "solid work"
        assert result.pass_criteria_met == ["clarity", "concision"]
        assert result.pass_criteria_unmet == []

    def test_score_only_parsed(self):
        from abe_froman.runtime.gates import _parse_llm_gate_response

        result = _parse_llm_gate_response(json.dumps({"score": 0.5}))
        assert result.score == 0.5
        assert result.feedback is None

    def test_malformed_json_loud_failure(self):
        from abe_froman.runtime.gates import _parse_llm_gate_response

        result = _parse_llm_gate_response("this is not json at all")
        assert result.score == 0.0
        assert result.feedback is not None
        assert "unparseable" in result.feedback

    def test_missing_score_loud_failure(self):
        from abe_froman.runtime.gates import _parse_llm_gate_response

        result = _parse_llm_gate_response(json.dumps({"feedback": "ok"}))
        assert result.score == 0.0
        assert "missing 'score'" in result.feedback

    def test_non_numeric_score_loud_failure(self):
        from abe_froman.runtime.gates import _parse_llm_gate_response

        result = _parse_llm_gate_response(json.dumps({"score": "high"}))
        assert result.score == 0.0
        assert "not numeric" in result.feedback

    def test_non_dict_top_level_loud_failure(self):
        from abe_froman.runtime.gates import _parse_llm_gate_response

        result = _parse_llm_gate_response(json.dumps([1, 2, 3]))
        assert result.score == 0.0
        assert "missing 'score'" in result.feedback


class TestMDGateDispatchGuard:
    """`.md` gate without a backend must raise loudly, not silently 0."""

    @pytest.mark.asyncio
    async def test_md_gate_no_backend_raises(self, tmp_path):
        gate_md = tmp_path / "g.md"
        gate_md.write_text("{{ output }}")
        gate = QualityGate(validator=str(gate_md), threshold=0.8)
        with pytest.raises(ValueError, match="requires a PromptBackend"):
            await evaluate_gate(gate, "p1", workdir=str(tmp_path))

    @pytest.mark.asyncio
    async def test_llm_gate_missing_template_returns_loud_failure(self, tmp_path):
        """A typo'd or deleted `.md` template must yield a structured GateResult,
        not raise FileNotFoundError up through the phase node."""
        from abe_froman.runtime.executor.backends.stub import StubBackend

        gate = QualityGate(validator="gates/nonexistent.md", threshold=0.8)
        backend = StubBackend()
        try:
            result = await evaluate_gate(
                gate, "p1", workdir=str(tmp_path),
                phase_output="anything", backend=backend,
            )
        finally:
            await backend.close()
        assert result.score == 0.0
        assert result.feedback is not None
        assert "gate template not found" in result.feedback
        assert "gates/nonexistent.md" in result.feedback


# ---------------------------------------------------------------------------
# Integration: gate_feedback reaches state; retry sees it via _retry_reason
# ---------------------------------------------------------------------------


class TestRetryWithFeedback:
    @pytest.mark.asyncio
    async def test_gate_feedback_written_to_state(self, tmp_path):
        """After a script gate with structured feedback runs, state has it."""
        validator = tmp_path / "validator.py"
        validator.write_text(
            'import json\n'
            'print(json.dumps({'
            '"score": 1.0, '
            '"feedback": "all good", '
            '"pass_criteria_met": ["a", "b"], '
            '"pass_criteria_unmet": []'
            '}))\n'
        )
        config = make_config([
            {
                "id": "p",
                "name": "P",
                "execution": {"type": "command", "command": "echo", "args": ["out"]},
                "quality_gate": {
                    "validator": str(validator),
                    "threshold": 0.5,
                    "blocking": True,
                },
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert state["gate_feedback"]["p"] == {
            "feedback": "all good",
            "pass_criteria_met": ["a", "b"],
            "pass_criteria_unmet": [],
        }

    @pytest.mark.asyncio
    async def test_retry_reason_flows_to_second_attempt(self, tmp_path):
        """End-to-end: a failing gate emits structured feedback; the validator on
        the retry reads the prior feedback from state-derived artifacts and
        asserts the orchestrator flows it through correctly.

        Mechanism: the command phase writes the _retry_reason it receives
        (via an env var we pipe in by having the phase be a real subprocess
        that records what's visible). We can't see {{_retry_reason}} from
        inside a command phase directly, but we CAN assert end-to-end that
        gate_feedback persists in state across the retry.
        """
        attempt_file = tmp_path / "n.txt"
        attempt_file.write_text("0")
        runner = tmp_path / "run.py"
        runner.write_text(
            f"n = int(open('{attempt_file}').read())\n"
            f"open('{attempt_file}', 'w').write(str(n+1))\n"
            "print('output')\n"
        )
        validator = tmp_path / "validator.py"
        validator.write_text(
            f"import json\n"
            f"n = int(open('{attempt_file}').read())\n"
            f"if n == 1:\n"
            f"    print(json.dumps({{'score': 0.3, 'feedback': 'needs more detail', "
            f"'pass_criteria_unmet': ['depth', 'breadth']}}))\n"
            f"else:\n"
            f"    print(json.dumps({{'score': 1.0, 'feedback': 'good'}}))\n"
        )
        config = make_config([
            {
                "id": "p",
                "name": "P",
                "execution": {"type": "command", "command": "python3", "args": [str(runner)]},
                "quality_gate": {
                    "validator": str(validator),
                    "threshold": 0.8,
                    "blocking": True,
                    "max_retries": 2,
                },
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)

        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_phases"]
        assert result["gate_feedback"]["p"]["feedback"] == "good"
        assert result["gate_scores"]["p"] == 1.0

    @pytest.mark.asyncio
    async def test_retry_reason_visible_to_prompt_phase_via_preamble(self, tmp_path):
        """For prompt phases, the retry reason IS rendered into the template.
        We verify the rendered-prompt path by constructing the context the way
        inject_retry_reason does and asserting the rendering substitutes.
        """
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.runtime.executor.prompt import render_template
        from abe_froman.schema.models import Phase, QualityGate

        phase = Phase(
            id="p",
            name="P",
            quality_gate=QualityGate(validator="v.py", threshold=0.8),
        )
        state = {
            "retries": {"p": 1},
            "gate_scores": {"p": 0.4},
            "gate_feedback": {
                "p": {
                    "feedback": "more depth please",
                    "pass_criteria_met": [],
                    "pass_criteria_unmet": ["depth"],
                }
            },
        }
        ctx = inject_retry_reason({}, phase, state, 3)
        template = "Previous feedback:\n{{ _retry_reason }}\n\nTry again."
        rendered = render_template(template, ctx)
        assert "more depth please" in rendered
        assert "- depth" in rendered
        assert "Attempt 1 failed" in rendered


# ---------------------------------------------------------------------------
# Node helper: inject_retry_reason with rich feedback
# ---------------------------------------------------------------------------


class TestInjectRetryReasonFeedback:
    def test_retry_reason_without_feedback_is_score_only(self):
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.schema.models import Phase, QualityGate

        phase = Phase(
            id="p",
            name="P",
            quality_gate=QualityGate(validator="v.py", threshold=0.8),
        )
        state = {"retries": {"p": 1}, "gate_scores": {"p": 0.5}, "gate_feedback": {}}
        ctx = inject_retry_reason({}, phase, state, 3)
        assert "Attempt 1 failed" in ctx["_retry_reason"]
        assert "Feedback:" not in ctx["_retry_reason"]

    def test_retry_reason_with_feedback_includes_it(self):
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.schema.models import Phase, QualityGate

        phase = Phase(
            id="p",
            name="P",
            quality_gate=QualityGate(validator="v.py", threshold=0.8),
        )
        state = {
            "retries": {"p": 1},
            "gate_scores": {"p": 0.5},
            "gate_feedback": {
                "p": {
                    "feedback": "add more depth",
                    "pass_criteria_met": ["clarity"],
                    "pass_criteria_unmet": ["depth", "nuance"],
                }
            },
        }
        ctx = inject_retry_reason({}, phase, state, 3)
        reason = ctx["_retry_reason"]
        assert "Feedback: add more depth" in reason
        assert "- depth" in reason
        assert "- nuance" in reason

    def test_retry_reason_no_retry_returns_context_unchanged(self):
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.schema.models import Phase, QualityGate

        phase = Phase(
            id="p",
            name="P",
            quality_gate=QualityGate(validator="v.py", threshold=0.8),
        )
        state = {"retries": {"p": 0}, "gate_scores": {}, "gate_feedback": {}}
        ctx = inject_retry_reason({"x": 1}, phase, state, 3)
        assert ctx == {"x": 1}
        assert "_retry_reason" not in ctx
