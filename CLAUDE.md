# sqrlly — Operator Notes for Claude

Workflow orchestrator using LangGraph for graph topology and Claude
(via the local ACP adapter) / scripts for execution.

This file is operational guidance for Claude Code working inside this
repo. Narrative documentation lives elsewhere:

- **`README.md`** — what the project is, install, quickstart, concept
  tour, CLI overview, examples gallery (PyPI-facing front page).
- **`SCHEMA.md`** — the exhaustive field-by-field schema reference
  (Settings / Node / Execute / presets / route / evaluation), URL
  dispatch table, route-predicate namespace.
- **`SKILLS.md`** — agent skill doc: instructions for an AI coding
  agent authoring and running sqrlly workflows (Codex skill format).
- **`TODO.md`** — open work (prioritized + non-prioritized) plus the
  consolidated deferred-defects/cleanups log (each with a diagnosis).
- **`CHANGELOG.md`** — release history.

Read those when narrative context is needed; this file stays focused on
day-to-day operator concerns (build, test, layout, limitations,
environment quirks).

## Architecture (30-second sketch)

```
YAML → Pydantic Graph → build_workflow_graph() → compiled LangGraph
                                                         │
                                                         ▼
                              Execution Node → ForemanExecutor → DispatchExecutor
                                    │         (queue +              │
                                    │          worktree             │
                                    │          pool)        ┌───────┼───────┐
                                    │                       ▼       ▼       ▼
                                    │                    Prompt  Script  Binary
                                    ▼                                Subgraph
                              Evaluation (script or LLM)
                                    │
                                  ┌─┼─┐
                                  ▼ ▼ ▼
                                pass retry fail   (router reads state; no reclassify)
```

The three-layer breakdown is in "Project Layout" below; layer rules are
enforced by `tests/architecture/test_layers.py`.

## Build & Test

```bash
uv sync                                      # core deps
npm i -g @zed-industries/claude-code-acp     # for ACP backend / acp tests
# `claude` CLI on PATH                       # for cli backend / cli tests

uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -v   # ~1k tests, ~50s
uv run pytest tests/acp -v                   # ACP tests, ~2 min, requires npm package above
uv run pytest tests/cli -v                   # CLI tests, ~15s, requires `claude` on PATH
uv run pytest -m live -v                     # restored live-roundtrip (cli-only)
uv run pytest tests/architecture/test_layers.py  # layer rule enforcement

uv run sqrlly validate config.yaml
uv run sqrlly run config.yaml             # uses settings.presets (required)
uv run sqrlly run config.yaml -p <name>   # force a named preset from settings.presets
uv run sqrlly run config.yaml --resume    # resume from checkpoint
uv run sqrlly run config.yaml --entry <node>  # cold-start: run <node> + downstream, no checkpoint
uv run sqrlly run config.yaml --log out.jsonl
uv run sqrlly graph config.yaml           # Mermaid topology
uv run sqrlly view config.yaml            # self-contained HTML viewer
```

CLI commands: `init`, `validate`, `run`, `graph`, `view`. There is no
`migrate` subcommand — legacy-YAML migration is the standalone
`scripts/migrate_legacy_executor_to_presets.py` (PEP-723; run with
`uv run`).

## Versioning

**Push to main and release are different operations.** Commits land
on main freely as work completes and tests pass. A *release*
(`scripts/release.sh ...`) happens only when there's something worth
surfacing to users — typically a batch of commits since the last
tag, not every individual push.

When releasing:

- Default to **patch**. Bug fixes, docs, polish, small additions
  (e.g., a CLI subcommand on the scale of `init`) bundle into a
  patch. Multiple commits per patch is normal.
- **Minor** (`0.x.0`) is reserved for substantial new features or
  surface changes that warrant a changelog heads-up — the kind of
  thing a user tracking releases should be alerted to, not folded
  into routine maintenance. Examples that earned minor: the
  `transport: api` strip (0.2.0), `transport: cli` addition (0.3.0).
