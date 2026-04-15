from __future__ import annotations

import asyncio
from pathlib import Path

import click
import yaml

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.workflow.persistence import load_state, state_file_path
from abe_froman.workflow.resume import prepare_resume_state, prepare_start_state
from abe_froman.workflow.runner import run_workflow
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
@click.option("--model", "-m", help="Override default model")
@click.option("--executor", "-e", help="Prompt executor backend (stub, acp)")
@click.option("--resume", is_flag=True, help="Resume from last saved state")
@click.option("--start", "start_phase", help="Start from a specific phase")
@click.option("--log", "log_file", type=click.Path(), help="JSONL log output file")
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    model: str | None,
    executor: str | None,
    resume: bool,
    start_phase: str | None,
    log_file: str | None,
):
    """Run a workflow from a configuration file."""
    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    if resume and start_phase:
        raise click.ClickException("Cannot use both --resume and --start")

    if model:
        config.settings.default_model = model

    executor_type = executor or config.settings.executor

    if resume:
        saved = load_state(workdir)
        if saved is None:
            raise click.ClickException(
                f"No state file found at {state_file_path(workdir)}"
            )
        state = prepare_resume_state(saved, config, workdir)
        click.echo(
            f"Resuming: {len(state['completed_phases'])} phases already completed"
        )
    elif start_phase:
        saved = load_state(workdir)
        if saved is None:
            raise click.ClickException(
                f"--start requires a prior state file at {state_file_path(workdir)}"
            )
        state = prepare_start_state(saved, config, start_phase, workdir)
        click.echo(
            f"Starting from {start_phase}: "
            f"{len(state['completed_phases'])} upstream phases cached"
        )
    else:
        state = make_initial_state(
            workflow_name=config.name,
            workdir=workdir,
            dry_run=dry_run,
        )

    if dry_run:
        executor_obj = None
    else:
        from abe_froman.runtime.executor.backends.factory import create_prompt_backend

        backend = create_prompt_backend(executor_type)
        executor_obj = DispatchExecutor(
            workdir=workdir,
            prompt_backend=backend,
            settings=config.settings,
        )

    compiled = build_workflow_graph(config, executor_obj)
    result = asyncio.run(
        run_workflow(compiled, state, config, persist=not dry_run, log_file=log_file)
    )

    if executor_obj is not None:
        asyncio.run(executor_obj.close())

    completed = result.get("completed_phases", [])
    failed = result.get("failed_phases", [])
    errors = result.get("errors", [])

    if dry_run:
        click.echo(f"Dry run completed: {len(completed)} phases traced")
    else:
        click.echo(f"Completed: {len(completed)} phases")

    token_usage = result.get("token_usage", {})
    if token_usage:
        total_in = sum(t.get("input", 0) for t in token_usage.values())
        total_out = sum(t.get("output", 0) for t in token_usage.values())
        click.echo(f"  Tokens: {total_in + total_out:,} total ({total_in:,} in, {total_out:,} out)")

    if completed:
        click.echo(f"  Phases: {', '.join(completed)}")

    if failed:
        click.echo(f"  Failed: {', '.join(failed)}")

    if errors:
        for err in errors:
            click.echo(f"  Error in {err['phase']}: {err['error']}")

    if failed:
        raise SystemExit(1)
