from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abe_froman.schema.models import Evaluation, OutputContract


@dataclass
class EvaluationResult:
    score: float
    scores: dict[str, float] = field(default_factory=dict)
    feedback: str | None = None
    pass_criteria_met: list[str] = field(default_factory=list)
    pass_criteria_unmet: list[str] = field(default_factory=list)


_NON_SCORE_KEYS = frozenset(
    {"feedback", "pass_criteria_met", "pass_criteria_unmet", "score"}
)


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
    for k, v in data.items():
        if k not in _NON_SCORE_KEYS and isinstance(v, (int, float)):
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
        feedback=data.get("feedback"),
        pass_criteria_met=list(met) if isinstance(met, list) else [],
        pass_criteria_unmet=list(unmet) if isinstance(unmet, list) else [],
    )


async def run_evaluation_script(
    validator_path: str,
    node_id: str,
    workdir: str,
    phase_output: str = "",
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
        stdout, stderr = await proc.communicate(input=phase_output.encode())
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
    phase_output: str,
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
            "output": phase_output,
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
    phase_output: str = "",
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
            evaluation.validator, node_id, workdir, phase_output,
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
            evaluation, node_id, workdir, phase_output,
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