- **Always confirm with the operator before a `minor` bump.** The
  operator decides what constitutes "worth a heads-up."

Mechanically: `scripts/release.sh patch` is the only no-confirmation
default; `minor` and `major` need explicit operator approval.

## Project Layout

Three-layer split (enforced by `tests/architecture/test_layers.py`):

**`src/sqrlly/schema/`** — Pydantic models (no langgraph imports).
- `models.py` — `Graph`, `Node`, `Settings`, `Execute`, `Evaluation`,
  `DimensionCheck`, `RouteCase`, `RouteElse`, `Route`, `OutputContract`,
  `FanOut`, `FanOutTemplate`, `FanOutFinalNode`, `LlmPreset`,
  `CommandPreset`.
- `params.py` — `PromptParams`, `SubgraphParams`,
  `SubprocessParams` + `coerce_params()` resolver.

**`src/sqrlly/compile/`** — YAML → LangGraph (no cli imports).
- `graph.py` — `build_workflow_graph()`, edge wiring, evaluation
  router insertion. Carries `_make_inline_route_node` for Stage 5c
  inline routes; synthetic `_route_<id>` dispatchers are registered
  for execute+route nodes (post-eval pass target = `_route_<id>`,
  which emits `Command(goto=...)`).
- `nodes.py` — `_make_execution_node`, `_make_evaluation_node`, pure
  helpers (`build_context`, `inject_retry_reason`,
  `classify_evaluation_outcome`, `run_evaluation_and_outcome`).
- `dynamic.py` — fan-out via Send, inline retry loop.
- `subgraph.py` — recursive composition, cycle detection, depth cap.
- `route.py` — simpleeval namespace + predicate evaluation
  (langgraph-free).
- `evaluation.py` — `evaluation_to_routes()`, `walk_routes()` —
  desugars Evaluation → first-match route ladder.
- `lint.py` — `collect_warnings()` — pure, langgraph-free advisory
  footgun checks (non-fatal); surfaced by `validate` + `run`.

**`src/sqrlly/runtime/`** — executors, backends, gates, foreman
(no compile/langgraph imports, except `url.py` which is also
langgraph-free).
- `state.py` — `WorkflowState` TypedDict + `REDUCERS`.
- `result.py` — `ExecutionResult`, `NodeExecutor` Protocol,
  `PromptBackend` Protocol, `OverloadError`.
- `runner.py` — `astream` loop, state-diff event detection.
- `logging.py` — `JsonlLogger`, `SubgraphLogger` (prefix decorator).
- `terminal.py` — `TerminalRenderer`, `TeeLogger` — live TTY event
  renderer (JsonlLogger-shaped `emit`/`log_update`/`close`).
- `gates.py` — `run_evaluation_script`, `run_evaluation_llm`,
  output parsing, `EvaluationResult`.
- `foreman.py` — `ForemanExecutor` (semaphores + memory back-pressure
  + worktree pool).
- `promote.py` — `discover_changes` / `apply_changes` / `plan_promotions`
  / `reconcile_promotions` / `PromoteConflictError`: cross-node promote
  reconciliation under `settings.on_promote_conflict` (discover→plan→apply).
- `settings_merge.py` — `merge_settings(parent, child)` for
  scope-aware inheritance.
- `url.py` — `resolve_url`, `fetch_url`, `_RemoteFetchCache`,
  remote URL gates.
- `secrets.py` — `resolve_secret(name, *, settings, settings_attr)`
  (env → workflow YAML → project-local `.env`, walking up from CWD).
- `worktree_share.py` — `write_worktree_excludes` / `materialize_shares` / `ensure_setup` (worktree dep sharing: info/exclude write, read-only symlinks, sentinel-gated rehydrate).
- `executor/dispatch.py` — `DispatchExecutor` (10-row URL dispatch).
- `executor/prompt.py` — `PromptExecutor` (template render, model
  downgrade).
- `executor/preset.py` — `resolve_preset_name` / `build_preset_registry`
  (named-preset → backend registry; CLI `--preset` override).
