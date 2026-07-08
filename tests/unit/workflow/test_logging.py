"""Tests for structured JSONL logging."""

import json
import shutil
from io import StringIO

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.logging import JsonlLogger
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.runtime.executor.dispatch import DispatchExecutor

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


class TestLogUpdate:
    """log_update consumes the partial state delta a node returned (per
    LangGraph stream_mode='updates'); each match in the delta produces
    an event."""

    def test_detects_completed(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({"completed_nodes": {"research"}})
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "node_completed"
        assert events[0]["node"] == "research"

    def test_detects_node_model(self):
        """node_models delta → a node_model event recording preset+model."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "node_models": {"gen": {"model": "sonnet", "preset": "fast"}},
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        e = events[0]
        assert e["event"] == "node_model"
        assert e["node"] == "gen"
        assert e["model"] == "sonnet"
        assert e["preset"] == "fast"

    def test_node_model_emitted_before_completed(self):
        """For a non-gated LLM node both land in one update; node_model
        is emitted first."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "node_models": {"gen": {"model": "sonnet", "preset": "fast"}},
            "completed_nodes": {"gen"},
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert [e["event"] for e in events] == ["node_model", "node_completed"]

    def test_detects_failed(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "failed_nodes": {"build"},
            "errors": [{"node": "build", "error": "exit code 1"}],
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "node_failed"
        assert events[0]["node"] == "build"
        assert events[0]["error"] == "exit code 1"

    def test_node_failed_carries_kind(self):
        """The failure `kind` from the error record is surfaced on the event."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "failed_nodes": {"build"},
            "errors": [{"node": "build", "error": "overloaded", "kind": "overload"}],
        })
        event = json.loads(buf.getvalue().strip())
        assert event["event"] == "node_failed"
        assert event["kind"] == "overload"

    def test_failed_kinds_map_pairs_node_to_kind(self):
        from sqrlly.runtime.logging import failed_kinds_map
        m = failed_kinds_map(
            {"a", "b"},
            [{"node": "a", "error": "x", "kind": "overload"},
             {"node": "b", "error": "y"}],  # no kind → node_error
        )
        assert m == {"a": "overload", "b": "node_error"}

    def test_failed_kinds_map_is_last_write_wins(self):
        """`errors` accumulates within a run (operator.add), so a node can
        carry more than one record. The summary must report the TERMINAL
        (last) kind, not an earlier one."""
        from sqrlly.runtime.logging import failed_kinds_map
        m = failed_kinds_map(
            {"a"},
            [{"node": "a", "error": "old", "kind": "overload"},   # earlier record
             {"node": "a", "error": "new", "kind": "gate_failure"}],  # later record
        )
        assert m == {"a": "gate_failure"}

    def test_failed_kinds_map_excludes_non_failed_nodes(self):
        """A warn_continue error record sits on a COMPLETED node — it must not
        leak into the failed-kind map."""
        from sqrlly.runtime.logging import failed_kinds_map
        m = failed_kinds_map(
            {"a"},
            [{"node": "a", "error": "x", "kind": "gate_failure"},
             {"node": "c", "error": "below threshold"}],  # c not failed
        )
        assert m == {"a": "gate_failure"}

    def test_node_failed_kind_defaults_to_node_error(self):
        """An error record with no `kind` (a legacy/unclassified site) emits
        `node_error` — fail-safe toward halt, never an accidental retry kind."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "failed_nodes": {"build"},
            "errors": [{"node": "build", "error": "boom"}],
        })
        event = json.loads(buf.getvalue().strip())
        assert event["kind"] == "node_error"

    def test_detects_gate(self):
        """gate_evaluated sources from update.evaluations (real scores)."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "evaluations": {
                "research": [{
                    "invocation": 0,
                    "result": {"score": 0.95},
                    "timestamp": "t",
                }],
            },
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "gate_evaluated"
        assert events[0]["node"] == "research"
        assert events[0]["score"] == 0.95
        assert events[0]["invocation"] == 0

    def test_gate_event_surfaces_passed_blocking_threshold(self):
        """A programmatic consumer can tell pass from warn-continue without
        recomputing score < threshold."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "evaluations": {
                "phase_2a": [{
                    "invocation": 0,
                    "result": {"score": 0.42, "passed": False,
                               "blocking": False, "threshold": 0.7},
                }],
            },
        })
        event = json.loads(buf.getvalue().strip())
        assert event["event"] == "gate_evaluated"
        assert event["passed"] is False
        assert event["blocking"] is False
        assert event["threshold"] == 0.7

    def test_detects_multidim_gate(self):
        """Per-dimension scores flow through (closes multi-dim log bug)."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "evaluations": {
                "p": [{
                    "invocation": 0,
                    "result": {"score": 0.0, "scores": {"rigor": 0.8, "humor": 0.5}},
                    "timestamp": "t",
                }],
            },
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert events[0]["event"] == "gate_evaluated"
        assert events[0]["scores"] == {"rigor": 0.8, "humor": 0.5}

    def test_gate_event_surfaces_dimension_thresholds(self):
        """Per-dimension floors flow through so a consumer can attribute which
        dimension blocked without reading the YAML."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "evaluations": {
                "p": [{
                    "invocation": 0,
                    "result": {
                        "score": 0.0,
                        "scores": {"rigor": 0.8, "humor": 0.4},
                        "dimension_thresholds": {"rigor": 0.6, "humor": 0.5},
                        "passed": False,
                    },
                    "timestamp": "t",
                }],
            },
        })
        event = json.loads(buf.getvalue().strip())
        assert event["event"] == "gate_evaluated"
        assert event["dimension_thresholds"] == {"rigor": 0.6, "humor": 0.5}

    def test_detects_retry(self):
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({"retries": {"research": 2}})
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        assert len(events) == 1
        assert events[0]["event"] == "node_retried"
        assert events[0]["node"] == "research"
        assert events[0]["attempt"] == 2

    def test_no_events_on_empty_update(self):
        """A super-step that returns no event-bearing fields produces
        no log lines."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        # node_outputs is not event-bearing — only completed/failed/
        # evaluations/retries trigger events.
        logger.log_update({"node_outputs": {"foo": "bar"}})
        events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        assert events == []

    def test_no_events_on_none_update(self):
        """Command-only nodes (route dispatchers emitting goto without
        any state update) appear in the LangGraph updates stream as
        ``{node_name: None}``. The logger must not crash on the None."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update(None)
        assert buf.getvalue() == ""

    def test_emits_multiple_events_per_update(self):
        """A single update can carry both an evaluation record AND a
        completion (the collapsed-gate pass case, one Command update). Both
        events fire from the one log_update call, in deterministic order."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "evaluations": {"p": [{"invocation": 0, "result": {"score": 0.9}}]},
            "completed_nodes": {"p"},
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        types = [e["event"] for e in events]
        # gate_evaluated comes BEFORE node_completed per the
        # _emit_update_events ordering contract: the collapsed gate emits
        # the record + the outcome in ONE update, and "gate ran, then the
        # node settled" is the order log consumers expect.
        assert types == ["gate_evaluated", "node_completed"]

    def test_gate_evaluated_precedes_node_failed_in_one_update(self):
        """The collapsed gate's fail_blocking Command carries evaluations +
        failed_nodes + errors together; gate_evaluated must still emit
        before node_failed."""
        buf = StringIO()
        logger = JsonlLogger(buf)
        logger.log_update({
            "evaluations": {"p": [{"invocation": 1, "result": {"score": 0.1}}]},
            "failed_nodes": {"p"},
            "errors": [{"node": "p", "error": "Evaluation failed after 1 retries"}],
        })
        events = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        types = [e["event"] for e in events]
        assert types == ["gate_evaluated", "node_failed"]


# ---------------------------------------------------------------------------
# Integration tests: logging through run_workflow
# ---------------------------------------------------------------------------


class TestRunWorkflowLogging:
    """run_workflow takes a caller-owned logger (injection only); the
    caller emits workflow_start / workflow_end and closes — the same
    lifecycle cli/main.py::_run_async owns."""

    @pytest.mark.asyncio
    async def test_logger_captures_workflow_events(self, tmp_path):
        """Two-node workflow should produce start, 2x completed, end."""
        log_path = str(tmp_path / "events.jsonl")
        config = make_config([
            cmd_phase("a", output="hello"),
            cmd_phase("b", output="world", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        logger = JsonlLogger(log_path)
        logger.emit({
            "event": "workflow_start",
            "workflow": config.name,
            "version": config.version,
        })
        result = await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            logger=logger,
        )
        logger.emit({
            "event": "workflow_end",
            "completed": len(result.get("completed_nodes", set())),
            "failed": len(result.get("failed_nodes", set())),
        })
        logger.close()

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
        logger = JsonlLogger(log_path)
        result = await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            logger=logger,
        )
        logger.emit({
            "event": "workflow_end",
            "completed": len(result.get("completed_nodes", set())),
            "failed": len(result.get("failed_nodes", set())),
        })
        logger.close()

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
        logger = JsonlLogger(log_path)
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            logger=logger,
        )
        logger.close()

        events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().strip().split("\n")]
        event_types = [e["event"] for e in events]
        assert "gate_evaluated" in event_types
        assert "node_retried" in event_types

    @pytest.mark.asyncio
    async def test_no_logger_no_side_effects(self, tmp_path):
        """logger=None (the default) should not create any file."""
        config = make_config([cmd_phase("a", output="ok")])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
        )
        # No .jsonl file should exist
        assert list(tmp_path.glob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------


class TestCliLogFlag:
    def test_cli_log_flag_creates_file(self, tmp_path):
        from click.testing import CliRunner

        from sqrlly.cli.main import cli

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


class TestSubgraphLogger:
    """Subgraph-internal events surface in the parent JSONL with the
    subgraph's parent_id prefixed onto each node id. This closes the
    Stage-4c observability gap where `make_subgraph_node` used
    `ainvoke()` and emitted nothing for internal nodes."""

    def test_prefix_is_applied_to_node_field(self):
        from sqrlly.runtime.logging import SubgraphLogger

        buf = StringIO()
        base = JsonlLogger(buf)
        sub = SubgraphLogger(base, prefix="paper")
        sub.emit({"event": "node_completed", "node": "reconcile"})

        record = json.loads(buf.getvalue())
        assert record["node"] == "paper::reconcile"

    def test_nested_prefixing_composes(self):
        from sqrlly.runtime.logging import SubgraphLogger

        buf = StringIO()
        base = JsonlLogger(buf)
        outer = SubgraphLogger(base, prefix="paper")
        inner = SubgraphLogger(outer, prefix="reconcile")
        inner.emit({"event": "node_completed", "node": "step1"})

        record = json.loads(buf.getvalue())
        assert record["node"] == "paper::reconcile::step1"

    def test_log_update_emits_with_prefix(self):
        from sqrlly.runtime.logging import SubgraphLogger

        buf = StringIO()
        base = JsonlLogger(buf)
        sub = SubgraphLogger(base, prefix="paper")
        sub.log_update({"completed_nodes": {"step1", "step2"}})
        records = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        nodes = sorted(r["node"] for r in records)
        assert nodes == ["paper::step1", "paper::step2"]
        for r in records:
            assert r["event"] == "node_completed"

    def test_event_without_node_passes_through(self):
        """Workflow-level events (workflow_start / workflow_end) carry no
        `node` field and should pass through unmodified."""
        from sqrlly.runtime.logging import SubgraphLogger

        buf = StringIO()
        base = JsonlLogger(buf)
        sub = SubgraphLogger(base, prefix="paper")
        sub.emit({"event": "workflow_start", "workflow": "x"})
        record = json.loads(buf.getvalue())
        assert "node" not in record
        assert record["event"] == "workflow_start"


class TestSubgraphEventsInParentLog:
    """End-to-end: a parent workflow with a subgraph reference (`execute.url:
    sub.yaml`) emits the subgraph's internal node_completed events into
    the parent JSONL with the parent_id prefix."""

    @pytest.mark.asyncio
    async def test_subgraph_internal_events_surface_with_prefix(self, tmp_path):
        # Two-node subgraph; both internal nodes complete during the run.
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: step1\n    name: Step 1\n"
            f"    execute:\n      url: {_ECHO}\n"
            "      params: {args: ['s1']}\n"
            "  - id: step2\n    name: Step 2\n    depends_on: [step1]\n"
            f"    execute:\n      url: {_ECHO}\n"
            "      params: {args: ['s2']}\n"
        )

        config = make_config([
            {
                "id": "paper",
                "name": "Paper",
                "execute": {"url": "sub.yaml"},
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))

        # Inject a logger so build_workflow_graph hands it to subgraph wrappers.
        log_path = tmp_path / "events.jsonl"
        logger = JsonlLogger(log_path)
        logger.emit({"event": "workflow_start", "workflow": "parent", "version": "1.0"})

        compiled = build_workflow_graph(
            config, executor, _base_dir=tmp_path, logger=logger,
        )
        await run_workflow(
            compiled,
            make_initial_state(workdir=str(tmp_path)),
            config,
            logger=logger,
        )
        logger.emit({"event": "workflow_end", "completed": 1, "failed": 0})
        logger.close()

        records = [json.loads(l) for l in log_path.read_text().strip().split("\n")]
        nodes_completed = sorted(
            r["node"] for r in records if r.get("event") == "node_completed"
        )
        # Parent-level: `paper` itself completes when the subgraph wrapper
        # returns. Subgraph internals appear with the `paper::` prefix.
        assert "paper" in nodes_completed
        assert "paper::step1" in nodes_completed
        assert "paper::step2" in nodes_completed

    @pytest.mark.asyncio
    async def test_no_logger_means_no_subgraph_events(self, tmp_path):
        """Backward compat: no logger argument = no astream cost, no events.
        Confirms the event-streaming path is opt-in."""
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\nversion: '1.0'\n"
            "nodes:\n"
            f"  - id: only\n    name: Only\n    execute:\n      url: {_ECHO}\n"
            "      params: {args: ['x']}\n"
        )
        config = make_config([
            {
                "id": "wrapper",
                "name": "Wrapper",
                "execute": {"url": "sub.yaml"},
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        # No logger anywhere — the subgraph wrapper should fall through to
        # the ainvoke() path and complete without raising.
        compiled = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await run_workflow(
            compiled, make_initial_state(workdir=str(tmp_path)), config,
        )
        assert "wrapper" in result["completed_nodes"]
