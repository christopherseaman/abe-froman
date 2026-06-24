from __future__ import annotations

import asyncio
import functools
import hashlib
from pathlib import Path
from typing import Any

import click
import yaml

from sqrlly.cli.init import (
    init_command, init_example, init_list_examples, init_skill,
)
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.lint import collect_warnings
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.promote import (
    PromoteConflictError, fanout_branch_specs, reconcile_promotions,
)
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

CHECKPOINT_DB = ".sqrlly-checkpoint.db"


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


def _run_artifact_summary(
    config: Graph, workdir: str, log_file: str | None,
) -> list[str]:
    """Lines telling the user where this run left its output, log, and
    artifacts. The live renderer shows only per-node status, so this
    epilogue is the one place a normal terminal run surfaces locations.

    - output: promoted files declared via top-level `output_contract`s that
      actually exist on disk; otherwise a pointer to run state + `--log`.
    - log: the resolved `--log` path, or how to capture one.
    - artifacts: the checkpoint DB and the foreman worktree pool, each
      listed only when present (a pure non-git script run has neither).
    """
    wd = Path(workdir)
    label_w = 12

    def row(label: str, value: str) -> str:
        return f"  {label:<{label_w}}{value}"

    # output — declared contract files that were actually produced
    seen: set[Path] = set()
    produced: list[Path] = []
    for node in config.nodes:
        if node.output_contract is None:
            continue
        for rel in node.output_contract.required_paths():
            p = (wd / rel).resolve()
            if p in seen:
                continue
            seen.add(p)
            if p.exists():
                produced.append(p)
    lines = ["where to find things:"]
    if produced:
        lines.append(row("output:", ", ".join(str(p) for p in produced)))
    else:
        lines.append(row(
            "output:",
            "in run state (node outputs) — pass --log <file>.jsonl to capture",
        ))

    # log
    if log_file:
        lines.append(row("log:", str(Path(log_file).resolve())))
    else:
        lines.append(row("log:", "not captured — re-run with --log run.jsonl"))

    # artifacts — checkpoint DB + worktree pool, only if present
    artifacts: list[str] = []
    db = (wd / CHECKPOINT_DB).resolve()
    if db.exists():
        artifacts.append(f"checkpoint {db}")
    pool = (wd / ".sqrlly").resolve()
    if pool.exists():
        artifacts.append(f"worktrees {pool}/")
    if artifacts:
        lines.append(row("artifacts:", "; ".join(artifacts)))
    return lines


def _emit_warnings(config: Graph) -> None:
    """Print advisory lint warnings to stderr (non-fatal)."""
    for warning in collect_warnings(config):
        click.echo(click.style(f"warning: {warning}", fg="yellow"), err=True)


@click.group()
@click.version_option(package_name="sqrlly", prog_name="sqrlly")
def cli():
    """sqrlly — workflow orchestrator."""
    pass


@cli.command()
@click.argument("directory", default=None, required=False, type=click.Path())
@click.option(
    "--skill", is_flag=True,
    help="Install the agent skill into <repo>/.agents/skills/sqrlly/ "
         "instead of scaffolding a workflow.",
)
@click.option(
    "--example", "example", default=None, metavar="NAME",
    help="Scaffold a bundled example (see --list-examples).",
)
@click.option(
    "--list-examples", "list_examples", is_flag=True,
    help="List the bundled examples available to --example.",
)
def init(
    directory: str | None, skill: bool, example: str | None,
    list_examples: bool,
):
    """Scaffold a runnable workflow into DIRECTORY.

    Bare: a minimal workflow (DIRECTORY default `.`). With --skill: install
    the agent skill doc into the working repo. With --example NAME: scaffold
    a bundled example (DIRECTORY default `./NAME`). --list-examples prints the
    available examples.
    """
    if sum(bool(x) for x in (skill, example, list_examples)) > 1:
        raise click.ClickException(
            "--skill, --example, and --list-examples may not be combined."
        )
    if list_examples:
        init_list_examples()
    elif example:
        init_example(example, directory or example)
    elif skill:
        init_skill(directory or ".")
    else:
        init_command(directory or ".")


@cli.command()
@click.argument("config_file", type=click.Path())
def validate(config_file: str):
    """Validate a workflow configuration file."""
    try:
        config = load_config(config_file)
        build_workflow_graph(config)
    except Exception as e:
        raise click.ClickException(str(e))
    _emit_warnings(config)
    click.echo(
        f"Valid: {config.name} v{config.version} ({len(config.nodes)} nodes)"
    )


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


