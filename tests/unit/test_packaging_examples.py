"""The curated examples must ship in the wheel under sqrlly/_examples/."""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

EXPECTED = [
    "sqrlly/_examples/jokes/workflow.yaml",
    "sqrlly/_examples/jokes/generate.md",
    "sqrlly/_examples/jokes/select.md",
    "sqrlly/_examples/jokes/gates/validate_jokes.py",
    "sqrlly/_examples/route_classify/workflow.yaml",
    "sqrlly/_examples/route_classify/scripts/triage.py",
    "sqrlly/_examples/explicit_join.yaml",
    "sqrlly/_examples/pipeline_style/workflow.yaml",
]


def test_curated_examples_present_in_wheel(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=repo, check=True, capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [p for p in EXPECTED if p not in names]
    assert not missing, f"missing from wheel: {missing}"
