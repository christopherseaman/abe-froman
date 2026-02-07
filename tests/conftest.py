import pytest
from pathlib import Path

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


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