@cli.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.option(
    "--log", "log_file", type=click.Path(exists=True),
    help="JSONL log from a prior run; adds status overlay + per-node "
         "event slices. Without it, the page renders authoring view "
         "(topology + per-node config only).",
)
@click.option(
    "--out", "out_path", type=click.Path(),
    help="Output HTML path. Defaults to "
         "<workdir>/sqrlly-view.html.",
)
@click.option(
    "--workdir", "-w", default=".",
    help="Working directory (used to resolve --out default).",
)
@click.option(
    "--direction", default="TB",
    type=click.Choice(["TB", "LR", "BT", "RL"], case_sensitive=False),
    help="Mermaid layout direction. TB (top-to-bottom, default), "
         "LR (left-to-right), BT, RL.",
)
def view(
    config_file: str,
    log_file: str | None,
    out_path: str | None,
    workdir: str,
    direction: str,
):
    """Render a workflow as a self-contained HTML viewer.

    Two modes:
      Authoring: `sqrlly view <yaml>` — topology + per-node
        config panel. No runtime overlay.
      Debug: `sqrlly view <yaml> --log <jsonl>` — same plus
        status overlay (passed/failed/retried/untouched) and
        per-node log slices on click.
    """
    from sqrlly.cli.view import read_jsonl_log, render_view

    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    events: list[dict] | None = None
    if log_file:
        try:
            events = read_jsonl_log(Path(log_file))
        except Exception as e:
            raise click.ClickException(f"Reading log: {e}")

    html = render_view(config, events, direction=direction.upper())

    if out_path is None:
        out_path = str(Path(workdir) / "sqrlly-view.html")
    Path(out_path).write_text(html)
    click.echo(f"Wrote {out_path}", err=True)


async def _run_async(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    log_file: str | None,
    quiet: bool = False,
    entry: str | None = None,
) -> dict:
    """Inner async runner — wires checkpointer, executor, and state.

    Logger lifecycle: constructed up front so the compile layer can hand
    it to subgraph wrappers (subgraph-internal events project into the
    same JSONL, prefixed with the parent node id). The runner itself
    only handles the outer state stream; CLI owns workflow_start /
    workflow_end / close().

    Two subscribers may attach to the event stream:
      - `JsonlLogger` when `--log <path>` is set
      - `TerminalRenderer` when stdout is a TTY and `--quiet` not set
    `TeeLogger` fans events to both when both are present.
    """
    import sys

    from sqrlly.runtime.logging import JsonlLogger
    from sqrlly.runtime.terminal import TeeLogger, TerminalRenderer

    thread_id = _thread_id_for(config, workdir)

    subs: list[Any] = []
    jsonl: JsonlLogger | None = None
    if log_file is not None:
        jsonl = JsonlLogger(log_file)
        subs.append(jsonl)
    if not quiet and not dry_run and sys.stdout.isatty():
        subs.append(TerminalRenderer(config))

    logger: Any | None
    if len(subs) == 0:
        logger = None
    elif len(subs) == 1:
        logger = subs[0]
    else:
        logger = TeeLogger(*subs)

    if logger is not None:
        logger.emit({
            "event": "workflow_start",
            "workflow": config.name,
            "version": config.version,
        })

    result: dict = {}
    try:
        result = await _execute_workflow(
            config, workdir, dry_run, preset_override, resume, resume_from, rerun_all,
            thread_id=thread_id, logger=logger, entry=entry,
        )
        return result
    finally:
        if logger is not None:
            # `result` stays {} if _execute_workflow raised before
            # returning; workflow_end then logs zeros. The detailed
            # error surfaces via Click already.
            logger.emit({
                "event": "workflow_end",
                "completed": len(result.get("completed_nodes", set())),
                "failed": len(result.get("failed_nodes", set())),
            })
            logger.close()


