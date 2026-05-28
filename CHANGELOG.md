# Changelog

All notable changes to sqrlly are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] — add `transport: cli` as a peer to `transport: acp`

### Added

- **`transport: "cli"`** — subprocess-per-call LLM backend invoking
  `claude -p --model <model>` per `send_prompt`. Prompt piped on
  stdin, stdout captured as response, exit code surfaces as
  `RuntimeError` (or `OverloadError` on 529 / "overload" stderr).
  Single shared file:
  `src/sqrlly/runtime/executor/backends/cli.py`. Provider remains
  `anthropic`; additional providers (codex, gemini, custom) are
  tracked as WISHLIST 36.
- **Factory dispatch row** for `("cli", "anthropic")` →
  `CLIBackend(argv_prefix=("claude", "-p"))`.
- **`LlmPreset.transport`** literal re-broadened to
  `Literal["acp", "cli"]`. Coexistence is real: a single workflow
  may declare both transports side-by-side; only the schema's
  "exactly one default" rule constrains the pair.
- **Tests** — `tests/unit/runtime/test_cli_backend.py` (11 cases,
  real shell-script fakes), schema regression for
  `transport: cli`, factory dispatch row, full E2E under
  `tests/cli/test_cli_backend.py` (new `cli` marker; pre-flighted in
  `tests/conftest.py`), restored
  `tests/e2e/test_live_backend_roundtrip.py` as cli-only.
- **Examples dogfood** — `examples/jokes/workflow.yaml` migrated to
  `transport: cli` as the new canonical quickstart. Other examples
  (e.g. `absurd-paper/subgraphs/*`) stay on `transport: acp` so
  coexistence is visible in the repo.

### Unchanged

- `transport: acp` is fully supported with no deprecation: existing
  workflows keep working, the ACP backend, its process-tree cleanup,
  and its session-warm semantics are untouched. The investigation
  (`docs/investigations/transport-context-parallelism.md`) recommended
  Path A (CLI replaces ACP) on cost grounds, but this 0.3.0 lands cli
  additively first so users can dogfood before a deprecation decision.

## [0.2.0] — strip `transport: api` (ACP-only release)

Consolidates around a single LLM transport while the project explores
adding `transport: cli` (subprocess-invoked `claude -p` / `codex` /
`gemini`). Direct-API backends are gone for now.

### Removed

- **`transport: "api"`** and the four api providers (`anthropic`,
  `openai`, `deepseek`, `custom`). The `LlmPreset.transport` literal
  is now `Literal["acp"]` and `LlmPreset.provider` is
  `Literal["anthropic"]`. `LlmPreset.api_base_url` deleted.
- **Backend modules**: `runtime/executor/backends/anthropic.py`,
  `openai.py`, and `_lazy_client.py` deleted. `_overload.py` keeps
  `ACP_OVERLOAD_SUBSTRINGS` + `maybe_raise_overload`; the
  `ANTHROPIC_OVERLOAD_NAMES` / `OPENAI_OVERLOAD_NAMES` frozensets are
  gone.
- **`pyproject.toml` extras**: `[anthropic]` and `[openai]` removed.
  `[acp]` is the only LLM-backend extra now.
- **Auto-detect** trimmed to the `npx`-on-`PATH` branch only;
  `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` no longer synthesize a
  default preset.
- **Tests**: `tests/unit/runtime/test_anthropic_backend.py`,
  `test_openai_backend.py`, `test_factory.py`, and the four-backend
  `tests/e2e/test_live_backend_roundtrip.py` deleted. Test count
  drops from 918 to 877.
