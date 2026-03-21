"""Shared test utilities — imported by test modules, not conftest.py."""

from abe_froman.schema.models import WorkflowConfig


def make_config(phases, **settings_kwargs) -> WorkflowConfig:
    """Build a WorkflowConfig from a phase list and optional settings."""
    return WorkflowConfig(
        name="Test",
        version="1.0.0",
        phases=phases,
        settings=settings_kwargs,
    )


def cmd_phase(id, name="", output="ok", depends_on=None, **kwargs):
    """Shorthand for a command phase that echoes a known string."""
    return {
        "id": id,
        "name": name or id,
        "execution": {"type": "command", "command": "echo", "args": ["-n", output]},
        "depends_on": depends_on or [],
        **kwargs,
    }


def fail_phase(id, name="", depends_on=None, **kwargs):
    """Shorthand for a command phase that always fails."""
    return {
        "id": id,
        "name": name or id,
        "execution": {"type": "command", "command": "false"},
        "depends_on": depends_on or [],
        **kwargs,
    }
