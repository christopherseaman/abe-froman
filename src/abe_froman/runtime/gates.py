from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abe_froman.schema.models import Evaluation, OutputContract


def build_eval_preamble(
    last_result: dict[str, Any],
    evaluation: Any,
    *,
    attempt: int | None = None,
    total_attempts: int | None = None,
) -> str:
    """Format an evaluation record as a neutral preamble block.

    Same builder serves two callers:
    1. Same-node retry — `compile/nodes.py::inject_retry_reason` passes
       ``attempt`` and ``total_attempts`` populated from
       ``state.retries``; the preamble carries an "Attempt N of M."
       footer line so the model knows how many tries remain.
    2. Goto target with ``include_eval=True`` — the synthetic
       ``_route_<id>`` writes the preamble into state under
       ``_route_eval_preamble``; ``_dispatch_prompt`` auto-prepends it
       to the rendered template body. Caller passes both arguments as
       ``None``; the footer is omitted.

    Lives in ``runtime/gates.py`` (alongside ``EvaluationResult``)
    rather than ``compile/nodes.py`` so the compile-layer route
    dispatcher can import it without violating the layer split
    (compile → runtime imports are allowed; the reverse is not).

    Neutral wording — no "failed" / "fail" / "failure" framing.
    A ``blocking: false`` settled score below threshold is not a
    failure, and goto targets opting in via ``include_eval: true``
    may receive contextual non-failure information (e.g., a passing
    score with feedback worth carrying forward).
    """
    lines: list[str] = []

    threshold = getattr(evaluation, "threshold", None) if evaluation else None
    dimensions = getattr(evaluation, "dimensions", None) if evaluation else None

    if dimensions:
        dim_scores = last_result.get("scores", {}) or {}
        dim_reasons = last_result.get("reasons", {}) or {}
        head_lines = ["Previous evaluation:"]
        for d in dimensions:
            score = dim_scores.get(d.field, 0.0)
            reason = dim_reasons.get(d.field)
            if reason:
                head_lines.append(
                    f"- {d.field}={score:.2f} (min={d.min}): {reason}"
                )
            else:
                head_lines.append(f"- {d.field}={score:.2f} (min={d.min})")
        # Surface any extra dimensions the gate reported beyond what
        # was declared, so unexpected coverage is visible.
        declared = {d.field for d in dimensions}
        for k, v in dim_scores.items():
            if k in declared:
                continue
            reason = dim_reasons.get(k)
            if reason:
                head_lines.append(f"- {k}={v:.2f}: {reason}")
            else:
                head_lines.append(f"- {k}={v:.2f}")
        lines.append("\n".join(head_lines))
    else:
        prev_score = last_result.get("score", 0.0) or 0.0
        if threshold is not None:
            lines.append(
                f"Previous evaluation: score={prev_score:.2f}, "
                f"threshold={threshold}."
            )
        else:
            lines.append(f"Previous evaluation: score={prev_score:.2f}.")

    if last_result.get("feedback"):
        lines.append(f"Feedback: {last_result['feedback']}")
    unmet = last_result.get("pass_criteria_unmet") or []
    if unmet:
        lines.append(
            "Unmet criteria:\n" + "\n".join(f"- {c}" for c in unmet)
        )
    met = last_result.get("pass_criteria_met") or []
    if met:
        lines.append(
            "Met criteria:\n" + "\n".join(f"- {c}" for c in met)
        )

    if attempt is not None and total_attempts is not None:
        lines.append(f"Attempt {attempt} of {total_attempts}.")

    return "\n\n".join(lines)


@dataclass
class EvaluationResult:
    score: float
    scores: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    feedback: str | None = None
    pass_criteria_met: list[str] = field(default_factory=list)
    pass_criteria_unmet: list[str] = field(default_factory=list)


_NON_SCORE_KEYS = frozenset(
    {"feedback", "pass_criteria_met", "pass_criteria_unmet", "score"}
)
_REASON_SUFFIX = "_reason"


def _parse_evaluation_output(
    raw: str, *, allow_bare_float: bool = False, require_score: bool = True,
) -> EvaluationResult:
    """Parse evaluation output into an EvaluationResult.

    Accepts: bare float (script gates only), JSON with "score", full
    feedback JSON, or multi-dimension JSON (numeric fields extracted as
    dimension scores). Loud failure on malformed output.
    """
    stripped = raw.strip()
    if allow_bare_float:
        try:
            return EvaluationResult(score=float(stripped))
        except ValueError:
            pass

    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return EvaluationResult(
            score=0.0,
            feedback=f"gate returned unparseable response: {stripped[:200]!r}",
        )

    if not isinstance(data, dict):
        return EvaluationResult(
            score=0.0,
            feedback="gate response missing or non-numeric 'score' field",
        )

    dim_scores: dict[str, float] = {}
    dim_reasons: dict[str, str] = {}
    for k, v in data.items():
        if k in _NON_SCORE_KEYS:
            continue
        # `<dim>_reason` is reserved for per-dimension string rationale.
        # If a key carries the suffix, capture string values into
        # `reasons[<dim>]`; non-string values are dropped silently
        # (likely author error, never a numeric dim score).
        if k.endswith(_REASON_SUFFIX) and len(k) > len(_REASON_SUFFIX):
            if isinstance(v, str):
                dim_reasons[k[: -len(_REASON_SUFFIX)]] = v
            continue
        if isinstance(v, (int, float)):
            dim_scores[k] = float(v)

    if "score" in data:
        try:
            score = float(data["score"])
        except (TypeError, ValueError):
            return EvaluationResult(
                score=0.0,
                feedback="gate response missing or non-numeric 'score' field",
            )
    elif require_score and not dim_scores:
        return EvaluationResult(
            score=0.0,
            feedback="gate response missing or non-numeric 'score' field",
        )
    else:
        score = 0.0

    met = data.get("pass_criteria_met", []) or []
    unmet = data.get("pass_criteria_unmet", []) or []
    return EvaluationResult(
        score=score,
        scores=dim_scores,
        reasons=dim_reasons,
        feedback=data.get("feedback"),
        pass_criteria_met=list(met) if isinstance(met, list) else [],
        pass_criteria_unmet=list(unmet) if isinstance(unmet, list) else [],
    )