- `executor/backends/{acp,cli,factory}.py`. Two LLM backends
  coexist: ACP (warm `claude-code-acp` adapter) and CLI
  (subprocess-per-call `claude -p`).
  `factory.create_backend_from_preset` is a two-row lookup table
  keyed on `(transport, provider)`.
- `executor/backends/_overload.py` — `maybe_raise_overload` +
  `ACP_OVERLOAD_SUBSTRINGS`. Both ACP and CLI backends share the
  same substring set because both ultimately hit the same upstream
  Claude API; the historical name "ACP" is retained.
- `executor/backends/_acp_policy.py` — pure ACP tool-permission policy
  (`permission_mode` + tool lists → allow/deny by tool kind+title).

**`src/sqrlly/cli/`** — entry point + helpers.
- `main.py` — Click CLI (`init` / `validate` / `run` / `graph` / `view`);
  wires `AsyncSqliteSaver`, `ForemanExecutor`, `thread_id`,
  `JsonlLogger`.
- `view.py` — `view` command: self-contained HTML workflow viewer.
- `init.py` — `init` command: scaffolds a minimal workflow
  (`workflow.yaml` + `prompts/hello.md`) for `pipx`-installed users
  with no repo on disk. `init --skill` instead installs the agent
  skill doc (repo-root `SKILLS.md`, `force-include`d into the wheel as
  `sqrlly/_skill.md`) into a working repo at
  `.agents/skills/sqrlly/SKILL.md` (repo-aware via git toplevel).
- `migrate.py` — internal module (NOT a CLI command): pre-Stage-4 →
  4 → 5b YAML transforms (idempotent; preserves comments + anchors).
  The `sqrlly migrate` subcommand was removed in the preset-rework
  cutover; `migrate_yaml` / `migrate_file` survive as a tested
  library utility.

## Testing principles

These guide all new tests; violations should be flagged in review.

1. **No mocks of external systems.** Tests use real subprocess / real
   ACP / real validators. `MockExecutor` (`tests/mock_executor.py`) is
   a custom test double implementing the `NodeExecutor` Protocol —
   NOT `unittest.mock`.
2. **Tests validate output, not just absence-of-error.** Every test
   asserts a specific value: an output string, a state key, a file's
   contents, a graph shape. "Did not raise" is not a passing
   assertion.
3. **Known-good AND known-bad fixtures for function-level tests.**
   Every helper gets a success path AND a failure path. Use
   `@pytest.mark.parametrize` for routing tables.
4. **Multi-function E2E tests use simple, scenario-scoped workflows.**
   Linear / diamond / fan-out / resume / ACP each get a fixture
   tailored to the scenario.
5. **No separate test codepaths in source.** Functions must not have
   `if testing:` branches. If a function can't be tested without
   special-casing, redesign it.
6. **No skip-around-the-bug.** No `try/except: pytest.skip(...)` to
   mask missing dependencies. ACP is a hard pre-req at collection
   time (see `tests/conftest.py`); skip is `--ignore=tests/acp`.
7. **If testing is impossible** (missing auth, unreliable env): STOP
   and raise the question. Do not paper over.
8. **Quality > count.** A few tests that pin meaningful output beat
   many tests that only check for absence of exceptions.
9. **Layer boundary tests** (`tests/architecture/test_layers.py`)
   enforce the three-layer split via AST walking. New source files
   must respect the import rules.
10. **ACP tests require `@zed-industries/claude-code-acp`** installed
    globally. Pre-flight in `tests/conftest.py` exits collection with
    install instructions if missing.
11. **Live-backend tests use `pytest.mark.live` + `pytest.mark.skipif(
    KEY is None, ...)`.** Self-skipping when the matching API key is
    absent on disk; opt-in for isolation via `pytest -m live`. Never
    fail loudly without a key — silent skip is the contract. The
    round-trip suite at `tests/e2e/test_live_backend_roundtrip.py`
    exercises the full CLI pipeline against `examples/jokes/` per
    backend.

