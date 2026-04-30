"""Shared test utilities — imported by test modules, not conftest.py."""

from abe_froman.schema.models import Graph


def make_config(nodes, **settings_kwargs) -> Graph:
    """Build a Graph from a node list and optional settings."""
    return Graph(
        name="Test",
        version="1.0.0",
        nodes=nodes,
        settings=settings_kwargs,
    )


def cmd_phase(id, name="", output="ok", depends_on=None, **kwargs):
    """Shorthand for a command node that echoes a known string."""
    return {
        "id": id,
        "name": name or id,
        "execution": {"type": "command", "command": "echo", "args": ["-n", output]},
        "depends_on": depends_on or [],
        **kwargs,
    }


def fail_phase(id, name="", depends_on=None, **kwargs):
    """Shorthand for a command node that always fails."""
    return {
        "id": id,
        "name": name or id,
        "execution": {"type": "command", "command": "false"},
        "depends_on": depends_on or [],
        **kwargs,
    }