async def run_evaluation_script(
    validator_path: str,
    node_id: str,
    workdir: str,
    node_output: str = "",
    workflow_name: str = "",
    attempt_number: int = 1,
    require_score: bool = True,
) -> EvaluationResult:
    """Run a .py or .js validator script and parse its response.

    The node output is passed via stdin so validators can inspect it.
    Returns an EvaluationResult; bare-float output is wrapped with feedback=None.
    """
    path = Path(validator_path)
    suffix = path.suffix.lower()

    if suffix == ".py":
        cmd = [sys.executable, str(path)]
    elif suffix == ".js":
        cmd = ["node", str(path)]
    else:
        raise ValueError(f"Unsupported validator type: {suffix}")

    import os

    env = {
        **os.environ,
        "NODE_ID": node_id,
        "WORKFLOW_NAME": workflow_name,
        "ATTEMPT_NUMBER": str(attempt_number),
        "WORKDIR": workdir,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
        )
        stdout, stderr = await proc.communicate(input=node_output.encode())
    except (FileNotFoundError, OSError) as e:
        return EvaluationResult(
            score=0.0,
            feedback=f"validator script not found or unexecutable: {validator_path} ({e})",
        )

    if proc.returncode != 0:
        snippet = stderr.decode(errors="replace").strip()[:200]
        return EvaluationResult(
            score=0.0,
            feedback=f"validator exited with code {proc.returncode}: {snippet}",
        )

    return _parse_evaluation_output(
        stdout.decode(), allow_bare_float=True, require_score=require_score,
    )


async def run_evaluation_llm(
    evaluation: Evaluation,
    node_id: str,
    workdir: str,
    node_output: str,
    backend: Any,
    default_model: str,
    attempt_number: int = 1,
    require_score: bool = True,
) -> EvaluationResult:
    """Evaluate a .md prompt-based evaluation via an LLM backend.

    The evaluation's .md file is rendered as a Jinja2 template with the
    node output, node id, and attempt number available as context. The
    backend's response must be JSON matching the feedback schema.
    """
    from abe_froman.runtime.executor.prompt import render_template

    template_path = Path(workdir) / evaluation.validator
    try:
        template_text = template_path.read_text()
    except FileNotFoundError:
        return EvaluationResult(
            score=0.0,
            feedback=f"evaluation template not found: {template_path}",
        )
    rendered = render_template(
        template_text,
        {
            "output": node_output,
            "node_id": node_id,
            "attempt": attempt_number,
        },
    )

    model = evaluation.model or default_model
    result = await backend.send_prompt(rendered, model, workdir)
    if not result.success:
        return EvaluationResult(
            score=0.0,
            feedback=f"evaluation backend error: {result.error}",
        )

    return _parse_evaluation_output(result.output, require_score=require_score)


async def run_evaluation(
    evaluation: Evaluation,
    node_id: str,
    workdir: str = ".",
    node_output: str = "",
    workflow_name: str = "",
    attempt_number: int = 1,
    backend: Any = None,
    default_model: str = "sonnet",
) -> EvaluationResult:
    """Run an evaluation and return an EvaluationResult.

    Script-based validators (.py/.js) are dispatched to subprocess.
    Prompt-based validators (.md) are dispatched to the provided backend.
    """
    path = Path(evaluation.validator)
    suffix = path.suffix.lower()
    require_score = not evaluation.dimensions

    if suffix in (".py", ".js"):
        return await run_evaluation_script(
            evaluation.validator, node_id, workdir, node_output,
            workflow_name=workflow_name, attempt_number=attempt_number,
            require_score=require_score,
        )
    elif suffix == ".md":
        if backend is None:
            raise ValueError(
                f"LLM evaluation validator '{evaluation.validator}' requires a "
                f"PromptBackend but none was provided"
            )
        return await run_evaluation_llm(
            evaluation, node_id, workdir, node_output,
            backend=backend, default_model=default_model,
            attempt_number=attempt_number, require_score=require_score,
        )
    else:
        raise ValueError(f"Unsupported validator type: {suffix}")




def scaffold_output_directory(contract: OutputContract, workdir: str) -> None:
    """Pre-create the output directory tree for a node's output contract."""
    base = Path(workdir) / contract.base_directory
    base.mkdir(parents=True, exist_ok=True)


def validate_output_contract(
    contract: OutputContract,
    workdir: str,
) -> list[str]:
    """Check that all required files exist. Returns list of missing files."""
    base = Path(workdir) / contract.base_directory
    missing = []
    for f in contract.required_files:
        if not (base / f).exists():
            missing.append(str(Path(contract.base_directory) / f))
    return missing