def _collect_subgraph_presets(
    config: Graph,
    base_dir: str,
    _depth: int = 0,
    _seen: set[str] | None = None,
) -> dict:
    """Recursively gather presets declared by every subgraph the
    workflow references.

    A subgraph node resolves ``params.preset`` against its own
    ``settings.presets`` (merged at runtime), so those preset names
    must have backends in the CLI-built registry. This walks the
    subgraph tree and returns ``{name: Preset}`` for all of them.

    Subgraph YAML that fails to load is skipped silently here — the
    real load error surfaces during ``build_workflow_graph``.
    """
    from sqrlly.compile.subgraph import load_graph, node_subgraph_path

    if _seen is None:
        _seen = set()
    collected: dict = {}
    if _depth >= config.settings.max_subgraph_depth:
        return collected
    for node in config.nodes:
        sub_path = node_subgraph_path(node)
        if sub_path is None or sub_path in _seen:
            continue
        _seen.add(sub_path)
        try:
            sub = load_graph(sub_path, base_dir)
        except Exception:
            continue
        for name, preset in sub.settings.presets.items():
            collected[name] = preset
        collected.update(
            _collect_subgraph_presets(sub, base_dir, _depth + 1, _seen)
        )
    return collected


async def _execute_workflow(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    *,
    thread_id: str,
    logger: Any | None,
    entry: str | None = None,
) -> dict:
    """Compile the graph, wire executors / checkpointer / state, then run.

    Backend wiring: ``build_preset_registry`` returns a fully-resolved
    ``dict[str, Preset]`` — the user's ``settings.presets`` with
    ``preset_override`` applied. Empty ``settings.presets`` raises;
    sqrlly does not synthesize defaults. Each preset gets a lazy backend
    builder; the backend is constructed on first dispatch to that preset,
    so a missing CLI or optional dependency surfaces as a backend error
    at the first call site (and unused presets never build at all), not
    as a pre-flight.
    """
    if dry_run:
        compiled = build_workflow_graph(config, None, logger=logger)
        state = make_initial_state(
            workflow_name=config.name, workdir=workdir, dry_run=True,
        )
        return await run_workflow(compiled, state, config, logger=logger)

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from sqrlly.runtime.executor.backends.factory import (
        create_backend_from_preset,
    )
    from sqrlly.runtime.executor.preset import build_preset_registry
    from sqrlly.schema.models import LlmPreset

    registry = build_preset_registry(
        config.settings, cli_override=preset_override,
    )
    # Reflect the (possibly synthesized or flipped) root registry in
    # the in-flight settings — root-scope runtime resolution reads
    # ``config.settings.presets``. This stays the ROOT registry only
    # (one ``default: true``); subgraph presets live in each subgraph's
    # own merged settings.
    config.settings.presets = registry

    # The backend registry, however, needs every preset NAME any node
    # — including subgraph nodes — might resolve. Subgraph nodes
    # resolve against their own settings.presets, so fold the whole
    # subgraph tree's presets into the backend set. A name shared
    # across scopes must describe the same backend (the registry is
    # flat, name-keyed); ``default``-flag differences are scope-local
    # and don't count as a conflict.
    all_presets = dict(registry)
    for name, preset in _collect_subgraph_presets(config, workdir).items():
        existing = all_presets.get(name)
        if existing is not None and (
            existing.model_dump(exclude={"default"})
            != preset.model_dump(exclude={"default"})
        ):
            raise click.ClickException(
                f"Preset {name!r} is declared with conflicting definitions "
                f"across the workflow and its subgraphs. A preset name "
                f"shared between scopes must describe the same backend."
            )
        all_presets.setdefault(name, preset)

    # Only LLM presets become PromptBackends — command presets describe
    # script interpreters, not LLM transports, and are consulted at
    # script-dispatch time, not wired as backends. Register builders, not
    # built backends: a declared-but-unused preset (a common case — e.g.
    # the jokes example ships both `cli` and `acp`) never constructs its
    # backend, so its optional dependency (the `acp` package) is only
    # imported if a node actually dispatches to it.
    prompt_backend_builders = {
        name: functools.partial(create_backend_from_preset, preset)
        for name, preset in all_presets.items()
        if isinstance(preset, LlmPreset)
    }
    dispatch = DispatchExecutor(
        workdir=workdir, prompt_backend_builders=prompt_backend_builders,
        settings=config.settings,
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
            from sqrlly.compile.resume import compute_skip_set
            prior_completed = set(old.get("completed_nodes", set()))
            skip = (
                set()
                if rerun_all
                else compute_skip_set(
                    config, prior_completed,
                    set(old.get("failed_nodes", set())), set(resume_from),
                )
            )
            state = {
                **old,
                "completed_nodes": skip,
                "failed_nodes": set(), "retries": {}, "errors": [],
                "workdir": workdir, "dry_run": False, "_resume_skip": skip,
            }
            if rerun_all:
                source = "all nodes (rerun-all)"
            elif resume_from:
                source = ", ".join(sorted(resume_from))
            else:
                source = "failed nodes"
            click.echo(
                f"Resuming: skipping {len(skip)} completed; re-running "
                f"{len(prior_completed - skip)} (from: {source})."
            )
            # Wipe the thread so reducers don't merge with stale state
            await cp.adelete_thread(thread_id)
        elif entry is not None:
            # Cold start at `entry`: NO checkpoint read. Seed FRESH state, then
            # compute the skip set as if every top-level node had already
            # completed and `entry` were the sole rerun target — the existing
            # dirty closure dirties `entry` + its downstream and freezes the
            # rest. Reseed completed_nodes + _resume_skip to that frozen set so
            # downstream join/barrier guards see upstream as done, exactly like
            # the resume reseed. The entry node trusts ON-DISK upstream
            # artifacts: upstream never ran this session, so node_outputs is
            # empty and `{{upstream}}` template vars resolve to nothing — an
            # --entry node must read files, not interpolate upstream output.
            from sqrlly.compile.resume import compute_skip_set
            all_ids = {n.id for n in config.nodes}
            skip = compute_skip_set(config, all_ids, set(), {entry})
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workflow_name=config.name, workdir=workdir, dry_run=False,
            )
            state["completed_nodes"] = set(skip)
            state["_resume_skip"] = set(skip)
            click.echo(
                f"Cold start at {entry!r}: running "
                f"{len(all_ids - skip)} node(s) ({entry} + downstream); "
                f"freezing {len(skip)} upstream (trusting on-disk artifacts)."
            )
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
            # Only surface in non-interactive contexts. When the live
            # renderer owns stdout, any mid-run write (even on stderr)
            # collides with the cursor-managed grid.
            import sys
            if not sys.stdout.isatty():
                click.echo(
                    "Note: workdir is not a git repo — running without "
                    "worktree isolation (foreman disabled).",
                    err=True,
                )
            executor_obj = dispatch

        compiled = build_workflow_graph(
            config, executor_obj, checkpointer=cp, logger=logger,
        )
        try:
            result = await run_workflow(
                compiled, state, config,
                thread_id=thread_id, logger=logger,
            )
            clean = not result.get("failed_nodes")
            # Promotion runs only for top-level nodes on a clean run, and
            # BEFORE GC (reclaim would remove the tree first). A promotion
            # error propagates (it is a data-loss path) rather than being
            # swallowed like reclaim's best-effort cleanup.
            if clean and isinstance(executor_obj, ForemanExecutor):
                specs: list[tuple[str, str, list[str] | None]] = []
                for node in config.nodes:
                    if not node.promote:
                        continue
                    tree = executor_obj.get_worktree(node.id)
                    if tree is None:
                        continue  # `off` node already wrote to the base workdir
                    # output_contract.required_files (base_directory-prepended)
                    # doubles as the promote pathspec filter when present;
                    # otherwise the full delta is promoted (discover mode).
                    globs = (
                        node.output_contract.required_paths()
                        if node.output_contract else None
                    )
                    specs.append((node.id, tree, globs))
                # Fan-out branch worktrees opt in via fan_out.promote — route
                # them through the same reconcile path (inherits conflict
                # detection + promote_exclude + promote_include). They are recorded in
                # node_worktrees keyed <parent>::<item>.
                promote_parents = {
                    n.id for n in config.nodes
                    if n.fan_out is not None and n.fan_out.promote
                }
                specs.extend(fanout_branch_specs(
                    promote_parents, result.get("node_worktrees", {}),
                ))
                if specs:
                    try:
                        plan = reconcile_promotions(
                            specs, workdir,
                            config.settings.on_promote_conflict,
                            excludes=config.settings.promote_exclude or None,
                            includes=config.settings.promote_include or None,
                        )
                    except PromoteConflictError as e:
                        raise click.ClickException(str(e)) from e
                    if (plan.conflicts
                            and config.settings.on_promote_conflict == "warn"):
                        for path, nodes in sorted(plan.conflicts.items()):
                            click.echo(click.style(
                                f"warning: promote conflict on {path!r} — "
                                f"promoted by {', '.join(nodes)}; "
                                f"last-write-wins",
                                fg="yellow"), err=True)
                if config.settings.worktree_gc == "on_success":
                    await executor_obj.reclaim()
        finally:
            await executor_obj.close()

        return result


