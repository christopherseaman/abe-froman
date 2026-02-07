from __future__ import annotations

import asyncio
from pathlib import Path

import click
import yaml

from abe_froman.engine.builder import build_workflow_graph
from abe_froman.engine.state import make_initial_state
from abe_froman.executor.dispatch import DispatchExecutor
from abe_froman.executor.mock import MockExecutor
from abe_froman.schema.models import WorkflowConfig


def load_config(config_file: str) -> WorkflowConfig:
    path = Path(config_file)
    if not path.exists():
        raise click.BadParameter(f"File not found: {config_file}")
    raw = yaml.safe_load(path.read_text())
    return WorkflowConfig(**raw)


@click.group()
def cli():
    """Abe Froman — workflow orchestrator."""
    pass


@cli.command()
@click.argument("config_file", type=click.Path())
def validate(config_file: str):
    """Validate a workflow configuration file."""
    try:
        config = load_config(config_file)
        build_workflow_graph(config)
        click.echo(
            f"Valid: {config.name} v{config.version} ({len(config.phases)} phases)"
        )
    except Exception as e:
        raise click.ClickException(str(e))


@cli.command()
@click.argument("config_file", type=click.Path())
def graph(config_file: str):
    """Print the dependency graph structure."""
    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    click.echo(f"Workflow: {config.name} v{config.version}")
    click.echo()

    for phase in config.phases:
        deps = ""
        if phase.depends_on:
            dep_list = ", ".join(phase.depends_on)
            deps = f" → depends_on: [{dep_list}]"

        exec_type = ""
        if phase.execution:
            exec_type = f" ({phase.execution.type})"

        gate = ""
        if phase.quality_gate:
            blocking = " BLOCKING" if phase.quality_gate.blocking else ""
            gate = f" [gate: {phase.quality_gate.threshold}{blocking}]"

        model_info = ""
        if phase.model:
            model_info = f" [model: {phase.model}]"

        click.echo(
            f"  {phase.id}: {phase.name}{exec_type}{model_info}{gate}{deps}"
        )


@cli.command()
@click.argument("config_file", type=click.Path())
@click.option("--workdir", "-w", default=".", help="Working directory")
@click.option(
    "--dry-run", is_flag=True, help="Validate and trace without executing"
)
@click.option("--phase", "-p", help="Run a specific phase only")
@click.option("--model", "-m", help="Override default model")
@click.option("--executor", "-e", help="Prompt executor backend (stub, acp)")
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    phase: str | None,
    model: str | None,
    executor: str | None,
):
    """Run a workflow from a configuration file."""
    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    if model:
        config.settings.default_model = model

    executor_type = executor or config.settings.executor

    if dry_run:
        executor_obj = MockExecutor()
    else:
        from abe_froman.executor.backends.factory import create_prompt_backend

        backend = create_prompt_backend(executor_type)
        executor_obj = DispatchExecutor(
            workdir=workdir,
            prompt_backend=backend,
            settings=config.settings,
        )

    compiled = build_workflow_graph(config, executor_obj)

    state = make_initial_state(
        workflow_name=config.name,
        workdir=workdir,
        dry_run=dry_run,
        current_phase_id=phase,
    )

    # TODO: LangGraph checkpointing for workflow resume on failure
    #   builder.compile(checkpointer=SqliteSaver(...)) enables resume from
    #   last successful state. Requires thread_id tracking per run.
    result = asyncio.run(compiled.ainvoke(state))

    if hasattr(executor_obj, "close"):
        asyncio.run(executor_obj.close())

    completed = result.get("completed_phases", [])
    failed = result.get("failed_phases", [])
    errors = result.get("errors", [])

    if dry_run:
        click.echo(f"Dry run completed: {len(completed)} phases traced")
    else:
        click.echo(f"Completed: {len(completed)} phases")

    if completed:
        click.echo(f"  Phases: {', '.join(completed)}")

    if failed:
        click.echo(f"  Failed: {', '.join(failed)}")

    if errors:
        for err in errors:
            click.echo(f"  Error in {err['phase']}: {err['error']}")

    if failed:
        raise SystemExit(1)
