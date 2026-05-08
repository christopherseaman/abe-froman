import json
import shutil

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.gates import EvaluationResult, run_evaluation
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.schema.models import Evaluation

from helpers import make_config

_ECHO = shutil.which("echo") or "/bin/echo"
_CAT = shutil.which("cat") or "/bin/cat"
_PYTHON = shutil.which("python3") or "/usr/bin/python3"


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
# Unit tests: run_evaluation receives node_output via stdin
# ---------------------------------------------------------------------------


class TestGateStdinPassing:
    @pytest.mark.asyncio
    async def test_valid_output_passes_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = Evaluation(validator=str(script), threshold=1.0)
        node_output = json.dumps({"items": ["a", "b", "c"]})
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path), node_output=node_output)
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_invalid_output_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = Evaluation(validator=str(script), threshold=1.0)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path), node_output="not json")
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_wrong_count_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = Evaluation(validator=str(script), threshold=1.0)
        node_output = json.dumps({"items": ["a"]})
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path), node_output=node_output)
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_empty_stdin_fails_validator(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text(JSON_VALIDATOR)
        gate = Evaluation(validator=str(script), threshold=1.0)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path), node_output="")
        assert result.score == 0.0


# ---------------------------------------------------------------------------
# Unit tests: run_evaluation basics (no stdin inspection)
# ---------------------------------------------------------------------------


class TestGateEvaluation:
    @pytest.mark.asyncio
    async def test_md_validator_requires_backend(self):
        """`.md` gates must be dispatched with a backend; without one, raise."""
        gate = Evaluation(validator="gates/v.md", threshold=0.8)
        with pytest.raises(ValueError, match="requires a PromptBackend"):
            await run_evaluation(gate, "p1")

    @pytest.mark.asyncio
    async def test_unsupported_extension_raises(self):
        gate = Evaluation(validator="gates/v.txt", threshold=0.8)
        with pytest.raises(ValueError, match="Unsupported"):
            await run_evaluation(gate, "p1")

    @pytest.mark.asyncio
    async def test_py_validator_returns_float_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(0.95)")
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.95

    @pytest.mark.asyncio
    async def test_py_validator_returns_json_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text('import json; print(json.dumps({"score": 0.75}))')
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.75

    @pytest.mark.asyncio
    async def test_py_validator_exception_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("raise Exception('fail')")
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_garbage_output_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print('not a number')")
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0

    @pytest.mark.asyncio
    async def test_nonexistent_py_validator_returns_zero(self):
        gate = Evaluation(validator="/tmp/does_not_exist_12345.py", threshold=0.8)
        result = await run_evaluation(gate, "p1")
        assert result.score == 0.0

# ---------------------------------------------------------------------------
# Node-level: gate pass/fail with stdin-inspecting validators in full graph
# ---------------------------------------------------------------------------


class TestGateNodePassFail:
    """Test gates in the context of actual graph execution with real validators
    that inspect node output via stdin."""

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
                    "execute": {"url": _CAT, "params": {"args": [str(payload)]}},
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                    },
                },
                {
                    "id": "b",
                    "name": "B",
                    "execute": {"url": _ECHO, "params": {"args": ["b done"]}},
                    "depends_on": ["a"],
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
        assert result["evaluations"]["a"][-1]["result"]["score"] == 1.0

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
                    "execute": {"url": _ECHO, "params": {"args": ["not json"]}},
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 0,
                    },
                },
                {
                    "id": "b",
                    "name": "B",
                    "execute": {"url": _ECHO, "params": {"args": ["b done"]}},
                    "depends_on": ["a"],
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["failed_nodes"]
        assert "b" not in result["completed_nodes"]
        assert result["evaluations"]["a"][-1]["result"]["score"] == 0.0

    @pytest.mark.asyncio
    async def test_non_blocking_gate_failure_continues(self, tmp_path):
        """Non-blocking gate failure: node completes with warning, dependent runs."""
        validator = tmp_path / "validator.py"
        validator.write_text(JSON_VALIDATOR)

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execute": {"url": _ECHO, "params": {"args": ["bad"]}},
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": False,
                        "max_retries": 0,
                    },
                },
                {
                    "id": "b",
                    "name": "B",
                    "execute": {"url": _ECHO, "params": {"args": ["b done"]}},
                    "depends_on": ["a"],
                },
            ]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
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
        script = tmp_path / "validator.js"
        script.write_text('process.stdout.write("0.85")')
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.85

    @pytest.mark.asyncio
    async def test_js_validator_not_found(self):
        gate = Evaluation(validator="/tmp/does_not_exist_99999.js", threshold=0.8)
        result = await run_evaluation(gate, "p1")
        assert result.score == 0.0


