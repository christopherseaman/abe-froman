from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import click
import yaml

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.foreman import ForemanExecutor
from abe_froman.runtime.runner import run_workflow
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import Graph

CHECKPOINT_DB = ".abe-froman-checkpoint.db"


def _is_git_repo(workdir: str) -> bool:
    """True if workdir is inside a git working tree."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, check=False,
        )
        return r.returncode == 0 and r.stdout.strip() == b"true"
    except FileNotFoundError:
        return False


def load_config(config_file: str) -> Graph:
    path = Path(config_file)
    if not path.exists():
        raise click.BadParameter(f"File not found: {config_file}")
    raw = yaml.safe_load(path.read_text())
    return Graph(**raw)


def _thread_id_for(config: Graph, workdir: str) -> str:
    """Deterministic thread_id for a (workflow, workdir) pair."""
    key = f"{config.name}:{Path(workdir).resolve()}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _db_path(workdir: str) -> str:
    return str(Path(workdir) / CHECKPOINT_DB)


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
            f"Valid: {config.name} v{config.version} ({len(config.nodes)} nodes)"
        )
    except Exception as e:
        raise click.ClickException(str(e))


@cli.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.option(
    "--dry-run", is_flag=True,
    help="Print rewritten YAML to stdout without writing the file.",
)
@click.option(
    "--in-place", is_flag=True,
    help="Rewrite the file on disk (default: print rewritten YAML to stdout).",
)
def migrate(config_file: str, dry_run: bool, in_place: bool):
    """Migrate a pre-Stage-4 workflow YAML to the current schema.

    Rewrites: phases → nodes, quality_gate → evaluation,
    dynamic_subphases → fan_out (with template lift + final_phases
    promoted to sibling nodes with depends_on).

    Comments, anchors, and templated {{}} strings are preserved.
    Idempotent: running on already-migrated YAML is a no-op.
    """
    from abe_froman.cli.migrate import migrate_file

    path = Path(config_file)
    rewritten, changes = migrate_file(path, in_place=in_place, dry_run=dry_run)

    if not changes:
        click.echo(f"No changes needed for {config_file}", err=True)
        return

    for c in changes:
        click.echo(f"  - {c}", err=True)

    if in_place and not dry_run:
        click.echo(f"Wrote {len(changes)} changes to {config_file}", err=True)
    else:
        click.echo(rewritten, nl=False)


@cli.command()
@click.argument("config_file", type=click.Path())
def graph(config_file: str):
    """Render the compiled LangGraph as a Mermaid diagram."""
    try:
        config = load_config(config_file)
        compiled = build_workflow_graph(config)
    except Exception as e:
        raise click.ClickException(str(e))

    click.echo(compiled.get_graph().draw_mermaid())


async def _run_async(
    config: Graph,
    workdir: str,
    dry_run: bool,
    executor_type: str,
    resume: bool,
    log_file: str | None,
) -> dict:
    """Inner async runner — wires checkpointer, executor, and state."""
    thread_id = _thread_id_for(config, workdir)

    if dry_run:
        compiled = build_workflow_graph(config, None)
        state = make_initial_state(
            workflow_name=config.name, workdir=workdir, dry_run=True,
        )
        return await run_workflow(compiled, state, config, log_file=log_file)

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from abe_froman.runtime.executor.backends.factory import create_prompt_backend

    backend = create_prompt_backend(executor_type)
    dispatch = DispatchExecutor(
        workdir=workdir, prompt_backend=backend, settings=config.settings,
    )

    async with AsyncSqliteSaver.from_conn_string(_db_path(workdir)) as cp:
        await cp.setup()
        state: dict
        if resume:
            prev = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
            if prev is None:
                raise click.ClickException(
                    f"No saved state for this workflow at {_db_path(workdir)}"
                )
            old = dict(prev.checkpoint.get("channel_values", {}))
            state = {
                **old,
                "failed_nodes": [],
                "retries": {},
                "errors": [],
                "workdir": workdir,
                "dry_run": False,
            }
            click.echo(
                f"Resuming: {len(state.get('completed_nodes', []))} "
                f"nodes already completed"
            )
            # Wipe the thread so reducers don't merge with stale state
            await cp.adelete_thread(thread_id)
        else:
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workflow_name=config.name, workdir=workdir, dry_run=False,
            )

        if _is_git_repo(workdir):
            executor_obj = ForemanExecutor(
                inner=dispatch,
                base_workdir=workdir,
                max_parallel_jobs=config.settings.max_parallel_jobs,
                per_model_limits=dict(config.settings.per_model_limits),
                rehydrate=dict(state.get("node_worktrees", {})),
                settings=config.settings,
            )
        else:
            click.echo(
                "Note: workdir is not a git repo — running without worktree "
                "isolation (foreman disabled)."
            )
            executor_obj = dispatch

        compiled = build_workflow_graph(config, executor_obj, checkpointer=cp)
        try:
            result = await run_workflow(
                compiled, state, config,
                thread_id=thread_id, log_file=log_file,
            )
        finally:
            await executor_obj.close()

        return result


@cli.command()
@click.argument("config_file", type=click.Path())
@click.option("--workdir", "-w", default=".", help="Working directory")
@click.option(
    "--dry-run", is_flag=True, help="Validate and trace without executing"
)
@click.option("--model", "-m", help="Override default model")
@click.option("--executor", "-e", help="Prompt executor backend (stub, acp)")
@click.option(
    "--resume", is_flag=True, help="Resume from the last checkpoint"
)
@click.option("--log", "log_file", type=click.Path(), help="JSONL log output file")
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    model: str | None,
    executor: str | None,
    resume: bool,
    log_file: str | None,
):
    """Run a workflow from a configuration file."""
    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    if model:
        config.settings.default_model = model

    executor_type = executor or config.settings.executor

    result = asyncio.run(
        _run_async(config, workdir, dry_run, executor_type, resume, log_file)
    )

    completed = result.get("completed_nodes", [])
    failed = result.get("failed_nodes", [])
    errors = result.get("errors", [])

    if dry_run:
        click.echo(f"Dry run completed: {len(completed)} nodes traced")
    else:
        click.echo(f"Completed: {len(completed)} nodes")

    token_usage = result.get("token_usage", {})
    if token_usage:
        total_in = sum(t.get("input", 0) for t in token_usage.values())
        total_out = sum(t.get("output", 0) for t in token_usage.values())
        click.echo(
            f"  Tokens: {total_in + total_out:,} total "
            f"({total_in:,} in, {total_out:,} out)"
        )

    if completed:
        click.echo(f"  Nodes: {', '.join(completed)}")

    if failed:
        click.echo(f"  Failed: {', '.join(failed)}")

    if errors:
        for err in errors:
            click.echo(f"  Error in {err['node']}: {err['error']}")

    if failed:
        raise SystemExit(1)
