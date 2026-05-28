"""End-to-end round-trip across the live ``transport: cli`` backend.

Exercises the full CLI pipeline (Click runner + AsyncSqliteSaver +
DispatchExecutor + CLIBackend → real ``claude -p`` subprocess) against
``examples/jokes/workflow.yaml`` with the YAML edited to use a
``transport: cli`` preset. Skipped at collection time when ``claude``
is not on PATH — runs cleanly offline (zero cases execute) and fills
in as the local CLI becomes available.

Why ``examples/jokes/``: ``gates/validate_jokes.py`` is deterministic
(JSON-schema check, prints 0.0 or 1.0). The gate passes against any
model that returns 3 JSON jokes regardless of joke content. Real
flakiness is contained to "did the model produce parseable JSON,"
which is exactly the kind of provider drift a live test catches.

Marked ``pytest.mark.live`` — opt-out with ``pytest -m "not live"``,
opt-in with ``pytest -m live``. Without the flag pytest collects the
test (still skipping when ``claude`` is missing); the marker exists so
CI can isolate live tests.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOKES_SRC = _REPO_ROOT / "examples" / "jokes"


def _stage_jokes(workdir: Path, transport: str, model: str) -> Path:
    """Copy examples/jokes/ into workdir + edit yaml for this transport.

    The CLI run resolves prompt URLs relative to the workdir; copying
    keeps the test hermetic and isolates the per-test SQLite checkpoint.
    """
    dst = workdir / "jokes"
    shutil.copytree(_JOKES_SRC, dst)
    yaml_path = dst / "workflow.yaml"
    text = yaml_path.read_text()
    # Re-target the workflow-relative paths to the copy under workdir.
    text = text.replace(
        'validator: "examples/jokes/gates/validate_jokes.py"',
        'validator: "jokes/gates/validate_jokes.py"',
    )
    text = text.replace(
        'url: "examples/jokes/generate.md"',
        'url: "jokes/generate.md"',
    )
    text = text.replace(
        'url: "examples/jokes/select.md"',
        'url: "jokes/select.md"',
    )
    # Pin transport + model deterministically. The source YAML may
    # already match `transport`; the regex is a no-op in that case.
    text = re.sub(
        r"transport: \w+",
        f"transport: {transport}",
        text, count=1,
    )
    text = text.replace(
        'model: "sonnet"',
        f'model: "{model}"',
    )
    yaml_path.write_text(text)
    return yaml_path


@pytest.mark.live
@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="`claude` CLI not on PATH",
)
def test_jokes_roundtrip_cli_transport(tmp_path):
    """Full CLI run of jokes/workflow.yaml against ``transport: cli``.

    Asserts only structural properties — no joke content checks.
    Failure modes worth catching: cli regressed (`claude -p` flag
    change), model name retired, gate validator drift, prompt template
    breakage end-to-end.
    """
    # Use sonnet — validate_jokes asks for 3 JSON jokes; sonnet
    # produces parseable JSON reliably, haiku occasionally exhausts
    # the 2 retries on schema misses.
    yaml_path = _stage_jokes(tmp_path, transport="cli", model="sonnet")

    runner = CliRunner()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(
            cli,
            ["run", str(yaml_path), "--workdir", str(tmp_path)],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)

    # Exit 0 means the gate passed (validate_jokes returned 1.0) and
    # both nodes completed.
    assert result.exit_code == 0, (
        f"cli run failed (exit {result.exit_code}). "
        f"Output:\n{result.output}"
    )
    assert "Completed: 2 nodes" in result.output, (
        f"Expected 2 nodes (generate + select); got:\n{result.output}"
    )
    assert "generate" in result.output
    assert "select" in result.output