The `feedback_no_fake_backends.md` memory expands on (1): no fake
`PromptBackend` doubles; real ACP / real subprocess only. The one
sanctioned exception is orchestration instrumentation (e.g. patching
`factory.shutil.which` to choose which environment-shape branch the
resolver sees) — distinct from faking what an external system returns.

## Known limitations

- **`--entry <node>` is a COLD start, not a resume** — `sqrlly run
  --entry <node>` seeds FRESH state (no checkpoint read), freezes everything
  upstream of `<node>`, and runs `<node>` + its downstream
  (`cli/main.py::_execute_workflow` reuses `compile/resume.py::compute_skip_set`
  with `prior_completed = {all node ids}`, `prior_failed = set()`,
  `rerun_targets = {entry}`). Mutually exclusive with `--resume` /
  `--resume-from` / `--rerun-all`. **The entry node trusts ON-DISK inputs:**
  upstream never ran this session, so `node_outputs` is empty and a `{{upstream}}`
  Jinja var resolves to nothing — an `--entry` node must READ FILES, not
  interpolate upstream output. Rejects `::` fan-out child ids (name the
  top-level parent). v1: like `--resume`, subgraph inner nodes are not
  individually addressable as the entry.
- **CLI backend kills the whole process group on timeout** — the CLI backend
  spawns `claude -p` with `start_new_session=True` (own process group) and, on
  timeout/cancel, SIGTERM→SIGKILLs the process GROUP
  (`runtime/executor/backends/cli.py::_kill_process_group`), so descendants
  (MCP servers, test runners, headless browsers) are reaped too. Unlike ACP —
  which `/proc`-walks because its descendants reparent to PID 1 after the
  adapter's graceful `__aexit__` — the CLI parent is still alive (the group
  leader) when we signal, so a single `killpg` reaches the whole tree; no
  `/proc` walk needed.
- **Hyphenated node IDs in Jinja templates** — `{{research-phase}}`
  parses as subtraction; use underscores. `validate`/`run` emit an
  advisory warning (`compile/lint.py`).
- **`graph` doesn't draw route edges** — `graph` renders the static
  compiled LangGraph only; inline-route targets are runtime
  `Command(goto=...)`, so a routed node's branch targets appear
  edge-less there. `view` reconstructs declared `route:` edges and
  fan-out (hexagon) structure from the schema
  (`cli/view.py::_route_targets`), so it *does* draw them — only
  realized per-manifest fan-out children (created at run time) are absent.
- **Remote script/binary execution not wired** — `http(s)://` urls
  fetch-and-run for *prompt* nodes only; `_dispatch_script` /
  `_dispatch_binary` require `file://` and halt on a remote scheme.
  `allow_remote_scripts` gates the fetch in preparation only.
- **Per-model backpressure under downgrade** — Foreman holds the
  semaphore for the node's *original* model; an `OverloadError`
  opus→sonnet downgrade mid-call does not acquire the sonnet semaphore.
  Intent, not enforcement under downgrade.
- **Worktree cleanup is opt-in** — `settings.worktree_gc: on_success`
  reclaims end-of-run on a clean exit; default `never` keeps trees under
  `<workdir>/.sqrlly/` for inspection and `--resume`. `git worktree
  remove <path>` always works manually.
- **`--resume` skips completed nodes (0.6.0)** — bare `--resume` reseeds
  the prior checkpoint and skips nodes that completed cleanly and aren't
  downstream of a failure (`compile/resume.py::compute_skip_set` → the
  `_resume_skip` frozen snapshot channel; guards in `compile/nodes.py` /
  `compile/dynamic.py` read it, never the live `completed_nodes`).
  `--rerun-all` forces the pre-0.6 full replay; `--resume-from <node>`
  re-runs a node + its downstream. For a fan-out parent target: only
  non-completed children re-run (completed siblings are frozen, not
  re-billed); failed children always re-run regardless. **v1 limitation:**
  subgraph *inner*
  nodes aren't individually skippable — a subgraph re-runs in full unless
  its reference node completed cleanly.
