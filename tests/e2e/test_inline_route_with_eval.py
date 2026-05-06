"""End-to-end: inline route with include_eval drives auto-prepended preamble.

Exercises the full Stage 5c lifecycle without instrumenting a
``PromptBackend`` (per the no-fakes rule):

  execute → eval (with retries exhausted under blocking:false)
    → route picks escalation case (include_eval=True)
    → synthetic ``_route_<id>`` writes a non-empty
      ``state["_route_eval_preamble"]`` carrying the neutral
      eval-result preamble (per-dim scores + ``<dim>_reason`` reasons).
    → goto target's ``build_context`` surfaces the preamble; the
      auto-prepend itself is a pure helper covered by
      ``tests/unit/runtime/test_prompt.py::TestPrependEvalPreamble``.

The asymmetric tests:
  - include_eval=True: state carries the preamble; sender bindings
    resolve.
  - include_eval=False: state carries an empty preamble (or none);
    sender bindings still resolve.

Real subprocess executor for the source node + real script gate for
evaluation. No mocks of the LLM/backend boundary.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    DimensionCheck,
    Evaluation,
    Execute,
    Graph,
    Node,
    Route,
    RouteCase,
    RouteElse,
)


_PYTHON = sys.executable


def _make_low_scoring_validator(tmp_path: Path) -> Path:
    """Validator that always returns a multi-dim low score with reasons."""
    p = tmp_path / "judge.py"
    p.write_text(
        """\
import sys, json
_ = sys.stdin.read()
print(json.dumps({
    "score": 0.30,
    "coverage": 0.40,
    "coverage_reason": "missing definitions for X and Y",
    "quality": 0.20,
    "quality_reason": "shallow analysis with no examples",
    "feedback": "see per-dimension reasons",
}))
"""
    )
    return p


@pytest.mark.asyncio
class TestInlineRouteIncludeEval:
    """End-to-end state assertions for the inline-route → goto path.

    The render-pipeline auto-prepend is covered by
    ``tests/unit/runtime/test_prompt.py::TestPrependEvalPreamble``.
    Here we confirm the synthetic ``_route_<id>`` writes the right
    state so the dispatcher can read it.
    """

    async def _run(self, tmp_path: Path, include_eval: bool) -> dict:
        validator = _make_low_scoring_validator(tmp_path)

        # Goto target is a script that just succeeds — no prompt body
        # to render. We're asserting on state.evaluations and
        # state["_route_eval_preamble"], not on what a backend received.
        config = Graph(
            name="inline-route-eval",
            version="0.1.0",
            nodes=[
                Node(
                    id="classify",
                    name="C",
                    execute=Execute(
                        url=_PYTHON,
                        params={"args": ["-c", "print('initial draft')"]},
                    ),
                    evaluation=Evaluation(
                        validator=str(validator),
                        threshold=0.8,
                        max_retries=0,  # immediate settle
                        blocking=False,  # pass-with-warning
                        dimensions=[
                            DimensionCheck(field="coverage", min=0.6),
                            DimensionCheck(field="quality", min=0.6),
                        ],
                    ),
                    route=Route(
                        cases=[
                            RouteCase(
                                when="True",
                                goto="rewrite",
                                include_eval=include_eval,
                            ),
                        ],
                        **{"else": RouteElse(goto="__end__")},
                    ),
                ),
                Node(
                    id="rewrite",
                    name="R",
                    execute=Execute(
                        url=_PYTHON,
                        params={"args": ["-c", "print('rewrite ran')"]},
                    ),
                ),
            ],
        )

        executor = DispatchExecutor(
            workdir=str(tmp_path), settings=config.settings,
        )
        compiled = build_workflow_graph(config, executor=executor)
        return await compiled.ainvoke(make_initial_state(workdir=str(tmp_path)))

    async def test_include_eval_true_writes_preamble_to_state(self, tmp_path):
        result = await self._run(tmp_path, include_eval=True)

        # Both nodes ran (rewrite reached via inline route's Command).
        assert "classify" in result["node_outputs"]
        assert "rewrite" in result["node_outputs"]

        # Sender threading: the synthetic _route_classify wrote the
        # source node id and the pre-built preamble into state.
        assert result.get("_route_sender") == "classify"
        preamble = result.get("_route_eval_preamble") or ""

        # Neutral wording — no "failed" framing.
        assert "failed" not in preamble.lower()
        # Per-dimension scores AND reasons are present (the parser fix).
        assert "coverage=0.40" in preamble
        assert "missing definitions for X and Y" in preamble
        assert "quality=0.20" in preamble
        assert "shallow analysis with no examples" in preamble
        # Top-level feedback also surfaced.
        assert "see per-dimension reasons" in preamble
        # No retry-context "Attempt N of M" — this is a goto, not a retry.
        assert "Attempt" not in preamble

    async def test_include_eval_false_no_preamble_in_state(self, tmp_path):
        result = await self._run(tmp_path, include_eval=False)

        assert "classify" in result["node_outputs"]
        assert "rewrite" in result["node_outputs"]

        # Sender id is still recorded — that's the always-on identity
        # binding for goto targets.
        assert result.get("_route_sender") == "classify"

        # Preamble is empty (the synthetic dispatcher writes "" when
        # include_eval is false, overwriting any stale value).
        assert (result.get("_route_eval_preamble") or "") == ""