@cli.command()
@click.argument("config_file", type=click.Path())
@click.option("--workdir", "-w", default=".", help="Working directory")
@click.option(
    "--dry-run", is_flag=True, help="Validate and trace without executing"
)
@click.option(
    "--preset", "-p",
    help=(
        "Force a specific named preset as the default for this run. "
        "Must exist in settings.presets. Without this flag, the YAML's "
        "default: true preset applies; if settings.presets is empty, "
        "only script/binary/subgraph nodes can run (LLM nodes have no "
        "backend)."
    ),
)
@click.option(
    "--resume", is_flag=True, help="Resume from the last checkpoint"
)
@click.option(
    "--resume-from", "resume_from", multiple=True,
    help="Re-run this node and everything downstream; freeze upstream. "
         "Implies --resume. Repeatable.",
)
@click.option(
    "--rerun-all", "rerun_all", is_flag=True,
    help="With --resume: re-execute every node (pre-0.6 full replay; "
         "disables skip-completed).",
)
@click.option(
    "--entry", "entry", default=None, metavar="NODE",
    help="Cold-start at NODE: run NODE and everything downstream, freezing "
         "everything upstream WITHOUT a checkpoint (the upstream artifacts "
         "must already be on disk). The entry node sees EMPTY {{upstream}} "
         "template vars — it must read files, not interpolate upstream "
         "output. Mutually exclusive with --resume / --resume-from / "
         "--rerun-all.",
)
@click.option("--log", "log_file", type=click.Path(), help="JSONL log output file")
@click.option(
    "--quiet", "-q", is_flag=True,
    help="Suppress the live terminal renderer (useful in CI/piped runs).",
)
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    preset: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    entry: str | None,
    log_file: str | None,
    quiet: bool,
):
    """Run a workflow from a configuration file."""
    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    resume = resume or bool(resume_from)
    if rerun_all and resume_from:
        raise click.ClickException(
            "--rerun-all and --resume-from are mutually exclusive"
        )
    valid_ids = {n.id for n in config.nodes}
    for rid in resume_from:
        if "::" in rid:
            raise click.ClickException(
                f"--resume-from {rid!r}: fan-out children are not addressable across runs"
            )
        if rid not in valid_ids:
            raise click.ClickException(
                f"--resume-from {rid!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    if entry is not None:
        # --entry is a COLD start (no checkpoint); --resume family reads a
        # checkpoint. The two seed strategies are incompatible.
        if resume or rerun_all:
            raise click.ClickException(
                "--entry is mutually exclusive with --resume, "
                "--resume-from, and --rerun-all"
            )
        if "::" in entry:
            raise click.ClickException(
                f"--entry {entry!r}: fan-out children are not addressable "
                f"(name the top-level fan-out parent instead)"
            )
        if entry not in valid_ids:
            raise click.ClickException(
                f"--entry {entry!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    _emit_warnings(config)

    from sqrlly.runtime.result import EvaluationError, ManifestError, RouteError
    try:
        result = asyncio.run(
            _run_async(config, workdir, dry_run, preset, resume, resume_from, rerun_all, log_file, quiet, entry)
        )
    except (EvaluationError, ManifestError, RouteError) as e:
        # Infrastructure failures (broken validator / unreadable manifest
        # / unevaluable route predicate) halt the run loudly with a clean
        # message, not a traceback.
        raise click.ClickException(str(e))

    completed = result.get("completed_nodes", set())
    failed = result.get("failed_nodes", set())
    errors = result.get("errors", [])

    # When the live renderer was active, it already shows per-node
    # status + a "done in Xs" header — skip the redundant summary
    # block. Errors still surface so failures aren't hidden.
    import sys
    renderer_active = (
        not quiet
        and not dry_run
        and sys.stdout.isatty()
    )

    if not renderer_active:
        if dry_run:
            click.echo(f"Dry run completed: {len(completed)} nodes traced")
        else:
            click.echo(f"Completed: {len(completed)} nodes")
        if completed:
            click.echo(f"  Nodes: {', '.join(sorted(completed))}")
        if failed:
            click.echo(f"  Failed: {', '.join(sorted(failed))}")

    if errors:
        for err in errors:
            click.echo(f"  Error in {err['node']}: {err['error']}", err=True)

    # Announce where the run left things. The live renderer shows only
    # per-node status, so this is the one place a normal terminal run
    # surfaces output / log / artifact locations. Printed on success AND
    # failure (the log + checkpoint are exactly what you want on failure).
    if not quiet and not dry_run:
        for line in _run_artifact_summary(config, workdir, log_file):
            click.echo(line)

    if failed:
        raise click.exceptions.Exit(1)