- **Subgraph event prefix is one level** — child events are prefixed
  `parent::child` (immediate parent only), so child ids must be unique
  across sibling subgraphs to avoid log collisions.
- **Checkpointer migration** — pre-refactor `.sqrlly-state.json` is
  ignored on `--resume`; re-run from scratch.
- **ACP soak under load** — process-tree cleanup is fixed for the test
  scenario, but a multi-hour run with `max_parallel_jobs > 1` is
  unvalidated end-to-end (TODO 49).
- **`_route_sender` is last-write-wins** — a node reachable by a static
  `depends_on` edge AFTER an inline-route hop elsewhere can see a stale
  `{{sender_id}}`; guard with `{% if sender_id %}` in that rare
  topology. Inside a goto-only target the var is always bound.
- **Fan-out branch promotion is `fan_out.promote`, and external promote needs `worktree_gc: never`** — `fan_out.promote: true` merges each Send branch's worktree delta back to base at end-of-run via the shared `reconcile_promotions` path (before GC, so no race for the in-CLI path). A promote done OUTSIDE the run (a runner script after `sqrlly run` returns) must set `settings.worktree_gc: never`: with `on_success`, `reclaim()` `git worktree remove`s the branch trees before the process exits, so an external promote finds nothing and silently no-ops (silent data loss).
- **`promote_include` re-includes after `promote_exclude`** — `settings.promote_include` (git pathspecs) is a second `discover_changes` pass unioned back into every promoting node's footprint, so you can exclude a directory but keep a subpath (`promote_exclude: ["log/"]` + `promote_include: ["log/phases/**"]`). It overrides `promote_exclude` and ignores a node's `output_contract` globs (footprint-level override).
- **Worktree dep sharing** — gitignored base deps reach branch worktrees two ways: `settings.worktree_share` (read-only whole-dir symlink — for read-only gates) and `settings.worktree_setup` (per-worktree rehydrate commands; sentinel-gated, fatal-per-branch). Both write the shared paths to the repo's shared `.git/info/exclude` so they stay out of the promote footprint; `settings.promote_exclude` is the promote-layer backstop. Wired in `runtime/foreman.py::_ensure_worktree_ready` via `runtime/worktree_share.py`. Consumer note: a `prisma generate` in `worktree_setup` needs its output path in `worktree_setup_exclude` (a `validate` lint warns otherwise).
  - **Shared `info/exclude`, not per-worktree** — git has no per-worktree exclude file: `git rev-parse --git-path info/exclude` resolves to the common git dir, the same `.git/info/exclude` for the base repo and every linked worktree, so `write_worktree_excludes` mutates that one shared file. Consequence: those entries persist in the base repo after the run and are NOT reclaimed by `worktree_gc`; however `info/exclude` only affects UNTRACKED paths, so it can never mask modifications to tracked files.
- **Per-fan-out worktree override** — `fan_out.template.worktree` (`auto`/`isolated`/`off`, default inherit) overrides `settings.worktree` for one fan-out's branches, so a single workflow can mix an isolated build fan-out with a shared-base planner fan-out. Threaded two ways to match the two fan-out execution paths: subgraph templates gate isolation in `compile/subgraph.py::make_fan_out_subgraph_invoker` (new `template_worktree` arg overrides the `_isolate` computation); non-subgraph (`.md`/`.py`/script) templates set the synthetic child node's `worktree` in `compile/dynamic.py::_make_fan_out_node` so `effective_worktree` resolves it at the foreman gate. `off` runs branches in the base workdir (writes visible to a join node).
- **Fan-out child-id collisions fail loud** — a dict manifest item missing `id` WARNs in `compile/_manifest.py::_normalize_items` (every id-less item collapses onto `<parent>::unknown`), and the Send router (`compile/graph.py::_make_dynamic_router`) raises `ManifestError` on any DUPLICATE child id before dispatch — catching both literal duplicate `id`s and the `::unknown` collapse before it becomes a silent N→1 fan-out.
- **Backend transient-error retry is opt-in** — `settings.backend_max_retries`
  (default `0`) retries the SAME backend dispatch on a non-`OverloadError`
  exception (a `claude exited 1` blip) up to N times with `retry_backoff`
  between attempts, wrapped around the `OverloadError`→downgrade loop in
  `runtime/executor/prompt.py::execute_rendered`. Distinct from the
  gate/evaluation `max_retries`; overload still flows through the
  model-downgrade chain (not double-counted). `0` = terminal on first
  backend error (the historical behavior).
