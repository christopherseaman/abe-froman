import asyncio
import json

import pytest

from abe_froman.engine.builder import build_workflow_graph
from abe_froman.engine.gates import evaluate_gate
from abe_froman.engine.state import make_initial_state
from abe_froman.executor.backends.acp import ACPBackend
from abe_froman.executor.dispatch import DispatchExecutor
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
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output=phase_output)
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_invalid_output_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output="not json")
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_wrong_count_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        phase_output = json.dumps({"items": ["a"]})
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output=phase_output)
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_empty_stdin_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = QualityGate(validator=str(script), threshold=1.0)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path), phase_output="")
        assert score == 0.0


# ---------------------------------------------------------------------------
# Unit tests: evaluate_gate basics (no stdin inspection)
# ---------------------------------------------------------------------------


class TestGateEvaluation:
    @pytest.mark.asyncio
    async def test_md_validator_stub_returns_pass(self):
        gate = QualityGate(validator="gates/v.md", threshold=0.8)
        score = await evaluate_gate(gate, "p1")
        assert score == 1.0

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
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.95

    @pytest.mark.asyncio
    async def test_py_validator_returns_json_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text('import json; print(json.dumps({"score": 0.75}))')
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.75

    @pytest.mark.asyncio
    async def test_py_validator_exception_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("raise Exception('fail')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_garbage_output_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print('not a number')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_nonexistent_py_validator_returns_zero(self):
        gate = QualityGate(validator="/tmp/does_not_exist_12345.py", threshold=0.8)
        score = await evaluate_gate(gate, "p1")
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_zero_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(0.0)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_perfect_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(1.0)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 1.0


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
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.85

    @pytest.mark.asyncio
    async def test_js_validator_not_found(self):
        gate = QualityGate(validator="/tmp/does_not_exist_99999.js", threshold=0.8)
        score = await evaluate_gate(gate, "p1")
        assert score == 0.0


class TestGateEnvironment:
    @pytest.mark.asyncio
    async def test_phase_id_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('PHASE_ID') == 'my-phase' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "my-phase", workdir=str(tmp_path))
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_workflow_name_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('WORKFLOW_NAME') == 'test-wf' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(
            gate, "p1", workdir=str(tmp_path), workflow_name="test-wf",
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_attempt_number_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('ATTEMPT_NUMBER') == '1' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(
            gate, "p1", workdir=str(tmp_path), attempt_number=1,
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_workdir_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            f"print('1.0' if os.environ.get('WORKDIR') == '{tmp_path}' else '0.0')\n"
        )
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 1.0

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
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0


# ---------------------------------------------------------------------------
# Integration: multi-step joke workflow with ACP + deterministic gate
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    """Tests for stepped retry backoff delays."""

    def test_get_retry_delay_empty_list(self):
        from abe_froman.engine.builder import _get_retry_delay

        assert _get_retry_delay(1, []) == 0.0
        assert _get_retry_delay(5, []) == 0.0

    def test_get_retry_delay_single_element(self):
        from abe_froman.engine.builder import _get_retry_delay

        assert _get_retry_delay(1, [10.0]) == 10.0
        assert _get_retry_delay(2, [10.0]) == 10.0
        assert _get_retry_delay(5, [10.0]) == 10.0

    def test_get_retry_delay_multiple_elements(self):
        from abe_froman.engine.builder import _get_retry_delay

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
