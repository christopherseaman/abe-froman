"""End-to-end: route node primitive.

Three multi-function flows verify Command(goto=) dispatch end-to-end:

    Flow A — score-based routing on evaluation history. A judge node's
    score determines whether ship runs or the workflow halts.

    Flow B — route reads accumulated evaluation history. The existing
    evaluation: max_retries machinery loops produce→judge until either
    the gate passes or runs out of attempts; then route halts via
    __end__ on history length.

    Flow C — route on structured_output produced by an upstream
    executor (forward-compat for Stage 5b's schema work). Uses
    MockExecutor (the protocol-conforming test double) to populate
    structured_output deterministically.

All three flows use the real DispatchExecutor (or MockExecutor for C);
gates are real subprocess scripts under tests/fixtures/route/.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import make_initial_state

from helpers import cmd_phase, make_config
from mock_executor import MockExecutor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "route"

_ECHO = shutil.which("echo") or "/bin/echo"


def _route_node(id, depends_on, cases, else_target):
    return {
        "id": id, "name": id, "depends_on": depends_on,
        "execute": {
            "type": "route",
            "cases": cases,
            "else": else_target,
        },
    }


def _gated_node(id, validator_path, depends_on=None, max_retries=0):
    """Gate-only-by-elision: omit `execute`, supply `evaluation`."""
    return {
        "id": id, "name": id,
        "depends_on": depends_on or [],
        "evaluation": {
            "validator": str(validator_path),
            "threshold": 0.5,
            "blocking": False,
            "max_retries": max_retries,
        },
    }


class TestFlowAScoreRouting:
    """produce → judge → route → ship | __end__"""

    @pytest.mark.asyncio
    async def test_high_score_routes_to_ship(self, tmp_path):
        config = make_config([
            cmd_phase("produce", output="draft"),
            _gated_node("judge", FIXTURES / "score_high.py", depends_on=["produce"]),
            _route_node(
                "decide",
                depends_on=["judge"],
                cases=[{
                    "when": "history['judge'][-1]['result']['score'] >= 0.5",
                    "goto": "ship",
                }],
                else_target="__end__",
            ),
            cmd_phase("ship", output="shipped"),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "produce" in result["completed_nodes"]
        assert "judge" in result["completed_nodes"]
        assert "ship" in result["completed_nodes"]
        assert result["node_outputs"]["ship"] == "shipped"

    @pytest.mark.asyncio
    async def test_low_score_routes_to_end(self, tmp_path):
        config = make_config([
            cmd_phase("produce", output="draft"),
            _gated_node("judge", FIXTURES / "score_low.py", depends_on=["produce"]),
            _route_node(
                "decide",
                depends_on=["judge"],
                cases=[{
                    "when": "history['judge'][-1]['result']['score'] >= 0.5",
                    "goto": "ship",
                }],
                else_target="__end__",
            ),
            cmd_phase("ship", output="shipped"),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "produce" in result["completed_nodes"]
        assert "judge" in result["completed_nodes"]
        assert "ship" not in result["completed_nodes"]


class TestFlowBHistoryDrivenHalt:
    """produce → judge (max_retries=3, all fail) → route halts on history length.

    The existing evaluation:max_retries machinery drives the produce-
    judge loop. Route makes the terminal topology decision based on
    accumulated history.
    """

    @pytest.mark.asyncio
    async def test_route_halts_after_max_retries(self, tmp_path):
        config = make_config([
            cmd_phase("produce", output="draft"),
            _gated_node(
                "judge", FIXTURES / "score_by_attempt.py",
                depends_on=["produce"], max_retries=3,
            ),
            _route_node(
                "decide",
                depends_on=["judge"],
                cases=[{
                    "when": "history['judge'][-1]['result']['score'] >= 0.5",
                    "goto": "ship",
                }],
                else_target="__end__",
            ),
            cmd_phase("ship", output="shipped"),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        # judge attempted multiple times (max_retries=3 means up to 4 total
        # attempts); each writes a record to evaluations history. All scored
        # 0.3 (below threshold), so route gotos __end__ — ship never runs.
        history = result["evaluations"]["judge"]
        assert len(history) >= 2, (
            f"Expected accumulated history; got {len(history)} entries"
        )
        for record in history:
            assert record["result"]["score"] == 0.3
        assert "ship" not in result["completed_nodes"]


class TestFlowCStructuredOutputRouting:
    """Route on a structured_output dict from upstream executor.

    Forward-compat for Stage 5b: when backends populate structured_output,
    route can dispatch on producer output dict fields directly without
    going through an evaluate gate. Today this requires a custom executor
    since neither stub nor command writes structured_output, but the
    namespace path is exercised end-to-end.
    """

    @pytest.mark.asyncio
    async def test_urgent_category_routes_to_escalate(self, tmp_path):
        config = make_config([
            {
                "id": "produce", "name": "produce", "depends_on": [],
                "execute": {"url": _ECHO, "params": {"args": ["produced"]}},
            },
            _route_node(
                "decide",
                depends_on=["produce"],
                cases=[
                    {"when": "produce['category'] == 'urgent'", "goto": "escalate"},
                    {"when": "produce['category'] == 'normal'", "goto": "ship"},
                ],
                else_target="__end__",
            ),
            cmd_phase("escalate", output="escalated"),
            cmd_phase("ship", output="shipped"),
        ])
        executor = MockExecutor(results={
            "produce": ExecutionResult(
                success=True,
                output="produced",
                structured_output={"category": "urgent"},
            ),
            "escalate": ExecutionResult(success=True, output="escalated"),
            "ship": ExecutionResult(success=True, output="shipped"),
        })
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "produce" in result["completed_nodes"]
        assert "escalate" in result["completed_nodes"]
        assert "ship" not in result["completed_nodes"]
        assert result["node_outputs"]["escalate"] == "escalated"

    @pytest.mark.asyncio
    async def test_normal_category_routes_to_ship(self, tmp_path):
        config = make_config([
            {
                "id": "produce", "name": "produce", "depends_on": [],
                "execute": {"url": _ECHO, "params": {"args": ["produced"]}},
            },
            _route_node(
                "decide",
                depends_on=["produce"],
                cases=[
                    {"when": "produce['category'] == 'urgent'", "goto": "escalate"},
                    {"when": "produce['category'] == 'normal'", "goto": "ship"},
                ],
                else_target="__end__",
            ),
            cmd_phase("escalate", output="escalated"),
            cmd_phase("ship", output="shipped"),
        ])
        executor = MockExecutor(results={
            "produce": ExecutionResult(
                success=True,
                output="produced",
                structured_output={"category": "normal"},
            ),
            "escalate": ExecutionResult(success=True, output="escalated"),
            "ship": ExecutionResult(success=True, output="shipped"),
        })
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "produce" in result["completed_nodes"]
        assert "ship" in result["completed_nodes"]
        assert "escalate" not in result["completed_nodes"]
