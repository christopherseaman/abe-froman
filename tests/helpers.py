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
