from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from abe_froman.schema.models import QualityGate


async def evaluate_gate_script(
    validator_path: str,
    phase_id: str,
    workdir: str,
    phase_output: str = "",
    workflow_name: str = "",
    attempt_number: int = 1,
) -> float:
    """Run a .py or .js validator script and parse its score from stdout.

    The phase output is passed via stdin so validators can inspect it.
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
        "PHASE_ID": phase_id,
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
    except (FileNotFoundError, OSError):
        return 0.0

    if proc.returncode != 0:
        return 0.0

    output = stdout.decode().strip()
    try:
        return float(output)
    except ValueError:
        pass

    try:
        data = json.loads(output)
        if isinstance(data, dict) and "score" in data:
            return float(data["score"])
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return 0.0


async def evaluate_gate(
    gate: QualityGate,
    phase_id: str,
    workdir: str = ".",
    phase_output: str = "",
    workflow_name: str = "",
    attempt_number: int = 1,
) -> float:
    """Evaluate a quality gate and return its score.

    For now, script-based validators (.py/.js) are dispatched to subprocess.
    Prompt-based validators (.md) are stubbed to return 1.0 (full implementation
    requires Claude executor integration).
    """
    path = Path(gate.validator)
    suffix = path.suffix.lower()

    if suffix in (".py", ".js"):
        return await evaluate_gate_script(
            gate.validator, phase_id, workdir, phase_output,
            workflow_name=workflow_name, attempt_number=attempt_number,
        )
    elif suffix == ".md":
        return 1.0
    else:
        raise ValueError(f"Unsupported validator type: {suffix}")
