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
    # absurd-paper: run-essential subset (reference-output/ + view.html excluded).
    "sqrlly/_examples/absurd-paper/workflow.yaml",
    "sqrlly/_examples/absurd-paper/preamble.md",
    "sqrlly/_examples/absurd-paper/subgraphs/compose_and_validate.yaml",
    "sqrlly/_examples/absurd-paper/prompts/abstract.md",
    "sqrlly/_examples/absurd-paper/prompts/choose_topic.md",
    "sqrlly/_examples/absurd-paper/prompts/discussion.md",
    "sqrlly/_examples/absurd-paper/prompts/intro.md",
    "sqrlly/_examples/absurd-paper/prompts/methods.md",
    "sqrlly/_examples/absurd-paper/prompts/outline.md",
    "sqrlly/_examples/absurd-paper/prompts/reconcile.md",
    "sqrlly/_examples/absurd-paper/prompts/results.md",
    "sqrlly/_examples/absurd-paper/gates/abstract_multi_dim.md",
    "sqrlly/_examples/absurd-paper/gates/choose_topic_eval.md",
    "sqrlly/_examples/absurd-paper/gates/outline_json.py",
    "sqrlly/_examples/absurd-paper/gates/submission_check.py",
    "sqrlly/_examples/absurd-paper/scripts/persist_paper.py",
    "sqrlly/_examples/absurd-paper/scripts/pick_topic.py",
    "sqrlly/_examples/absurd-paper/scripts/render_pdf.py",
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
