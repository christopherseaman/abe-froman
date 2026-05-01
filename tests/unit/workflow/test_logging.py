"""Tests for structured JSONL logging."""

import json
import shutil
from io import StringIO

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.logging import JsonlLogger
from abe_froman.runtime.runner import run_workflow
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.dispatch import DispatchExecutor

from helpers import cmd_phase, fail_phase, make_config

_ECHO = shutil.which("echo") or "/bin/echo"


# ---------------------------------------------------------------------------
# Unit tests: JsonlLogger
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_writes_jsonl_line(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.emit({"event": "test"})
        line = buf.getvalue()
        assert line.endswith("\n")
        data = json.loads(line)
        assert data["event"] == "test"

    def test_emit_includes_timestamp(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.emit({"event": "test"})
        data = json.loads(buf.getvalue())
        assert "ts" in data
        assert "T" in data["ts"]  # ISO-8601 format

    def test_emit_to_file(self, tmp_path):
        path = tmp_path / "events.jsonl"
        logger = JsonlLogger(str(path))
        logger.emit({"event": "a"})
        logger.emit({"event": "b"})
        logger.close()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "a"
        assert json.loads(lines[1])["event"] == "b"


class TestLogSnapshot:
    def test_detects_completed(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        prev = {"completed_nodes": [], "failed_nodes": [], "retries": {}, "errors": []}
        curr = {**prev, "completed_nodes": ["research"]}
        logger.log_snapshot(prev, curr)
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "node_completed"
        assert events[0]["node"] == "research"

    def test_detects_failed(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        prev = {"completed_nodes": [], "failed_nodes": [], "retries": {}, "errors": []}
        curr = {
            **prev,
            "failed_nodes": ["build"],
            "errors": [{"node": "build", "error": "exit code 1"}],
        }
        logger.log_snapshot(prev, curr)
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "node_failed"
        assert events[0]["node"] == "build"
        assert events[0]["error"] == "exit code 1"

    def test_detects_gate(self):
        """gate_evaluated sources from state.evaluations (real scores)."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        prev = {"completed_nodes": [], "failed_nodes": [], "evaluations": {}, "retries": {}, "errors": []}
        curr = {
            **prev,
            "evaluations": {
                "research": [{"invocation": 0, "result": {"score": 0.95}, "timestamp": "t"}]
            },
        }
        logger.log_snapshot(prev, curr)
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "gate_evaluated"
        assert events[0]["node"] == "research"
        assert events[0]["score"] == 0.95
        assert events[0]["invocation"] == 0

    def test_detects_multidim_gate(self):
        """Per-dimension scores flow through (closes multi-dim log bug)."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        prev = {"completed_nodes": [], "failed_nodes": [], "evaluations": {}, "retries": {}, "errors": []}
        curr = {
            **prev,
            "evaluations": {
                "p": [{
                    "invocation": 0,
                    "result": {"score": 0.0, "scores": {"rigor": 0.8, "humor": 0.5}},
                    "timestamp": "t",
                }]
            },
        }
        logger.log_snapshot(prev, curr)
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert events[0]["event"] == "gate_evaluated"
        assert events[0]["scores"] == {"rigor": 0.8, "humor": 0.5}

    def test_detects_retry(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        prev = {"completed_nodes": [], "failed_nodes": [], "retries": {}, "errors": []}
        curr = {**prev, "retries": {"research": 2}}
        logger.log_snapshot(prev, curr)
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "node_retried"
        assert events[0]["node"] == "research"
        assert events[0]["attempt"] == 2

    def test_no_events_on_identical_snapshots(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        state = {"completed_nodes": ["a"], "failed_nodes": [], "retries": {}, "errors": []}
        logger.log_snapshot(state, state)
        # Parse rather than string-compare so stray whitespace can't silently pass.
        events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        assert events == []


# ---------------------------------------------------------------------------
# Integration tests: logging through run_workflow
# ---------------------------------------------------------------------------


class TestRunWorkflowLogging:
    @pytest.mark.asyncio
    async def test_log_file_captures_workflow_events(self, tmp_path):
        """Two-node workflow should produce start, 2x completed, end."""
        log_path = str(tmp_path / "events.jsonl")
        config = make_config([
            cmd_phase("a", output="hello"),
            cmd_phase("b", output="world", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            log_file=log_path,
        )

        events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().strip().split("\n")]
        event_types = [e["event"] for e in events]
        assert event_types[0] == "workflow_start"
        assert event_types[-1] == "workflow_end"
        assert event_types.count("node_completed") == 2
        assert events[-1]["completed"] == 2
        assert events[-1]["failed"] == 0

    @pytest.mark.asyncio
    async def test_log_captures_failure(self, tmp_path):
        """Failed node should produce node_failed event."""
        log_path = str(tmp_path / "events.jsonl")
        config = make_config([fail_phase("broken")])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            log_file=log_path,
        )

        events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().strip().split("\n")]
        event_types = [e["event"] for e in events]
        assert "node_failed" in event_types
        assert events[-1]["event"] == "workflow_end"
        assert events[-1]["failed"] == 1

    @pytest.mark.asyncio
    async def test_log_captures_gate_and_retry(self, tmp_path):
        """Gated node that fails gate should produce gate + retry events."""
        validator = tmp_path / "gate.py"
        validator.write_text("print('0.0')\n")

        config = make_config([
            {
                "id": "gated",
                "name": "gated",
                "execute": {"url": _ECHO, "params": {"args": ["-n", "output"]}},
                "evaluation": {
                    "validator": str(validator),
                    "threshold": 0.9,
                    "blocking": True,
                    "max_retries": 1,
                },
            }
        ])
        log_path = str(tmp_path / "events.jsonl")
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            log_file=log_path,
        )

        events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().strip().split("\n")]
        event_types = [e["event"] for e in events]
        assert "gate_evaluated" in event_types
        assert "node_retried" in event_types

    @pytest.mark.asyncio
    async def test_no_log_file_no_side_effects(self, tmp_path):
        """log_file=None should not create any file."""
        config = make_config([cmd_phase("a", output="ok")])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            log_file=None,
        )
        # No .jsonl file should exist
        assert list(tmp_path.glob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------


class TestCliLogFlag:
    def test_cli_log_flag_creates_file(self, tmp_path):
        from click.testing import CliRunner

        from abe_froman.cli.main import cli

        config_path = tmp_path / "workflow.yaml"
        config_path.write_text(
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: a\n    name: A\n    execute:\n"
            f"      url: {_ECHO}\n      params:\n        args: ['-n', 'hi']\n"
        )
        log_path = tmp_path / "events.jsonl"

        runner = CliRunner()
        result = runner.invoke(cli, [
            "run", str(config_path),
            "--workdir", str(tmp_path),
            "--log", str(log_path),
        ])

        assert result.exit_code == 0, result.output
        assert log_path.exists()
        events = [json.loads(l) for l in log_path.read_text().strip().split("\n")]
        event_types = [e["event"] for e in events]
        assert "workflow_start" in event_types
        assert "workflow_end" in event_types
