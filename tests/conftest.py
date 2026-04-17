import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _has_acp_adapter() -> bool:
    """Check whether @zed-industries/claude-code-acp is resolvable via npx."""
    npx = shutil.which("npx")
    if npx is None:
        return False
    try:
        result = subprocess.run(
            [npx, "--no-install", "@zed-industries/claude-code-acp", "--version"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pytest_collection_modifyitems(config, items):
    """Hard pre-req checks — fail collection loudly instead of letting tests error
    cryptically at runtime."""
    acp_tests = [i for i in items if "tests/acp/" in str(i.fspath).replace("\\", "/")]
    if acp_tests:
        missing = []
        if importlib.util.find_spec("acp") is None:
            missing.append("Python `acp` package (`pip install agent-client-protocol`)")
        if not _has_acp_adapter():
            missing.append(
                "`@zed-industries/claude-code-acp` (`npm i -g @zed-industries/claude-code-acp`)"
            )
        if missing:
            pytest.exit(
                "ACP tests collected but pre-reqs missing:\n  - "
                + "\n  - ".join(missing)
                + "\nInstall the above, or run with `--ignore=tests/acp`.",
                returncode=4,
            )

    node_tests = [
        i
        for i in items
        if "TestGateJSValidator" in i.nodeid or i.nodeid.endswith("::test_js_validator_returns_score")
    ]
    if node_tests and shutil.which("node") is None:
        pytest.exit(
            "JS gate tests collected but `node` not on PATH.\n"
            "Install Node.js (https://nodejs.org) or deselect the JS gate tests.",
            returncode=4,
        )


@pytest.fixture
def minimal_config_dict():
    return {
        "name": "Test Workflow",
        "version": "1.0.0",
        "phases": [
            {
                "id": "phase-1",
                "name": "First Phase",
                "prompt_file": "phases/phase-1.md",
            }
        ],
    }


@pytest.fixture
def multi_phase_config_dict():
    return {
        "name": "Multi Phase",
        "version": "1.0.0",
        "phases": [
            {
                "id": "phase-1",
                "name": "First",
                "prompt_file": "phases/phase-1.md",
            },
            {
                "id": "phase-2",
                "name": "Second",
                "prompt_file": "phases/phase-2.md",
                "depends_on": ["phase-1"],
            },
        ],
    }


@pytest.fixture
def parallel_config_dict():
    """Diamond dependency: A -> (B, C) -> D"""
    return {
        "name": "Parallel Workflow",
        "version": "1.0.0",
        "phases": [
            {"id": "a", "name": "A", "prompt_file": "a.md"},
            {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["a"]},
            {
                "id": "d",
                "name": "D",
                "prompt_file": "d.md",
                "depends_on": ["b", "c"],
            },
        ],
    }


@pytest.fixture
def example_workflow_path():
    return EXAMPLES_DIR / "example_workflow.yaml"