- **`.env.example`**: the four API-key blocks (`ANTHROPIC_API_KEY`,
  `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `CUSTOM_API_KEY` +
  `CUSTOM_API_BASE_URL`) removed. ACP inherits the local `claude` CLI
  session — no env vars needed.

### Changed

- **Migrators refuse non-acp legacy executors**:
  `scripts/migrate_legacy_executor_to_presets.py` and the internal
  `cli/migrate.py` raise `MigrateError` rather than emit
  `transport: api`. The "no executor declared" default migrates to
  `transport: acp` (matching runtime auto-detect).
- **Example workflows** (`absurd-paper/subgraphs/*.yaml`) updated to
  `transport: acp`.
- **Docs** — README, SKILLS, schema-reference, CLAUDE, TECHNICAL
  refreshed for the narrower transport surface; the strip is flagged
  as consolidation pending a `transport: cli` follow-up.

### Why

The api transport was functional but narrow: text-in/text-out with no
filesystem or tool access. Producer-shape workflow nodes (the design
center of gravity) silently degraded under api presets — the agent
produced text describing what it would have done, not the artifact.
Until structured-return via output contracts lands, that mismatch
costs more in user-confusion than the cheap-classifier use case
gains. The full pre-strip patch lives on
`experiment/strip-api-transport` and the audit + design notes are in
[WISHLIST.md][wl35] (item 35) for re-introduction once
`transport: cli` provides the agent-shaped alternative.

[wl35]: https://github.com/christopherseaman/sqrlly/blob/main/WISHLIST.md

## [Unreleased] — command presets (named script interpreters)

Extends the preset concept beyond LLM config: a script node can now
run under a named interpreter/command instead of the hardwired
extension map (`.py` → `python3`).

### Added

- **`CommandPreset`** (`kind: command`) — a named command string for
  script dispatch. `settings.presets` is now a `kind`-discriminated
  union of `LlmPreset` (the prior shape) and `CommandPreset`:
  ```yaml
  settings:
    presets:
      uv: { kind: command, command: "uv run --no-project" }
  nodes:
    - id: build
      execute:
        url: scripts/build.py
        params: { preset: uv }      # → `uv run --no-project scripts/build.py`
  ```
- The Preset union uses a **callable discriminator** that defaults
  absent `kind` to `"llm"` — all existing kind-less preset YAML keeps
  parsing, zero migration.
- **`SubprocessParams.preset`** — script nodes carry the command-preset
  reference.
- Command assembly supports `{{file}}` / `{{args}}` placeholder tokens
  (token-level, post-`shlex.split`): `command: "pytest {{args}} {{file}}"`
  places them explicitly; absent placeholders fall back to default
  append (command + path + args).
- `examples/command_preset/` — a workflow demonstrating `uv run`.

### Changed

- `Settings._validate_default_preset` scoped to `LlmPreset`s — exactly
  one `default: true` LLM preset when any exist; command presets have
  no `default` (script nodes opt in by name, no-preset scripts use the
  extension map).
- `Graph._validate_preset_refs` rejects a command-preset reference
  combined with `execute.mode` — mutually exclusive (the command
  preset already specifies the interpreter).
- `prompt.resolve_model` returns `None` for command-preset / no-LLM-
  model nodes, so foreman's per-model semaphore selection cleanly
  skips script nodes.

## [Unreleased] — preset rework review follow-ups

A three-agent code review of the preset rework (bugs / YAGNI-KISS-DRY /
off-the-shelf-reuse) surfaced findings; this is the cleanup pass.

### Fixed

- **Migrator silently dropped `Node.model` on gate-only nodes** — a
  node with a `model:` override but no `execute:` block lost the
  override without indication. The migrator now emits an explicit
  change entry naming the dropped field + value + what the gate uses
  going forward.
- **`params.preset` typos crashed at runtime, not validate time** —
  new `Graph._validate_preset_refs` model validator rejects any
  `params.preset:` reference not declared in `settings.presets` at
  parse time, instead of a `RuntimeError` on first node execution.

### Changed

- **`Preset.base_url` renamed to `Preset.api_base_url`** — disambiguates
  from `Settings.base_url`, which serves an unrelated purpose (resolving
  relative `execute.url` paths to local files).
- **`DispatchExecutor` ctor takes only `prompt_backends` (dict)** — the
  singular `prompt_backend=` convenience arg + its synthetic `_legacy`
  registry key are gone. Three downstream methods no longer branch on
  the magic key. Tests + `examples/jokes/run.py` construct an explicit
  `prompt_backends={"default": backend}` registry.
- **`create_backend_from_preset` uses a `(transport, provider)` dispatch
  table** instead of a 5-branch if/elif chain. Adding a transport is one
  row + one builder function.

### Removed

- **`factory.create_prompt_backend` and `factory.auto_detect_executor`**
  — dead code after the preset cutover (zero production callers).
- **The legacy executor → presets migration moved out of the CLI** —
  `_migrate_legacy_executor_to_presets` and helpers are now a standalone
  one-shot script at `scripts/migrate_legacy_executor_to_presets.py`
  (PEP-723 metadata, run via `uv run`). The longer-lived base migrator
  in `sqrlly.cli.migrate` keeps the phases→nodes / quality_gate→evaluation
  transforms.

### Internal

- `prompt.resolve_model` import hoisted to module top (the "avoid cycle"
  late-import guarded against a cycle that didn't exist).
- `build_preset_registry` passthrough returns `dict(settings.presets)`
  instead of a per-element `model_copy()` — Preset instances aren't
  mutated downstream.
- `cli/migrate.py` collector + rewriter share one `_walk_model_holders`
  iterator instead of two parallel `isinstance` cascades over the same
  fan_out topology (this helper moved to the standalone script with the
  rest of the preset migrator).

## [Unreleased] — named-preset rework (executor: → settings.presets:)

Three-commit migration replacing the collapsed ``executor:`` enum
with named presets. Authors declare workflow-level bundles of
``(transport, provider, model[, base_url])`` and nodes opt into a
specific one via ``params.preset:``; otherwise the preset marked
``default: true`` applies. CLI ``--preset/-p`` forces a specific
preset at run time.

### Added

- **``Settings.presets: dict[str, Preset]``** — workflow-level named
  execution bundles. Exactly one preset must be marked
  ``default: true`` when the dict is non-empty (schema validator).
- **``Preset``** Pydantic model (``transport`` ∈ {api, acp};
  ``provider`` ∈ {anthropic, openai, deepseek, custom};
  ``model``; ``base_url`` for ``api+custom``; ``default``).
  Combination validators reject impossible shapes
  (``transport=acp`` requires ``provider=anthropic``; ``base_url``
  only meaningful for ``api+custom``).
- **``PromptParams.preset: str | None``** — per-node preset
  reference. The migrator rewrites legacy ``Node.model:`` and
  ``params.model:`` into auto-named presets + this reference.
- **``runtime/executor/preset.py``** — ``resolve_preset_name``,
  ``auto_detect_default_preset``, ``build_preset_registry``.
- **``factory.create_backend_from_preset(preset)``** — instantiates
  the right ``PromptBackend`` from a ``Preset`` instance.
- **``DispatchExecutor.__init__(prompt_backends=...)``** — new
  preset-name → backend dict argument (mutually exclusive with the
  legacy single-backend ``prompt_backend=``). Resolves per-node via
  ``_resolve_prompt_executor``.
- **CLI ``--preset/-p``** — forces a specific preset as the default
  for this run. Errors if the named preset isn't declared.

### Removed

- **``Settings.executor``** and **``Settings.default_model``** —
  replaced by ``Settings.presets``.
- **``Node.model``** — per-node model override now goes through
  ``params.preset`` referencing a preset.
- **``PromptParams.model``** — same.
- **``--executor/-e``** and **``--model/-m``** CLI flags on
  ``sqrlly run`` — replaced by ``--preset/-p``.
- **``sqrlly migrate`` CLI subcommand** — the ``migrate.py`` module
  remains as an internal utility (used during this rework) but is
  no longer exposed as a user-facing command.

### Changed

- ``runtime/executor/prompt.resolve_model`` — now consults the
  resolved preset's model. Returns ``None`` when ``settings.presets``
  is empty so the foreman's per-model semaphore selection cleanly
  skips for pure-script workflows.
- ``cli/main._execute_workflow`` — branches on
  ``settings.presets`` (now always populated after
  ``build_preset_registry`` synthesizes an auto-detect default when
  YAML didn't declare any).
- Subgraph settings inheritance: ``model_fields_set`` already
  handles ``presets:`` correctly. Subgraph without ``presets:`` →
  inherits parent's; subgraph with ``presets:`` → wholly replaces
  (key-wise additive merge is a deferred refinement).
- All ``examples/`` workflows migrated to the new shape via the
  internal migrator. Author-visible diff: ``executor: acp +
  default_model: sonnet`` → ``presets: {default: {transport: acp,
  provider: anthropic, model: sonnet, default: true}}``.

### Migration

- One-time migration runs internally:
  ```python
  from sqrlly.cli.migrate import migrate_file
  migrate_file(Path("workflow.yaml"), in_place=True)
  ```
- The migrator detects prompt-mode dispatch in nodes and
  synthesizes ``settings.presets`` with the legacy executor
  mapped to ``(transport, provider)``. For each unique model used
  via ``default_model`` / ``Node.model`` / ``params.model``, a
  preset is created (``default`` for ``default_model``;
  ``_auto_<sanitized>`` for others). Node-level overrides are
  rewritten as ``params.preset:`` references.
- Pure-script workflows (no prompt-mode nodes) just have their
  vestigial ``default_model``/``executor`` keys dropped — no
  presets are added (auto-detect at runtime is enough).

### Testing

- 914 tests passing, 2 skipped. New tests in
  ``tests/unit/runtime/test_preset.py`` (20) and
  ``tests/unit/runtime/test_dispatch_presets.py`` (14).
- The legacy ``sqrlly migrate`` subcommand tests in
  ``test_migrate.py`` were dropped; the underlying ``migrate.py``
  module tests stay.

## [Unreleased] — set-union reducer for completed_nodes / failed_nodes

### Changed (state shape — breaking for state-introspecting consumers)

- **`WorkflowState.completed_nodes` and `WorkflowState.failed_nodes`
  are now `set[str]`** (were `list[str]`), with `_merge_sets`
  (set-union) replacing `operator.add` as their reducer.

  Structural consequences:
  - Duplicate accumulation is impossible. A goto-driven re-fire
    (`Command(goto=X)` re-entering a node already in `completed_nodes`)
    no longer produces `["X", "X"]`; the merged value stays `{"X"}`.
  - O(1) `in` membership checks (vs. prior O(n) on lists). Affects
    every guard in `compile/nodes.py`, `compile/dynamic.py`,
    `compile/graph.py`, `compile/subgraph.py` — same code, faster.
  - Iteration order is no longer deterministic. CLI summary lines
    (`Nodes: <list>` / `Failed: <list>`) now sort the output for
    stable display.

  Resolves the structural cause of WISHLIST #32 (post-Phase-B audit).

- **`make_initial_state` defaults**: `completed_nodes=set()`,
  `failed_nodes=set()` (were `[]`).

### Migration

- Source code: 9 sites that emitted `{"completed_nodes": [node_id]}`
  style list literals migrated to `{"completed_nodes": {node_id}}`
  set literals. Inline retry loop in `compile/dynamic.py` uses
  `update.setdefault("completed_nodes", set()).add(child_id)` instead
  of `.append`.
- Tests: ~30 assertions updated from `== ["p1"]` to `== {"p1"}`. The
  resume regression test (`test_resume_fan_out.py`) lost its
  `.count("a") == 2` assertion — duplicates can no longer exist in
  state; the runs-counter side channel still pins the bug. The wave-
  pattern test (`test_wave_pattern.py`) dropped its dispatcher-fired-
  count assertion for the same reason; the `dispatcher::q_gamma`
  presence assertion is now the load-bearing regression check.
- JSONL event log: unchanged. Events fire per super-step, not per
  state entry — `node_completed` events still emit per fire, so
  consumers that care about exact fire counts can count log events.

### Not addressed (deferred)

- WISHLIST #31 (`--resume` discards checkpointer) remains open. The
  set-union reducer masks the *visible symptom* of duplicate
  accumulation on resume, but the resume logic still deletes the
  thread and replays — body bodies still re-execute. The proper fix
  requires picking one of three API shapes documented in WISHLIST
  #26 (skip-completed-via-prior-run channel, `--resume-from <node>`,
  or JSONL-driven skip).

## [Unreleased] — terminology cleanup: subphase → branch

### Changed (breaking — template vars)

- **User-facing fan-out aggregate template vars renamed**:
  - `{{<dep>}}_subphases` → `{{<dep>}}_branches`
  - `{{<dep>}}_subphase_worktrees` → `{{<dep>}}_branch_worktrees`

  Workflows referencing these in prompt templates must update by hand
  (no shim — sqrlly is pre-1.0 and the user base is internal).
  Completes the rename pass that previously retained these vars for
  compatibility.

### Changed (internal)

- LangGraph node display name for fan-out branch bodies:
  `subphase_<parent>` → `branch_<parent>`. Visible only in graph
  debug output, not API.
- Test names, comments, and example prompts updated to use "branch" /
  "fan-out branch" instead of "subphase". The `cli/migrate.py` legacy
  YAML migrator continues to handle the historical `dynamic_subphases:`
  key — that's deliberately frozen vocabulary.

### Removed

- `compile/graph.py::_subphase_id_resolver` — dead helper with zero
  callers. The pattern actually used is the `node_id_resolver`
  parameter on `_make_evaluation_node` / `_make_decision_node`, set
  via inline lambdas at the call sites.

## [Unreleased] — Stage 5d: split Evaluation from Decision + gate dep-outputs

### Added

- **`_make_decision_node`** in `compile/nodes.py` — second half of a
  gated node pair. Reads the latest EvaluationRecord from
  `state.evaluations[node_id]`, classifies via the existing
  `classify_evaluation_outcome` helper, and returns
  `Command(update={completed/failed/retries/errors}, goto=target)`.
  Mirrors the inline-route node pattern. Top-level gated wiring
  becomes plain edges only: `exec → _eval_<id> → _decide_<id>`.
- **Gate validators see dep outputs** (WISHLIST 216). Script gates
  receive `DEPS_JSON` / `DEPS_STRUCTURED_JSON` / `DEPS_WORKTREES_JSON`
  env vars. LLM gate templates bind each dep by id directly
  (`{{research}}`) plus `{{_deps}}` aggregate — same shape as
  `build_context` for executor templates. Dep scoping mirrors
  `build_context`: declared `depends_on` outputs only; gate-only
  phases see all completed outputs.

### Changed

- **`_make_evaluation_node` body now writes only the
  EvaluationRecord** to `state.evaluations[node_id]`. Outcome
  classification (pass/retry/fail/warn-continue) and state writes
  (`completed_nodes`/`failed_nodes`/`retries`/`errors`) moved to
  `_make_decision_node`. Subphase inline retry loops in
  `compile/dynamic.py::_make_fan_out_node` unchanged.
- **`_make_evaluation_router` deleted** and `add_conditional_edges`
  for the eval pair removed. Routing destinations are no longer
  visible in `compiled.get_graph().edges` for gated nodes — they're
  runtime `Command(goto=...)` returns from the Decision node.
- **Dynamic gated parents keep the combined factory**
  (`_make_combined_eval_decide_node`) — their downstream is the
  manifest-dispatching dynamic router, which still needs
  `completed_nodes`/`failed_nodes` already-in-state. Documented
  inline as a deliberate deferral.

### Migration

- Authors: no YAML changes. Topology equivalence at the goto
  level (retry → exec_id, fail → END, pass → pass_targets).
  Same gate semantics, same retry budgets, same outcome states.
- Tooling that introspects `compiled.get_graph()` to learn gate
  routing destinations: those edges are now runtime, not static.
  Inspect `_decide_<id>` node body or the YAML evaluation block
  instead.
- Tests asserting on eval-node outcome state writes
  (`update["completed_nodes"]`, etc.) need to either compose
  eval+decide or move to `test_decision_node.py`.

## [Unreleased] — `view` HTML viewer + runner None-update guard

### Added

- **`sqrlly view <yaml> [--log <jsonl>]`** — self-contained HTML
  viewer for workflows. Authoring mode (no log): topology + per-node
  config inspector. Debug mode (with log): adds status overlay
  (passed/failed/retried/untouched), per-node `fired N×` chip for
  goto re-fires, retry chip, last-error display, full event slice on
  click. Custom Mermaid emission (not `compiled.get_graph().
  draw_mermaid()`) gives layout control + author-perspective output
  (skips synthetic `_eval_<id>` and `_route_<id>` nodes); Mermaid
  loaded via CDN with raw-source fallback if blocked. Layout uses an
  invisible-spine subgraph block for predictable direction;
  `--direction TB|LR|BT|RL` flag (default TB). Output defaults to
  `<workdir>/sqrlly-view.html`.

### Fixed

- **Runner crash on Command-only nodes when `--log` is set.** Route
  dispatchers that emit `Command(goto=...)` without a state update
  appear in the LangGraph updates stream as `{node_name: None}`;
  `JsonlLogger.log_update(None)` was crashing with `AttributeError:
  'NoneType' object has no attribute 'get'`. Guard added; regression
  test in `test_logging.py::TestLogUpdate::test_no_events_on_none_update`.

## [Unreleased] — Native event stream + Anthropic backend + StubBackend removal + wave-driven re-execution + Foreman memory back-pressure

### Added

- **`AnthropicBackend`** (`runtime/executor/backends/anthropic.py`) —
  fourth `PromptBackend` after `acp` / `openai` / `deepseek`. Talks
  to Anthropic's Messages API directly via `AsyncAnthropic`. Generic
  model alias table (`sonnet` / `opus` / `haiku` → vendor IDs) with
  pass-through for explicit model pins. `OverloadError` mapping for
  transient failures (status 429 / 502 / 503 / 504 / 529 + class-name
  fallback) so the model-downgrade chain activates as designed.
  Optional dependency `anthropic = ["anthropic>=0.40"]`; install with
  `uv sync --extra anthropic`.
- **CLI `--executor anthropic`** and **YAML `settings.executor:
  anthropic`** select the new backend explicitly. `_resolve_anthropic_key()`
  reads `ANTHROPIC_API_KEY` first, falling back to
  `~/.pi/agent/auth.json` carrying `{"anthropic": {"key": "..."}}`.
- **Memory back-pressure in Foreman**
  (`runtime/foreman.py::ForemanExecutor._wait_for_memory`). Two
  complementary gates, AND-composed:
    - `settings.memory_threshold_pct` (float | None) — blocks new
      dispatches while `psutil.virtual_memory().percent` is above
      the threshold.
    - `settings.memory_min_available_bytes` (int | str | None) —
      blocks new dispatches while available bytes are below the
      threshold. Accepts raw bytes (`4_294_967_296`) or a string
      with a binary-multiplier suffix (`"4GB"`, `"500MiB"`, `"2T"`).
      Case-insensitive; `KB = 1024` (binary semantics, matches
      `free -h` / `psutil`).
  Both compose (AND) with `max_parallel_jobs` and `per_model_limits`;
  in-flight jobs are never aborted by the gates. `None` (the default
  for both) disables the checks. ``psutil>=5.9`` added as a core dep.
- **`examples/wave_planner/`** — shipped example + e2e test
  (`tests/e2e/test_wave_pattern.py`) demonstrating the wave-driven
  dynamic-task pattern. Pure-Python deterministic stubs (no backend
  key required). Runs from a non-git workdir to share `state.json`
  across waves without git-worktree isolation; the README explains
  how to thread state through `node_outputs` for worktree-isolated
  workflows. Folds in a documented use of `memory_threshold_pct` in
  the workflow's `settings:` block.
- **Wave-driven dynamic-task re-execution**: dropping the
  pre-LangGraph-checkpointer `# Skip if already completed (resume
  mode)` guards (3 sites in `compile/nodes.py` and `compile/dynamic.py`)
  unblocks fan-out parents being re-entered via inline-route
  `Command(goto=...)`. A node reached via goto now executes its body
  again — idempotence is the procedure's responsibility, not the
  framework's. The `failed_nodes` checks are kept (those guard
  re-attempts of hard-failed nodes; semantically distinct).

### Changed

- **`auto_detect_executor()` now picks Anthropic first.** Resolution
  order is: Anthropic key → DeepSeek key → `npx` (ACP). Users with
  both Anthropic and DeepSeek keys configured will see workloads
  routed to Anthropic by default; pin explicitly with `-e deepseek`
  to opt out. Mixed-tier authoring (Claude for some nodes, DeepSeek
  for others) still works through the per-node `model:` field plus
  `--executor` overrides.
- **Logging stream switched to `stream_mode=["updates", "values"]`**
  in `runtime/runner.py` and `compile/subgraph.py`. The 6 emitted
  event types (`workflow_start`, `workflow_end`, `node_completed`,
  `node_failed`, `gate_evaluated`, `node_retried`) and their schema
  are unchanged; the internal source is now LangGraph's native
  partial-update payloads instead of state diffs over successive
  cumulative snapshots. `JsonlLogger.log_snapshot(prev, curr)` was
  replaced with `log_update(node_name, update)`. The
  `SubgraphLogger` `parent::child` prefix mechanism still applies,
  via the new entry point.

### Removed

- **`FanOut.enabled`** — DELETED. The presence of the `fan_out:` block
  on a node IS the activation; the legacy bool flag was an author
  footgun (a present `fan_out: { template: ... }` block silently
  no-op'd when `enabled` defaulted to `false`). Schema gained
  `extra="forbid"` so legacy YAML carrying `enabled: true|false`
  fails at `validate` time with a clear ValidationError.
  **Breaking change**: author migration is "delete the line"
  (`enabled: true` was redundant; `enabled: false` was the silent
  footgun this removal fixes).
- **`StubBackend`** (`runtime/executor/backends/stub.py`) — DELETED.
  The deterministic `[prompt-stub] model=X prompt_length=N`
  placeholder previously wired as the auto-detect last-resort
  fallback was the same antipattern `feedback_no_fake_backends.md`
  forbids in tests, just shipped in production. **Breaking
  changes:**
  - `--executor stub` and `settings.executor: stub` are no longer
    accepted; `create_prompt_backend("stub")` raises `ValueError`
    listing the supported types.
  - `auto_detect_executor()` raises `RuntimeError` with concrete
    remediation when no backend resolves (set `ANTHROPIC_API_KEY`,
    set `DEEPSEEK_API_KEY`, or install `npx +
    @zed-industries/claude-code-acp`).
  - `DispatchExecutor` constructed without a `prompt_backend` no
    longer emits a fake success for prompt URLs — it raises
    `RuntimeError` naming the offending node id.

### Migration notes

- If your YAML carried `executor: stub`, switch to
  `executor: acp` (no API key needed once `npx
  @zed-industries/claude-code-acp` is installed) or to
  `executor: anthropic` with `ANTHROPIC_API_KEY` set.
- If you embedded `DispatchExecutor(prompt_backend=StubBackend())`
  in a custom harness, instantiate a real backend via
  `create_prompt_backend("anthropic" | "deepseek" | "acp" |
  "openai", ...)` instead.
- If your YAML carried `fan_out: { enabled: true | false, ... }`,
  remove the `enabled` line. To disable fan-out, remove the entire
  `fan_out:` block instead.
- If your YAML carried `route: { else: <target> }` without a
  `cases:` list, switch to `route: { goto: <target> }` — the bare-
  `else:` form is now rejected at validate time as confusing
  redundant structure. (Multi-dim evaluations: no migration needed;
  the parser now derives `score = min(dim_scores)` automatically
  when the JSON omits a top-level `score` field — passing gates
  no longer surface as misleading `score=0.0` in JSONL events.)

## [Pre-Stage-5d] — Stage 5c: Inline Route

### Added

- **`Node.route: Route | None`** as a first-class block alongside
  `execute:` / `evaluation:` / `fan_out:`. Two shapes: `goto:` (str
  or list[str]) for unconditional dispatch, or `cases: + else:` for a
  first-match conditional ladder. List-valued goto fans out to all
  targets in the next super-step via `Command(goto=[...])`. Replaces
  the standalone `Execute.type="route"` form (removed; `migrate.py`
  auto-lifts legacy YAML).
- **Per-case `include_eval: bool = False`** flag (also on `goto:`
  shorthand and structured `else:`). When true, the goto target's
  prompt receives a neutral, structurally-formatted eval-result
  preamble auto-prepended before the rendered template body. The
  same builder serves same-node retries and `include_eval`-true gotos
  via `runtime/gates.py::build_eval_preamble` (no "failed" framing).
- **`EvaluationResult.reasons: dict[str, str]`** — gate parser
  captures `<dim>_reason` JSON fields as per-dimension string
  rationale, surfaced in the preamble alongside per-dimension scores.
- **Sender bindings in prompt context**: when an inline-route hop
  fires, the goto target's Jinja context binds `{{sender_id}}`,
  `{{sender}}`, `{{sender_structured}}`, `{{sender_worktree}}`
  (always-on identity vars). Eval feedback flows via the auto-
  prepended preamble, not via Jinja vars.
- **Cross-cutting `{{evals}}` global** in every prompt template —
  `evals[node_id]` returns the latest evaluation result dict.
- **Route namespace helpers** for `when:` predicates: `evals[id]`,
  `passed(id)`, `score(id)`, `scores(id)` close over state for
  ergonomic single-line predicate authoring.
- **State fields**: `_route_sender: NotRequired[str]` and
  `_route_eval_preamble: NotRequired[str]` on `WorkflowState`
  (last-write-wins via `_merge_updates`'s default; pattern parallels
  `_fan_out_item`).

### Changed

- **`inject_retry_reason`** delegates to `build_eval_preamble`. Retry
  preamble is now neutral ("Attempt N of M" footer instead of
  "Attempt N failed evaluation"); `blocking: false` settled scores
  below threshold are no longer framed as failures.
- **`Execute` shape simplified**: removed `type="route"`,
  `Execute.cases`, `Execute.else_`. Execute is now URL mode + join
  sentinel. Standalone routes live as `Node` with `route:` and no
  `execute:` block.

### Removed

- `Execute.type="route"` (hard cutover; `migrate.py` rewrites legacy
  YAML).
- `_make_route_node` / `_has_legacy_route` helpers in `compile/graph.py`.
- Route-as-execute escape in the runtime dispatcher.

### Migrated

- `examples/route_classify/workflow.yaml` (decide node → inline route).
- All route-using tests converted to `Route` + `Node.route`
  constructors. ~30 redundant test cases dropped (Execute.type=route
  is no longer constructible).
- `examples/pipeline_style/workflow.yaml` recreated as a 3-node linear
  chain in `route: { goto: ... }` style — replaces the
  reverted-and-superseded `next:` field experiment.

## [Unreleased] — Post-Stage-5b: defaults, scope, cleanup

### Added

- **OpenAI-compatible backend** (`runtime/executor/backends/openai.py`)
  via the `openai` SDK with an overridable `base_url`. Validated
  end-to-end against DeepSeek (`https://api.deepseek.com/v1`, model
  `deepseek-v4-flash`). Maps 429/502/503/504/529 plus
  `RateLimitError` / `APIConnectionError` to `OverloadError` so the
  existing model-downgrade chain in `PromptExecutor` activates the
  same way it does for ACP. Install with `uv sync --extra openai`.
- **Auto-detect executor backend** at CLI dispatch
  (`factory.auto_detect_executor()`). Resolution order: `ANTHROPIC_API_KEY`
  (placeholder) → DeepSeek key (env or `~/.pi/agent/auth.json`) →
  `npx` on PATH → `stub` with a `UserWarning` naming concrete
  remediation. CLI: `executor or settings.executor or auto_detect()`.
- **Scope-aware settings inheritance** for subgraphs
  (`runtime/settings_merge.merge_settings`). Subgraph `settings:`
  blocks now actually apply: child fields explicitly authored win,
  parent's flow through. `NodeExecutor.execute` Protocol gained
  `settings_override: Settings | None`; threaded through
  DispatchExecutor, ForemanExecutor, PromptExecutor at every read
  site. Compile layer: `build_workflow_graph(effective_settings=)`
  threads merged settings into per-node closures.
- **ACP process-tree teardown** in `ACPBackend.close()`. Captures
  descendant PIDs from `/proc` *before* `__aexit__` so re-parented
  orphans stay tracked, then SIGTERM → 0.5s grace → SIGKILLs each.
  Test (`tests/acp/test_acp_cleanup.py`) observes a 15-PID descendant
  tree disappear within 3s of close. Soak-test under load still
  pending before the WISHLIST item fully closes.

### Changed

- **`Settings.executor` is now `str | None = None`** (was `"stub"`).
  `None` triggers auto-detect at CLI dispatch. Explicit
  `executor: stub` in YAML or `--executor stub` on the CLI still
  works — just no longer the default. Workflows that rely on the
  old default behavior need to either set `executor: stub` or rely
  on auto-detect picking the right real backend.

### Deprecated

(none)

### Removed

(none)

### Fixed

- `compile_fn` recursive call in `build_workflow_graph` was
  forwarding the OUTER scope's settings into nested subgraph
  builds instead of the inner scope's. Caught by the artifact-driven
  `default_timeout` inheritance test. Fix: the closure forwards
  its own `effective_settings` parameter.
- Stage 5b cutover left two ACP tests using removed APIs
  (`Node(prompt_file=...)`, `run_evaluation_llm(gate=...)`); both
  migrated to current schema. ACP suite back to 14/14 green.
- `ACPBackend._collect_descendants` was defined twice (Phase 4
  Edit cruft, second definition silently shadowed the first).
  Deduped.
- `auto_detect_executor()` returned `"anthropic"` when
  `ANTHROPIC_API_KEY` was set, but `create_prompt_backend("anthropic")`
  raises (no native backend). A common dev env var actively broke
  the workflow. Branch removed; `ANTHROPIC_API_KEY` alone now falls
  through to whichever real backend is available.
- `OpenAIBackend.send_prompt` returned silent empty output when the
  API responded with no choices (refusal, content filter, upstream
  truncation). Now surfaces as `ExecutionResult(success=False,
  error=...)` so the orchestrator routes via failure semantics.
- CLI `--executor` help text was stale ("(stub, acp)"). Updated to
  list all four choices and document auto-detect order.

## [Unreleased] — Stage 5b: `execute: { url, params }` Schema Cutover

### ⚠️ Breaking changes

#### Schema (hard cutover; no aliases)

The eight execution shapes — `prompt_file:` shorthand, `execution: {
type: prompt | command | gate_only | join | route }`, top-level
`config:` + `inputs:` + `outputs:`, and
`fan_out.template.prompt_file` — collapse into a single
`execute: { url, params }` block on `Node`. The URL extension drives
dispatch.

| Pre-Stage-5b | Stage 5b |
|---|---|
| `prompt_file: x.md` | `execute: { url: x.md }` |
| `execution: { type: prompt, prompt_file: x.md }` | `execute: { url: x.md }` |
| `execution: { type: command, command: c, args: [...] }` | `execute: { url: <abs path>, params: { args: [...] } }` |
| `execution: { type: gate_only }` | omit `execute:` (gate-only-by-elision) |
| `execution: { type: join }` | `execute: { type: join }` |
| `execution: { type: route, cases, else }` | `execute: { type: route, cases, else }` |
| `config: x.yaml` + top-level `inputs:` / `outputs:` | `execute: { url: x.yaml, params: { inputs: ..., outputs: ... } }` |
| `fan_out.template.prompt_file: x.md` | `fan_out.template.execute: { url: x.md }` |

`Node.model_config = ConfigDict(extra="forbid")` makes pre-Stage-5b
YAML raise a clear ValidationError naming the offending key, instead of
silently dropping it.

The migrate tool (`sqrlly migrate`) now chains Stage 3 → 4 → 5b
transforms automatically. Idempotent on already-migrated YAML; round-
trip preserves comments, anchors, and `{{templated}}` strings.

#### Removed schema/runtime symbols

- `PromptExecution`, `CommandExecution`, `GateOnlyExecution`,
  `JoinExecution`, `RouteExecution`, the `Execution` discriminated
  union — all gone. Use `Execute` and dispatch by URL extension.
- `Node.prompt_file`, `Node.execution`, `Node.config`, `Node.inputs`,
  `Node.outputs` — all gone. Use `Node.execute`.
- `_normalize_prompt_shorthand` model_validator — no longer needed.
- `runtime/executor/command.py` (and `CommandExecutor`) — folded into
  `runtime/executor/dispatch.py::_run_subprocess`. Script + binary
  dispatch share a single subprocess runner.
- `PromptExecutor.execute(node, context)` — deleted. The orchestrator
  layer (`dispatch._dispatch_prompt`) reads the prompt file, applies
  preamble, renders Jinja, and calls the new
  `PromptExecutor.execute_rendered(rendered, model, workdir, timeout)`
  helper, which owns the overload→downgrade fallback loop.

### Added

#### `runtime/url.py` — URL resolution + remote fetch gates

- `resolve_url(url, base_url, workdir)` — three-rule resolver
  (explicit-protocol passthrough → absolute path → relative against
  base_url else workdir). Canonical form via `urllib.parse.urlsplit`
  reassembly + lowercase host.
- `fetch_url(resolved, settings, cache)` — validates against four gates
  (`allow_remote_urls`, `allowed_url_hosts`, `allow_remote_scripts`,
  `max_remote_fetch_bytes`), consults a per-compile `_RemoteFetchCache`,
  applies `url_headers` with `${VAR}` env expansion. Defaults reproduce
  a "local files only" policy.
- `RemoteURLBlockedError`, `RemoteURLFetchError` exceptions surface as
  node failures with clear messages — never silent fallbacks.

#### `schema/params.py` — per-mode Pydantic params

- `PromptParams(model?, agent?, timeout?)`,
  `SubgraphParams(inputs, outputs)`,
  `SubprocessParams(args, env)`. All set `extra="forbid"` so typos like
  `args:` on a prompt URL surface as ValidationError at schema parse
  time, not runtime.
- `params_for_url(resolved_url) -> type[BaseModel]` resolver picks the
  right dataclass by URL extension/scheme.

#### `Execute` schema model

- New `schema/models.py::Execute(BaseModel)` with `url`, `type`
  (`"join" | "route" | None`), `params`, `cases`, `else_` fields.
  Validator enforces exactly one of {url, type=join, type=route} active
  per node.

#### Settings: remote URL gates

- `base_url`, `allow_remote_urls`, `allow_remote_scripts`,
  `allowed_url_hosts`, `url_headers`, `max_remote_fetch_bytes` — see
  `CLAUDE.md`'s "Remote URL support" section.

#### Per-child subgraph fan-out

- `fan_out.template.execute.url` ending in `.yaml`/`.yml` runs the
  referenced subgraph **per Send branch**. Each manifest item drives
  one subgraph invocation; the subgraph's terminal output flows back
  as that branch's `child_outputs[parent::item_id]`. Cycle detection
  walks the URL-reference DAG at parent compile time.
- Demo: `examples/absurd-paper/subgraphs/single_review.yaml` — each
  reviewer in `reviewer_pool` runs draft → critique sequentially in
  its own Send branch.
- Closes the Stage 4 audit's deferred `FanOutTemplate.config:` carve.

### Changed

- The `evaluation:` block on a Node is the only way to attach gate logic
  (the alias `quality_gate:` was dropped in Stage 3b; Stage 5b doesn't
  touch this).
- Subgraph inputs/outputs now live under `execute.params.{inputs,outputs}`
  on the parent reference (no more top-level `inputs:` / `outputs:`).

### Documentation

- `CLAUDE.md`: Workflow Schema rewritten for the unified `execute:`
  shape; new sections "Execute URLs", "URL resolution", "Remote URL
  support".
- `docs/plans/stage-5b-execute-url.md`: marked as landed.

---

## Stage 4: Phase → Node + Recursive Subgraphs + Join Nodes

### ⚠️ Breaking changes

#### YAML schema (hard cutover; no aliases)
- `phases:` → `nodes:`
- `dynamic_subphases:` → `fan_out:` (with structural flattening — see below)
- `quality_gate:` → `evaluation:`
- `dynamic_subphases.template.prompt_file` is now lifted onto the parent
  node (`prompt_file:`) since fan-out spawns instances of the parent
  itself.
- `dynamic_subphases.final_phases:` items are now top-level sibling
  nodes with explicit `depends_on: [<parent_id>]` chains. The first
  former-final-phase depends on the fan-out parent; subsequent ones
  chain depends on the previous.

A migration tool ships with this release: `sqrlly migrate <file>
[--dry-run | --in-place]` rewrites pre-Stage-4 YAML to the new shape
using `ruamel.yaml` (preserves comments, anchors, references, and
`{{templated}}` strings).

#### State channels
- `phase_outputs` → `node_outputs`
- `phase_structured_outputs` → `node_structured_outputs`
- `completed_phases` → `completed_nodes`
- `failed_phases` → `failed_nodes`
- `phase_worktrees` → `node_worktrees`
- `subphase_outputs` → `child_outputs`
- `_subphase_item` (transient field) → `_fan_out_item`

#### JSONL event log
- `phase_started` → `node_started`
- `phase_completed` → `node_completed`
- `phase_failed` → `node_failed`
- `phase_retried` → `node_retried`
- `gate_evaluated` is unchanged.

#### Schema/runtime symbols
- `WorkflowConfig` → `Graph`
- `Phase` (Pydantic model) → `Node`
- `PhaseExecutor` (Protocol) → `NodeExecutor`
- `_make_phase_node` → `_make_execution_node`
- `_make_subphase_node` → `_make_fan_out_node`
- `_make_final_phase_node` → `_make_final_fan_out_node`
- Many internal-only renames in `compile/graph.py` and `runtime/gates.py`
  (`phase_map`, `gated_phase_ids`, `dynamic_phase_ids`, `phase_output`
  parameter, etc.). User-facing template variables — `{{dep_subphases}}`,
  `{{<parent>_subphases}}`, `{{<parent>_subphase_worktrees}}` — were
  retained at the time of this cutover; later renamed to `_branches` /
  `_branch_worktrees` (see top of Unreleased: terminology cleanup).

### Added

#### `JoinExecution` — explicit topology marker
- New `execution: { type: join }` body. No-op execution that exists
  purely to name an explicit synchronization point at fan-in. Multi-
  predecessor nodes implicit-join automatically (LangGraph default);
  the join type is for author readability. Composes with `evaluation:`
  like any other node.

#### Recursive subgraph composition
- A `Node` may reference another graph YAML via `config:
  path/to/sub.yaml`. The subgraph compiles recursively via
  `add_node(name, compiled_subgraph)`. Graphs and subgraphs are
  definitionally identical — the same YAML is invokable both
  standalone (via `sqrlly run`) and as a subgraph reference.
- `inputs:` projects parent state into the subgraph's `node_inputs`
  channel; subgraph nodes see them as plain template variables alongside
  their own dep outputs. Subgraph never sees parent's full state.
- `outputs:` exposes named subgraph node outputs as `node_outputs[
  parent_id.key]` in the parent. Default (empty `outputs:`) projects
  the subgraph's terminal-node output as `node_outputs[parent_id]`.
- Compile-time guards: cycle detection over the config-reference DAG
  (`SubgraphCycleError`) and `settings.max_subgraph_depth` cap (default
  10; `SubgraphDepthError`).
- Subgraph internal failures surface as `failed_nodes[parent_id]` only;
  internals are not flattened into parent state.
- Demo: `examples/absurd-paper/subgraphs/compose_and_validate.yaml`
  carves the reconcile → persist → submission_check chain into a
  standalone-runnable subgraph; the parent workflow's `paper` node
  references it via `config:` + `inputs:`.

#### `sqrlly migrate` CLI
- Rewrites pre-Stage-4 YAML to the new shape losslessly. Round-trip
  YAML mode preserves comments, anchors, and `{{templated}}` strings.
- `--dry-run` prints the rewrite to stdout without modifying the file;
  `--in-place` writes back; default writes to stdout.
- Idempotent: running on already-migrated YAML is a no-op.

#### Multi-dep template aggregates
- `build_context` now synthesizes `_deps` (JSON map of dep_id → output)
  and `_dep_worktrees` (JSON map of dep_id → worktree path) when a node
  has 2+ dependencies. Lets multi-dep templates iterate inputs
  generically without hardcoding dep names.

### Changed
- The `evaluation:` block on a Node is the only way to attach gate logic
  (the alias `quality_gate:` was dropped in Stage 3b).
- `Node.config:` is mutually exclusive with `prompt_file:` / `execution:`
  on the same node (a node defines either an atom or a subgraph
  reference, not both).

### Documentation
- New: `docs/plans/stage-5a-route-node.md` — the next-stage plan for the
  `route` node primitive (simpleeval predicates, Command(goto), zero
  baked-in retry/halt semantics).
- New: `docs/plans/stage-5b-execute-url.md` — proposed redesign that
  collapses today's seven execution shapes (`prompt_file:` shorthand,
  `execution: { type: prompt | command | gate_only | join }`, top-level
  `config: + inputs: + outputs:`, `fan_out.template.prompt_file`) into a
  single `execute: { url, params }` block dispatched by URL extension.

### Notes on Stage 4c demo

The original Stage-4 plan called for `reviewer_pool` (in `examples/
absurd-paper/workflow.yaml`) to be carved into a per-child subgraph as
the Stage 4c demo. After landing the carve, two things became clear:

1. `reviewer_pool` is N-way parallelism over a manifest of single-prompt
   reviewers — that's exactly what the existing fan-out already does;
   wrapping each child in a subgraph reference adds nothing.
2. `paper` (the multi-step reconcile → persist → check chain) is the
   right shape for subgraph composition — multiple sequential nodes
   that compose into one logical unit.

Stage 4c therefore ships with `paper` as the canonical demo (top-level
`Node.config:`). The fan-out path stays inline-prompt-only by design;
when a real consumer needs multi-step per-child templates, that's the
right time to extend `FanOutTemplate` (queued under Stage 5b's broader
unified-execution-shape redesign).

### Removed
- Token usage tracking. The `tokens_used` field on `ExecutionResult`,
  the `token_usage` channel on `WorkflowState`, the per-phase token
  capture in `_ACPCallbacks`, the JSONL `tokens` field on
  `node_completed` events, and the CLI `Tokens:` summary line are all
  gone. Cost visibility was a documented backlog item that wasn't
  pulling its weight; if it returns later, the dispatch path needs
  redesign anyway under Stage 5b.