- **`settings:` rejects unknown keys (`extra=forbid`, 0.7.6)** — a typo'd or stale `settings` key now raises a `ValidationError` at load (previously silently ignored). Every other schema model already enforced this; `settings` was the last to adopt it. Run `sqrlly validate` to surface the offending key.
- **`--resume` re-runs only failed fan-out children** — a fan-out parent
  with a failed child in the prior checkpoint is dirtied in
  `compile/resume.py::compute_skip_set` (the failed child's `<parent>::<item>`
  id is mapped back to its parent and seeded into the dirty frontier), so the
  parent re-fans; the per-child gate in `compile/dynamic.py::_make_fan_out_node`
  freezes a child on `child_id in _resume_skip` alone, so completed siblings
  are NOT re-billed and only the formerly-failed child (never in the frozen
  snapshot) re-runs. Stable-id-safe: a re-fan that drifts the manifest yields
  new ids absent from the snapshot.

## Environment quirks

- **Python 3.14**: `pydantic.v1` deprecation warning from
  `langchain_core` is harmless. The system runs on 3.14.2 in this
  env; `python` binary is unavailable — use `sys.executable` for
  subprocess calls in tests.
- **ACP `aclose()` warning** on Python 3.14 — async-generator cleanup
  warning from the SDK. Harmless; the new `close()` reaps the
  process tree explicitly via `/proc` walk.
- **`tests/__init__.py` must NOT exist** — its presence breaks
  `from helpers import ...` and `from mock_executor import ...` in
  tests. Removing it is the supported state.
- **Secret resolution** — `runtime/secrets.py::resolve_secret` layers
  workflow YAML setting → `os.environ[name]` → project-local `.env`
  (walking up from CWD); never machine-global keystores. In-tree the
  `.env` layer is used by `url.py::_expand_vars` (header `${VAR}`
  expansion); `resolve_secret` itself remains for workflow-defined keys
  (e.g. a script node hitting a third-party service).
- **`pyproject.toml`** marker for ACP tests: `acp` (used in
  `pytest -m`).

## Quick references

| Task | Where to look |
|---|---|
| New schema field | `src/sqrlly/schema/models.py` (mind layer rules) |
| New backend | `src/sqrlly/runtime/executor/backends/` + factory case |
| New CLI flag | `src/sqrlly/cli/main.py` `@click.option` decorators |
| New node type | `src/sqrlly/compile/graph.py` (registration) + `src/sqrlly/compile/nodes.py` (factory) |
| New gate validator shape | `src/sqrlly/runtime/gates.py::_parse_evaluation_output` |
| New WorkflowState field | `src/sqrlly/runtime/state.py` (TypedDict + REDUCERS) — beware of parity invariant in `compile/dynamic.py::_merge_updates` |
| Inline routing (route block, sender bindings, include_eval preamble) | `src/sqrlly/schema/models.py::Route`, `src/sqrlly/compile/graph.py::_make_inline_route_node`, `src/sqrlly/runtime/gates.py::build_eval_preamble` |
| Layer rule violation | `tests/architecture/test_layers.py` errors point to the offending file |
| Transient-error → `OverloadError` mapping | `src/sqrlly/runtime/executor/backends/_overload.py::maybe_raise_overload` (status-code set + `ACP_OVERLOAD_SUBSTRINGS`) |

When in doubt before changing compile or runtime layer code, re-read the
"Known limitations" section above and the `tests/architecture/test_layers.py`
rules — several non-obvious invariants there bite contributors who
haven't seen them before.