class TestGateEnvironment:
    @pytest.mark.asyncio
    async def test_phase_id_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('NODE_ID') == 'my-node' else '0.0')\n"
        )
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "my-node", workdir=str(tmp_path))
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_workflow_name_env_var(self, tmp_path):
        script = tmp_path / "env_check.py"
        script.write_text(
            "import os\n"
            "print('1.0' if os.environ.get('WORKFLOW_NAME') == 'test-wf' else '0.0')\n"
        )
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(
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
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(
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
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
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
                    "execute": {
                        "url": _PYTHON,
                        "params": {"args": [str(counter_script)]},
                    },
                    "evaluation": {
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

        assert "a" in result["completed_nodes"]
        assert result["evaluations"]["a"][-1]["result"]["score"] == 1.0

    @pytest.mark.asyncio
    async def test_explicit_nonzero_exit(self, tmp_path):
        script = tmp_path / "exit1.py"
        script.write_text("import sys; sys.exit(1)")
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0


# ---------------------------------------------------------------------------
# Integration: multi-step joke workflow with ACP + deterministic gate
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    """Real-sleep integration test for stepped retry backoff.

    The pure `_get_retry_delay` function is unit-tested in
    tests/unit/compile/test_node_helpers.py. This test verifies the delay
    values are actually awaited between retry attempts.
    """

    @pytest.mark.asyncio
    async def test_retry_backoff_delays_are_awaited(self, tmp_path):
        """With backoff=[0.05, 0.1] and 3 total attempts, elapsed time must
        include both delays (≥0.15s) and not much more."""
        import time

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
            "print('1.0' if count >= 3 else '0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execute": {
                        "url": _PYTHON,
                        "params": {"args": [str(counter_script)]},
                    },
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 3,
                    },
                },
            ],
            retry_backoff=[0.05, 0.1],
        )

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        t0 = time.monotonic()
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        elapsed = time.monotonic() - t0

        assert "a" in result["completed_nodes"]
        assert elapsed >= 0.05 + 0.1, f"retries ran too fast ({elapsed:.3f}s); backoff not applied"
        # Upper bound: backoff (0.15s) + generous wall-clock slack for 3 subprocess
        # executions + 3 validator runs. If we blow past this, something other
        # than the backoff is slowing us down.
        assert elapsed < 10.0, f"suspiciously slow ({elapsed:.3f}s)"


# ---------------------------------------------------------------------------
# Script gate: structured-feedback JSON parsing
# ---------------------------------------------------------------------------


class TestScriptGateStructuredFeedback:
    @pytest.mark.asyncio
    async def test_script_gate_with_expanded_json_populates_feedback(self, tmp_path):
        """Script gate returning full feedback schema populates all EvaluationResult fields."""
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
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.6
        assert result.feedback == "missing docstring"
        assert result.pass_criteria_met == ["tests pass"]
        assert result.pass_criteria_unmet == ["docs"]

    @pytest.mark.asyncio
    async def test_script_gate_bare_float_has_empty_feedback(self, tmp_path):
        """Backward compat: bare-float scripts still work, feedback stays empty."""
        script = tmp_path / "validator.py"
        script.write_text("print(0.9)")
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.9
        assert result.feedback is None
        assert result.pass_criteria_met == []
        assert result.pass_criteria_unmet == []

    @pytest.mark.asyncio
    async def test_garbage_output_populates_feedback(self, tmp_path):
        """Loud failure on unparseable validator output — feedback explains why."""
        script = tmp_path / "validator.py"
        script.write_text("print('not a number')")
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
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
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0
        assert "score" in result.feedback

    @pytest.mark.asyncio
    async def test_nonexistent_validator_populates_feedback(self):
        """Missing validator script surfaces a clear path in feedback."""
        gate = Evaluation(
            validator="/tmp/abe_froman_does_not_exist_99999.py", threshold=0.8
        )
        result = await run_evaluation(gate, "p1")
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
        gate = Evaluation(validator=str(script), threshold=0.8)
        result = await run_evaluation(gate, "p1", workdir=str(tmp_path))
        assert result.score == 0.0
        assert "code 2" in result.feedback
        assert "validator went boom" in result.feedback


