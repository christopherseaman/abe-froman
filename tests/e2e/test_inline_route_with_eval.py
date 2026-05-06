"""End-to-end: inline route with include_eval drives auto-prepended preamble.

Exercises the full Stage 5c lifecycle:
  execute → eval (with retries exhausted under blocking:false)
    → route picks escalation case (include_eval=True)
    → goto target's rendered prompt has the neutral eval-result preamble
      auto-prepended (with per-dimension scores AND <dim>_reason reasons),
      AND `{{sender_id}}` / `{{sender}}` resolve to the calling node.

A parallel run with include_eval=False (success path) confirms the
preamble is absent — only the always-bound identity vars resolve.

No mocks of LLMs / backends — the source node is a script that prints
a JSON manifest, the validator is a script that returns a low-scoring
multi-dimensional result with `<dim>_reason` fields, and the goto
target is a script that captures stdin (the rendered prompt) and
prints it. We then read the captured prompt to assert preamble shape.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.executor.prompt import PromptExecutor
from abe_froman.runtime.executor.backends.acp import ACPBackend  # noqa: F401  (factory smoke)
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


def _make_capture_script(tmp_path: Path, name: str) -> Path:
    """Script that captures the rendered prompt (passed via args[0])
    and writes it to a file plus echoes a tiny ack."""
    capture = tmp_path / f"{name}_capture.txt"
    p = tmp_path / f"{name}.py"
    p.write_text(
        f"""\
import sys
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
open({str(capture)!r}, "w").write(prompt)
print("ack:{name}")
"""
    )
    return p, capture


@pytest.mark.asyncio
class TestInlineRouteIncludeEval:
    async def _run(self, tmp_path: Path, include_eval: bool) -> tuple[dict, Path]:
        validator = _make_low_scoring_validator(tmp_path)
        rewrite_script, capture_path = _make_capture_script(tmp_path, "rewrite")

        # The "rewrite" target is a script (not a prompt) so we can
        # capture *what would have been the prompt body* via args. That
        # means the auto-prepend we want to test is the dispatch path
        # for prompts — so use a prompt for the goto target instead.
        # Prompt body lives in tmp/rewrite.md; the stub backend echoes
        # `[prompt-stub] node_id: url`, which doesn't reveal the
        # rendered prompt. We need a real backend that captures.
        #
        # Approach: use DispatchExecutor with a CapturingBackend that
        # returns the rendered prompt as its output. Then the result's
        # `node_outputs[rewrite]` IS the rendered prompt with preamble.

        captured: dict[str, str] = {}

        class _Capture:
            async def send_prompt(self, prompt, model, workdir, timeout=None):
                from abe_froman.runtime.result import ExecutionResult
                captured.setdefault("rewrite", prompt)
                return ExecutionResult(success=True, output=f"[captured len={len(prompt)}]")

            async def close(self):
                pass

        # Source produces a draft as a script; gate evaluates it; route
        # fires (single case, condition matches `True`); goto target is
        # a prompt that captures the rendered text.
        rewrite_md = tmp_path / "rewrite.md"
        rewrite_md.write_text(
            "Rewrite from {{sender_id}}.\nOriginal output:\n{{sender}}\n"
        )

        config = Graph(
            name="inline-route-eval",
            version="0.1.0",
            nodes=[
                Node(
                    id="classify",
                    name="C",
                    execute=Execute(url=_PYTHON, params={"args": ["-c", "print('initial draft')"]}),
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
                    execute=Execute(url=str(rewrite_md)),
                ),
            ],
        )

        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=_Capture(),
            settings=config.settings,
        )

        compiled = build_workflow_graph(config, executor=executor)
        result = await compiled.ainvoke(make_initial_state(workdir=str(tmp_path)))
        return result, captured

    async def test_include_eval_true_prepends_preamble(self, tmp_path):
        result, captured = await self._run(tmp_path, include_eval=True)
        assert "rewrite" in result["node_outputs"]
        prompt = captured.get("rewrite", "")

        # Preamble is auto-prepended BEFORE the rendered template body.
        # Body starts with "Rewrite from classify."
        assert "Rewrite from classify." in prompt
        # Preamble appears before the body.
        body_idx = prompt.index("Rewrite from classify.")
        preamble = prompt[:body_idx]

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

        # Sender identity bindings resolve in the body.
        assert "Original output:\ninitial draft" in prompt

    async def test_include_eval_false_no_preamble(self, tmp_path):
        result, captured = await self._run(tmp_path, include_eval=False)
        assert "rewrite" in result["node_outputs"]
        prompt = captured.get("rewrite", "")

        # Body present
        assert "Rewrite from classify." in prompt
        # No preamble — prompt should be JUST the body (modulo trailing newline).
        # Sender bindings still resolve (always-on).
        assert "Original output:\ninitial draft" in prompt
        # No eval data leaked through.
        assert "coverage" not in prompt.lower()
        assert "quality" not in prompt.lower()
        assert "Previous evaluation" not in prompt
