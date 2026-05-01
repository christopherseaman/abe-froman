"""Shared test utilities — imported by test modules, not conftest.py."""

import shutil

from abe_froman.schema.models import Graph

# Resolve binaries once at import time; the migrate tool uses
# shutil.which the same way, so test helpers stay consistent.
_ECHO = shutil.which("echo") or "/bin/echo"
_FALSE = shutil.which("false") or "/bin/false"


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
        "execute": {"url": _ECHO, "params": {"args": ["-n", output]}},
        "depends_on": depends_on or [],
        **kwargs,
    }


def fail_phase(id, name="", depends_on=None, **kwargs):
    """Shorthand for a command node that always fails."""
    return {
        "id": id,
        "name": name or id,
        "execute": {"url": _FALSE},
        "depends_on": depends_on or [],
        **kwargs,
    }