# ---------------------------------------------------------------------------
# LLM gate parser: pure-function tests, no backend involved
# ---------------------------------------------------------------------------


class TestGateOutputParser:
    """_parse_evaluation_output is pure: string in, EvaluationResult out.

    No backend needed. Integration with a real backend (ACP) is covered
    separately in tests/acp/.
    """

    def test_full_schema_parsed(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        raw = json.dumps({
            "score": 0.85,
            "feedback": "solid work",
            "pass_criteria_met": ["clarity", "concision"],
            "pass_criteria_unmet": [],
        })
        result = _parse_evaluation_output(raw)
        assert result.score == 0.85
        assert result.feedback == "solid work"
        assert result.pass_criteria_met == ["clarity", "concision"]
        assert result.pass_criteria_unmet == []

    def test_score_only_parsed(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output(json.dumps({"score": 0.5}))
        assert result.score == 0.5
        assert result.feedback is None

    def test_bare_float_accepted_for_scripts(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output("0.85", allow_bare_float=True)
        assert result.score == 0.85
        assert result.feedback is None

    def test_bare_float_rejected_for_llm(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output("0.85")
        assert result.score == 0.0
        assert "score" in result.feedback

    def test_malformed_json_loud_failure(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output("this is not json at all")
        assert result.score == 0.0
        assert result.feedback is not None
        assert "unparseable" in result.feedback

    def test_missing_score_loud_failure(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output(json.dumps({"feedback": "ok"}))
        assert result.score == 0.0
        assert "missing" in result.feedback and "score" in result.feedback

    def test_non_numeric_score_loud_failure(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output(json.dumps({"score": "high"}))
        assert result.score == 0.0
        assert "score" in result.feedback

    def test_non_dict_top_level_loud_failure(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        result = _parse_evaluation_output(json.dumps([1, 2, 3]))
        assert result.score == 0.0
        assert "score" in result.feedback


class TestMultiDimensionParser:
    """_parse_evaluation_output extracts numeric fields as dimension scores."""

    def test_dimension_scores_extracted(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        raw = json.dumps({"correctness": 0.8, "style": 0.6, "score": 0.7})
        result = _parse_evaluation_output(raw)
        assert result.score == 0.7
        assert result.scores == {"correctness": 0.8, "style": 0.6}

    def test_no_score_with_dimensions_derives_min(self):
        """When the JSON omits top-level `score` but supplies per-dim
        numbers, the headline `score` is derived as `min(dim_scores)`.
        Routing for multi-dim gates uses per-dim threshold clauses
        directly (see `compile/evaluation.py::evaluation_to_routes`),
        so this derivation is purely cosmetic — but it makes JSONL log
        events meaningful instead of misleadingly showing 0.0 for
        passing gates."""
        from abe_froman.runtime.gates import _parse_evaluation_output

        raw = json.dumps({"correctness": 0.8, "style": 0.6})
        result = _parse_evaluation_output(raw, require_score=False)
        # min of [0.8, 0.6] = 0.6 (weakest-link, mirrors
        # `dimensions[].min` semantics).
        assert result.score == 0.6
        assert result.scores == {"correctness": 0.8, "style": 0.6}
        assert result.feedback is None

    def test_single_dimension_no_score_derives_min(self):
        """One dim, no top-level score: derived score == that dim's value."""
        from abe_froman.runtime.gates import _parse_evaluation_output

        raw = json.dumps({"correctness": 0.8})
        result = _parse_evaluation_output(raw)
        assert result.score == 0.8
        assert result.feedback is None
        assert result.scores == {"correctness": 0.8}

    def test_non_numeric_fields_ignored(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        raw = json.dumps({"score": 0.5, "label": "good", "count": 3})
        result = _parse_evaluation_output(raw)
        assert result.scores == {"count": 3.0}
        assert "label" not in result.scores

    def test_feedback_field_not_treated_as_dimension(self):
        from abe_froman.runtime.gates import _parse_evaluation_output

        raw = json.dumps({"score": 0.5, "feedback": "ok", "quality": 0.9})
        result = _parse_evaluation_output(raw)
        assert result.scores == {"quality": 0.9}
        assert result.feedback == "ok"


class TestMDGateDispatchGuard:
    """`.md` gate without a backend must raise loudly, not silently 0."""

    @pytest.mark.asyncio
    async def test_md_gate_no_backend_raises(self, tmp_path):
        gate_md = tmp_path / "g.md"
        gate_md.write_text("{{ output }}")
        gate = Evaluation(validator=str(gate_md), threshold=0.8)
        with pytest.raises(ValueError, match="requires a PromptBackend"):
            await run_evaluation(gate, "p1", workdir=str(tmp_path))



# ---------------------------------------------------------------------------
# Integration: evaluation records reach state; retry sees them via _retry_reason
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
                "execute": {"url": _ECHO, "params": {"args": ["out"]}},
                "evaluation": {
                    "validator": str(validator),
                    "threshold": 0.5,
                    "blocking": True,
                },
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        last_result = state["evaluations"]["p"][-1]["result"]
        assert last_result["feedback"] == "all good"
        assert last_result["pass_criteria_met"] == ["a", "b"]
        assert last_result["pass_criteria_unmet"] == []
        assert last_result["scores"] == {}

    @pytest.mark.asyncio
    async def test_retry_reason_flows_to_second_attempt(self, tmp_path):
        """End-to-end: a failing gate emits structured feedback; the validator on
        the retry reads the prior feedback from state-derived artifacts and
        asserts the orchestrator flows it through correctly.

        Mechanism: the command node writes the _retry_reason it receives
        (via an env var we pipe in by having the node be a real subprocess
        that records what's visible). We can't see {{_retry_reason}} from
        inside a command node directly, but we CAN assert end-to-end that
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
                "execute": {"url": _PYTHON, "params": {"args": [str(runner)]}},
                "evaluation": {
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

        assert "p" in result["completed_nodes"]
        last_result = result["evaluations"]["p"][-1]["result"]
        assert last_result["feedback"] == "good"
        assert last_result["score"] == 1.0

    @pytest.mark.asyncio
    async def test_retry_reason_visible_to_prompt_phase_via_preamble(self, tmp_path):
        """For prompt nodes, the retry reason IS rendered into the template.
        We verify the rendered-prompt path by constructing the context the way
        inject_retry_reason does and asserting the rendering substitutes.
        """
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.runtime.executor.prompt import render_template
        from abe_froman.schema.models import Node, Evaluation

        node = Node(
            id="p",
            name="P",
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        state = {
            "retries": {"p": 1},
            "evaluations": {
                "p": [{
                    "invocation": 0,
                    "result": {
                        "score": 0.4,
                        "scores": {},
                        "feedback": "more depth please",
                        "pass_criteria_met": [],
                        "pass_criteria_unmet": ["depth"],
                    },
                    "timestamp": "t",
                }],
            },
        }
        ctx = inject_retry_reason({}, node, state, 3)
        template = "Previous feedback:\n{{ _retry_reason }}\n\nTry again."
        rendered = render_template(template, ctx)
        assert "more depth please" in rendered
        assert "- depth" in rendered
        assert "Attempt 1 of 3" in rendered
        # Neutral preamble — no "failed" framing.
        assert "failed" not in rendered.lower()


# ---------------------------------------------------------------------------
# Node helper: inject_retry_reason with rich feedback
# ---------------------------------------------------------------------------


class TestInjectRetryReasonFeedback:
    def test_retry_reason_without_feedback_is_score_only(self):
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.schema.models import Node, Evaluation

        node = Node(
            id="p",
            name="P",
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        state = {
            "retries": {"p": 1},
            "evaluations": {
                "p": [{
                    "invocation": 0,
                    "result": {
                        "score": 0.5,
                        "scores": {},
                        "feedback": None,
                        "pass_criteria_met": [],
                        "pass_criteria_unmet": [],
                    },
                    "timestamp": "t",
                }],
            },
        }
        ctx = inject_retry_reason({}, node, state, 3)
        assert "Attempt 1 of 3" in ctx["_retry_reason"]
        assert "Feedback:" not in ctx["_retry_reason"]
        assert "failed" not in ctx["_retry_reason"].lower()

    def test_retry_reason_with_feedback_includes_it(self):
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.schema.models import Node, Evaluation

        node = Node(
            id="p",
            name="P",
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        state = {
            "retries": {"p": 1},
            "evaluations": {
                "p": [{
                    "invocation": 0,
                    "result": {
                        "score": 0.5,
                        "scores": {},
                        "feedback": "add more depth",
                        "pass_criteria_met": ["clarity"],
                        "pass_criteria_unmet": ["depth", "nuance"],
                    },
                    "timestamp": "t",
                }],
            },
        }
        ctx = inject_retry_reason({}, node, state, 3)
        reason = ctx["_retry_reason"]
        assert "Feedback: add more depth" in reason
        assert "- depth" in reason
        assert "- nuance" in reason

    def test_retry_reason_no_retry_returns_context_unchanged(self):
        from abe_froman.compile.nodes import inject_retry_reason
        from abe_froman.schema.models import Node, Evaluation

        node = Node(
            id="p",
            name="P",
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        state = {"retries": {"p": 0}, "evaluations": {}}
        ctx = inject_retry_reason({"x": 1}, node, state, 3)
        assert ctx == {"x": 1}
        assert "_retry_reason" not in ctx


# ---------------------------------------------------------------------------
# Parser: <dim>_reason fields preserve per-dimension rationale
# ---------------------------------------------------------------------------


class TestEvaluationReasons:
    """Stage 5c: LLM gates can return per-dimension reasons via the
    `<dim>_reason` JSON field convention; parser captures them into
    `EvaluationResult.reasons`."""

    def test_dim_reason_field_captured(self):
        from abe_froman.runtime.gates import _parse_evaluation_output
        raw = json.dumps({
            "score": 0.55,
            "coverage": 0.7,
            "coverage_reason": "missing definitions for X",
            "quality": 0.4,
            "quality_reason": "shallow analysis",
            "feedback": "see per-dim reasons",
        })
        result = _parse_evaluation_output(raw)
        assert result.score == 0.55
        assert result.scores == {"coverage": 0.7, "quality": 0.4}
        assert result.reasons == {
            "coverage": "missing definitions for X",
            "quality": "shallow analysis",
        }
        assert result.feedback == "see per-dim reasons"

    def test_no_reason_fields_means_empty_dict(self):
        from abe_froman.runtime.gates import _parse_evaluation_output
        raw = json.dumps({"score": 0.8, "coverage": 0.9})
        result = _parse_evaluation_output(raw)
        assert result.reasons == {}

    def test_non_string_reason_is_ignored(self):
        # `<dim>_reason: <number>` shouldn't crash the parser; it's
        # neither a numeric dim score (suffix is "_reason") nor a string
        # reason. Drop silently rather than capture noise.
        from abe_froman.runtime.gates import _parse_evaluation_output
        raw = json.dumps({"score": 0.5, "x": 0.7, "x_reason": 42})
        result = _parse_evaluation_output(raw)
        assert result.scores == {"x": 0.7}
        assert "x" not in result.reasons


# ---------------------------------------------------------------------------
# Preamble builder: structural / neutral output
# ---------------------------------------------------------------------------


class TestEvalPreamble:
    """Stage 5c: build_eval_preamble produces neutral structural text;
    no "failed" framing; per-dimension reasons surfaced inline."""

    def test_no_dimensions_simple_score(self):
        from abe_froman.runtime.gates import build_eval_preamble
        from abe_froman.schema.models import Evaluation
        evaluation = Evaluation(validator="v.md", threshold=0.8)
        result = {"score": 0.42, "feedback": None}
        text = build_eval_preamble(result, evaluation)
        assert "score=0.42" in text
        assert "threshold=0.8" in text
        assert "failed" not in text.lower()

    def test_with_dimensions_and_reasons(self):
        from abe_froman.runtime.gates import build_eval_preamble
        from abe_froman.schema.models import Evaluation, DimensionCheck
        evaluation = Evaluation(
            validator="v.md",
            dimensions=[
                DimensionCheck(field="coverage", min=0.6),
                DimensionCheck(field="quality", min=0.6),
            ],
        )
        result = {
            "scores": {"coverage": 0.7, "quality": 0.4},
            "reasons": {"coverage": "missing X", "quality": "shallow"},
            "feedback": "see per-dim",
        }
        text = build_eval_preamble(result, evaluation)
        assert "coverage=0.70 (min=0.6): missing X" in text
        assert "quality=0.40 (min=0.6): shallow" in text
        assert "Feedback: see per-dim" in text
        assert "failed" not in text.lower()

    def test_attempt_footer_only_when_provided(self):
        from abe_froman.runtime.gates import build_eval_preamble
        result = {"score": 0.5, "feedback": None}
        without = build_eval_preamble(result, None)
        with_attempt = build_eval_preamble(
            result, None, attempt=2, total_attempts=3,
        )
        assert "Attempt" not in without
        assert "Attempt 2 of 3" in with_attempt

    def test_extra_dimensions_surfaced(self):
        # Gate returned a dimension the evaluation didn't declare —
        # surface it rather than drop, so unexpected coverage is visible.
        from abe_froman.runtime.gates import build_eval_preamble
        from abe_froman.schema.models import Evaluation, DimensionCheck
        evaluation = Evaluation(
            validator="v.md",
            dimensions=[DimensionCheck(field="coverage", min=0.6)],
        )
        result = {
            "scores": {"coverage": 0.7, "extras": 0.3},
            "reasons": {"extras": "unexpected"},
        }
        text = build_eval_preamble(result, evaluation)
        assert "coverage=0.70" in text
        assert "extras=0.30: unexpected" in text


# ---------------------------------------------------------------------------
# Dep-output projection (WISHLIST 216 — gate validators see dep outputs)
# ---------------------------------------------------------------------------


class TestGateDepOutputs:
    """Gates today see only the node's own output. The dep-outputs
    projection lets a validator inspect upstream nodes' outputs too —
    the bug case that gate-only phases rely on the `$WORKDIR` filesystem
    workaround for. After this fix:

    - Script gates: `DEPS_JSON` env var carries the JSON-serialized
      dict of dep node-id → output. Validators read it via
      `json.loads(os.environ["DEPS_JSON"])`. Stdin shape unchanged
      (still the node's own output).
    - LLM gates: each dep is bound by id directly in the Jinja
      context (matches `build_context`'s shape).
    """

    @pytest.mark.asyncio
    async def test_script_gate_reads_DEPS_JSON_env_var(self, tmp_path):
        """Validator script reads DEPS_JSON; verifies the projected
        dep value is present."""
        script = tmp_path / "v.py"
        script.write_text(
            "import json, os, sys\n"
            "deps = json.loads(os.environ['DEPS_JSON'])\n"
            "print('1.0' if deps.get('research') == 'r-output' else '0.0')\n"
        )
        gate = Evaluation(validator=str(script), threshold=1.0)
        result = await run_evaluation(
            gate, "writer", workdir=str(tmp_path),
            node_output="any",
            dep_outputs={"research": "r-output"},
        )
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_script_gate_DEPS_JSON_empty_when_no_deps(self, tmp_path):
        """Without dep_outputs, DEPS_JSON is the empty dict — never
        absent. Validators can rely on it being a parseable JSON
        object without an `if "DEPS_JSON" in os.environ` guard."""
        script = tmp_path / "v.py"
        script.write_text(
            "import json, os\n"
            "deps = json.loads(os.environ['DEPS_JSON'])\n"
            "print('1.0' if deps == {} else '0.0')\n"
        )
        gate = Evaluation(validator=str(script), threshold=1.0)
        result = await run_evaluation(
            gate, "n", workdir=str(tmp_path), node_output="x",
        )
        assert result.score == 1.0

    @pytest.mark.asyncio
    async def test_script_gate_structured_and_worktrees_env_vars(self, tmp_path):
        """DEPS_STRUCTURED_JSON and DEPS_WORKTREES_JSON are the
        structured-output and worktree-path companions to DEPS_JSON.
        Useful when validators want parsed JSON content from a dep
        rather than its raw stdout."""
        script = tmp_path / "v.py"
        script.write_text(
            "import json, os\n"
            "structured = json.loads(os.environ['DEPS_STRUCTURED_JSON'])\n"
            "worktrees = json.loads(os.environ['DEPS_WORKTREES_JSON'])\n"
            "ok = structured.get('analyze', {}).get('topic') == 'AI' and "
            "worktrees.get('analyze') == '/tmp/wt-x'\n"
            "print('1.0' if ok else '0.0')\n"
        )
        gate = Evaluation(validator=str(script), threshold=1.0)
        result = await run_evaluation(
            gate, "writer", workdir=str(tmp_path),
            node_output="x",
            dep_structured_outputs={"analyze": {"topic": "AI"}},
            dep_worktrees={"analyze": "/tmp/wt-x"},
        )
        assert result.score == 1.0

    def test_llm_gate_template_binds_dep_by_id(self):
        """An LLM gate template referencing `{{ research }}` resolves
        to research's stdout — same shape executor templates use via
        `build_context`."""
        from abe_froman.runtime.executor.prompt import render_template
        from abe_froman.runtime.gates import build_llm_gate_context

        context = build_llm_gate_context(
            node_id="writer",
            node_output="my draft",
            attempt_number=1,
            dep_outputs={"research": "cats are great"},
        )
        rendered = render_template("Research said: {{ research }}", context)
        assert "cats are great" in rendered

    def test_llm_gate_template_aggregate_deps_form(self):
        """`{{ _deps }}` is a JSON dump of all dep outputs — useful
        for templates that want to iterate generically."""
        from abe_froman.runtime.executor.prompt import render_template
        from abe_froman.runtime.gates import build_llm_gate_context

        context = build_llm_gate_context(
            node_id="writer",
            node_output="x",
            attempt_number=1,
            dep_outputs={"a": "alpha", "b": "beta"},
        )
        rendered = render_template("All deps: {{ _deps }}", context)
        assert "alpha" in rendered
        assert "beta" in rendered


class TestGateScopingByDeps:
    """`run_evaluation_and_outcome` filters dep_outputs to the gated
    node's declared deps, matching `build_context`'s scoping. A node
    with `depends_on: [a]` does NOT see `b`'s output even though `b`
    is in completed_nodes. Gate-only phases (no execute, no deps)
    see ALL completed outputs (the WISHLIST bug case)."""

    def test_scoped_to_declared_deps(self, tmp_path):
        """Direct test of the scoping helper."""
        from abe_froman.compile.nodes import _scope_dep_outputs_for_gate
        from abe_froman.schema.models import Node

        node = Node(
            id="writer", name="Writer",
            depends_on=["research"],
            execute={"url": "/usr/bin/echo"},
            evaluation={"validator": "/usr/bin/true", "threshold": 1.0},
        )
        state = {
            "node_outputs": {
                "research": "r-out",
                "unrelated": "u-out",
            },
            "node_structured_outputs": {},
            "node_worktrees": {},
        }
        deps, structured, worktrees = _scope_dep_outputs_for_gate(node, state)
        assert deps == {"research": "r-out"}
        assert "unrelated" not in deps

    def test_gate_only_phase_sees_all_completed(self, tmp_path):
        """Node with no `execute:`, no `depends_on:`, only
        `evaluation:` — gets all completed outputs."""
        from abe_froman.compile.nodes import _scope_dep_outputs_for_gate
        from abe_froman.schema.models import Node

        node = Node(
            id="checker", name="Checker",
            evaluation={"validator": "/usr/bin/true", "threshold": 1.0},
        )
        state = {
            "node_outputs": {"a": "a-out", "b": "b-out"},
            "node_structured_outputs": {},
            "node_worktrees": {},
        }
        deps, _, _ = _scope_dep_outputs_for_gate(node, state)
        assert deps == {"a": "a-out", "b": "b-out"}

    def test_no_completed_outputs_returns_none(self, tmp_path):
        """Empty state → None (run_evaluation_script writes
        DEPS_JSON='{}' anyway, so the validator sees an empty dict
        either way)."""
        from abe_froman.compile.nodes import _scope_dep_outputs_for_gate
        from abe_froman.schema.models import Node

        node = Node(
            id="writer", name="Writer",
            depends_on=["research"],
            execute={"url": "/usr/bin/echo"},
            evaluation={"validator": "/usr/bin/true", "threshold": 1.0},
        )
        state = {"node_outputs": {}}
        deps, _, _ = _scope_dep_outputs_for_gate(node, state)
        assert deps is None
