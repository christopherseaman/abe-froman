"""End-to-end round-trip across all four prompt backends.

Exercises the full CLI pipeline (Click runner + AsyncSqliteSaver +
DispatchExecutor + real backend) against `examples/jokes/workflow.yaml`
for each backend whose API key is configured. Skipped per-backend
when the matching key is absent — runs cleanly offline (zero
parametrized cases execute) and gradually fills in coverage as keys
get configured.

Why jokes/workflow.yaml: ``validate_jokes.py`` is deterministic
(JSON-schema check, prints 0.0 or 1.0). The gate passes against any
backend that returns 5 JSON jokes regardless of joke content. Real
flakiness is contained to "did the model produce parseable JSON,"
which is exactly the kind of provider drift we want a live test to
catch.

Marked ``pytest.mark.live`` — opt-out with ``pytest -m "not live"``,
opt-in with ``pytest -m live``. Without the flag pytest collects them
all (skipping per-key); the marker exists so CI can isolate them.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from abe_froman.cli.main import cli
from abe_froman.runtime.executor.backends.factory import (
    _resolve_anthropic_key,
    _resolve_deepseek_key,
)
from abe_froman.runtime.secrets import resolve_secret


_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOKES_SRC = _REPO_ROOT / "examples" / "jokes"


def _key_for(backend: str) -> str | None:
    """Resolve the env/dotenv key required by each backend."""
    if backend == "anthropic":
        return _resolve_anthropic_key()
    if backend == "deepseek":
        return _resolve_deepseek_key()
    if backend == "openai":
        return resolve_secret("OPENAI_API_KEY")
    if backend == "custom":
        # Custom requires both KEY and BASE_URL; skip if either missing.
        if (
            resolve_secret("CUSTOM_API_KEY")
            and resolve_secret("CUSTOM_API_BASE_URL")
        ):
            return "configured"
        return None
    raise ValueError(f"unknown backend {backend!r}")


def _skip_reason(backend: str) -> str:
    return f"{backend} backend not configured (no key on disk)"


# Models per backend. Cheap tier where available so cost stays
# negligible (<$0.001 per case). Anthropic alias 'haiku' resolves
# via _MODEL_ALIASES; the others are pinned vendor IDs.
_BACKEND_MODELS = {
    # Anthropic uses sonnet (not haiku) because the validate_jokes
    # gate requires reliable structured-JSON output; haiku 4.5
    # empirically failed the 5-joke schema check twice in a row,
    # exhausting retries.
    "anthropic": "sonnet",
    "deepseek": "deepseek-v4-flash",
    "openai": "gpt-4o-mini",
    "custom": "openai/gpt-4o-mini",
}


def _stage_jokes(workdir: Path, backend: str, model: str) -> Path:
    """Copy examples/jokes/ into workdir + edit yaml for this backend.

    The CLI run resolves prompt URLs relative to the workdir; copying
    keeps the test hermetic and isolates the per-test SQLite checkpoint.
    """
    dst = workdir / "jokes"
    shutil.copytree(_JOKES_SRC, dst)
    yaml_path = dst / "workflow.yaml"
    text = yaml_path.read_text()
    # Re-target the validator path to the copy under workdir.
    text = text.replace(
        'validator: "examples/jokes/gates/validate_jokes.py"',
        f'validator: "jokes/gates/validate_jokes.py"',
    )
    text = text.replace(
        'url: "examples/jokes/generate.md"',
        f'url: "jokes/generate.md"',
    )
    text = text.replace(
        'url: "examples/jokes/select.md"',
        f'url: "jokes/select.md"',
    )
    # Pin model + backend deterministically.
    text = text.replace(
        'default_model: "sonnet"',
        f'default_model: "{model}"',
    )
    text = text.replace(
        'executor: "acp"',
        f'executor: "{backend}"',
    )
    yaml_path.write_text(text)
    return yaml_path


@pytest.mark.live
@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            b,
            marks=pytest.mark.skipif(
                _key_for(b) is None, reason=_skip_reason(b),
            ),
        )
        for b in ("anthropic", "deepseek", "openai", "custom")
    ],
)
def test_jokes_roundtrip(backend, tmp_path):
    """Full CLI run of jokes/workflow.yaml against the live backend.

    Asserts only structural properties — no joke content checks.
    Failure modes worth catching: backend regressed (HTTP layer),
    model name retired (404 with confusing message), gate validator
    drift, prompt template breakage.
    """
    model = _BACKEND_MODELS[backend]
    yaml_path = _stage_jokes(tmp_path, backend, model)

    runner = CliRunner()
    # Run from tmp_path so workdir-relative URLs resolve correctly.
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
        f"{backend} run failed (exit {result.exit_code}). "
        f"Output:\n{result.output}"
    )
    assert "Completed: 2 nodes" in result.output, (
        f"Expected 2 nodes (generate + select); got:\n{result.output}"
    )
    assert "generate" in result.output
    assert "select" in result.output
