# sqrlly — Operator Notes for Claude

Workflow orchestrator using LangGraph for graph topology and Claude
(via the local ACP adapter) / scripts for execution.

This file is operational guidance for Claude Code working inside this
repo. Narrative documentation lives elsewhere:

- **`README.md`** — what the project is, install, quickstart, concept
  tour, CLI overview, examples gallery (PyPI-facing front page).
- **`docs/schema-reference.md`** — the exhaustive field-by-field
  schema reference (Settings / Node / Execute / presets / route /
  evaluation), URL dispatch table, route-predicate namespace.
- **`TECHNICAL.md`** — three-layer architecture, pipeline flow, state
  model, key invariants, contributor reading order.
- **`SKILLS.md`** — agent skill doc: instructions for an AI coding
  agent authoring and running sqrlly workflows (Codex skill format).
- **`WISHLIST.md`** — open work, prioritized + non-prioritized.
- **`TODO.md`** — review-surfaced defects/cleanups deferred for
  focused work (each with a diagnosis). Distinct from WISHLIST
  (feature wants).
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

See `TECHNICAL.md` for the full layered breakdown.

## Build & Test

```bash
uv sync                                      # core deps
npm i -g @zed-industries/claude-code-acp     # for ACP backend / acp tests
# `claude` CLI on PATH                       # for cli backend / cli tests

uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -v   # ~940 tests, ~50s
uv run pytest tests/acp -v                   # ACP tests, ~2 min, requires npm package above
uv run pytest tests/cli -v                   # CLI tests, ~15s, requires `claude` on PATH
uv run pytest -m live -v                     # restored live-roundtrip (cli-only)
uv run pytest tests/architecture/test_layers.py  # layer rule enforcement

uv run sqrlly validate config.yaml
uv run sqrlly run config.yaml             # uses settings.presets (required)
uv run sqrlly run config.yaml -p <name>   # force a named preset from settings.presets
uv run sqrlly run config.yaml --resume    # resume from checkpoint
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
  `RouteCase`, `RouteElse`, `Route`, `OutputContract`, `FanOut`,
  `FanOutTemplate`, `FanOutFinalNode`.
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
- `gates.py` — `run_evaluation_script`, `run_evaluation_llm`,
  output parsing, `EvaluationResult`.
- `foreman.py` — `ForemanExecutor` (semaphores + memory back-pressure
  + worktree pool).
- `settings_merge.py` — `merge_settings(parent, child)` for
  scope-aware inheritance.
- `url.py` — `resolve_url`, `fetch_url`, `_RemoteFetchCache`,
  remote URL gates.
- `secrets.py` — `resolve_secret(name, *, settings, settings_attr)`
  (env → workflow YAML → project-local `.env`, walking up from CWD).
- `executor/dispatch.py` — `DispatchExecutor` (10-row URL dispatch).
- `executor/prompt.py` — `PromptExecutor` (template render, model
  downgrade).
- `executor/backends/{acp,cli,factory}.py`. Two LLM backends
  coexist: ACP (warm `claude-code-acp` adapter) and CLI
  (subprocess-per-call `claude -p`).
  `factory.create_backend_from_preset` is a two-row lookup table
  keyed on `(transport, provider)`.
- `executor/backends/_overload.py` — `maybe_raise_overload` +
  `ACP_OVERLOAD_SUBSTRINGS`. Both ACP and CLI backends share the
  same substring set because both ultimately hit the same upstream
  Claude API; the historical name "ACP" is retained.

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
`PromptBackend` doubles; real ACP / real subprocess only. The narrow
exception is patching the OpenAI SDK's HTTP layer in
`tests/unit/runtime/test_openai_backend.py` to verify our error
mapping (we're testing our wrapping code, not the SDK).

## Known limitations

- **Hyphenated node IDs in Jinja templates** — `{{research-phase}}`
  parses as subtraction. Use underscores in IDs that need template
  substitution. `validate` / `run` now emit an advisory warning for
  any hyphenated node id (`compile/lint.py`).
- **`graph` / `view` don't draw route edges** — `cli/main.py::graph`
  renders `compiled.get_graph().draw_mermaid()`, which only sees the
  static LangGraph. Inline-route targets are emitted at run time as
  `Command(goto=...)`, so a routed node's branch targets appear as
  unconnected nodes (no edges). Inherent to the routing implementation;
  the diagram shows static topology only. Flagged in SKILLS.md.
- **Remote script/binary execution not wired** — `base_url` /
  `http(s)://` urls fetch-and-run for *prompt* nodes
  (`_dispatch_prompt` → `fetch_url`), but `_dispatch_script` /
  `_dispatch_binary` still require `file://` and halt on a remote scheme
  ("Remote script execution not yet wired"). `allow_remote_scripts`
  gates the fetch in preparation only. Documented in
  `docs/schema-reference.md`, `TECHNICAL.md`, and `SKILLS.md`.
- **Per-model backpressure under downgrade** — Foreman acquires the
  semaphore for the node's *original* model. If `PromptExecutor`
  downgrades opus → sonnet mid-call (on `OverloadError`), the sonnet
  semaphore is not acquired for that call. Intent, not enforcement
  under downgrade.
- **Worktree cleanup is opt-in** — `settings.worktree_gc: on_success`
  triggers end-of-run cleanup (success only; the CLI calls
  `foreman.reclaim()` only when the run exits clean). The default
  (`never`) keeps trees under `<workdir>/.sqrlly/` for inspection
  and `--resume`. Manual cleanup with `git worktree remove <path>`
  always works regardless of the setting.
- **`--resume` is a fault-recovery re-run, not skip-completed** —
  `cli/main.py` loads the prior checkpoint's `channel_values`, seeds a
  *fresh* run with it, clears `failed_nodes`/`retries`/`errors`, and
  deletes the thread. Execution/evaluation `node_fn`s have no
  `completed_nodes` short-circuit (nodes.py:583, :684), so completed
  nodes **re-execute** (outputs refreshed; LLM nodes may diverge from
  the original run). State stays internally consistent. A true
  skip-completed resume is the pending `--resume` rewrite (WISHLIST
  26/31). Docs (README/SKILLS) describe the real semantics.
- **Subgraph event prefix is one level** — `runner`/`SubgraphLogger`
  prefix child events `parent::child`; a 2-level nest shows the
  *immediate* parent only (`mid::child`, not `top::mid::child`), so
  child ids must be unique across sibling subgraphs to avoid log
  collisions.
- **Checkpointer migration** — Pre-refactor `.sqrlly-state.json`
  format is ignored on `--resume`; re-run from scratch.
- **ACP soak under load** — process-tree cleanup is fixed for the
  test scenario, but a multi-hour run with `max_parallel_jobs > 1`
  hasn't been validated end-to-end (WISHLIST 49).
- **`_route_sender` is last-write-wins** — set by every Command
  emission from a `_route_<id>` dispatcher (with empty preamble
  when `include_eval` is off). Templates that reference
  `{{sender_id}}` should guard with `{% if sender_id %}` if they
  could be reached via a static depends_on edge AFTER an inline-
  route hop earlier in the same workflow (rare topology — a node
  fed both by goto and by depends_on from a different branch — but
  possible). Inside a goto-only target the var is always bound.

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
- **Secret resolution** — generic resolver at
  `runtime/secrets.py::resolve_secret(name, *, settings, settings_attr)`.
  Layers: workflow YAML setting → `os.environ[name]` → project-local
  `.env` file (auto-discovered by walking up from CWD). sqrlly
  never reads from machine-global keystores. The api-transport strip
  removed the only in-tree consumer of `resolve_secret` itself, but the
  `.env` layer is still used in-tree by `url.py::_expand_vars` (header
  `${VAR}` expansion), and `resolve_secret` remains available for
  workflow-defined keys (e.g. a script node calling a third-party
  service).
- **`pyproject.toml`** marker for ACP tests: `acp` (used in
  `pytest -m`).

## Quick references

| Task | Where to look |
|---|---|
| New schema field | `src/sqrlly/schema/models.py` (mind layer rules) |
| New backend | `src/sqrlly/runtime/executor/backends/` + factory case |
| New CLI flag | `src/sqrlly/cli/main.py` `@click.option` decorators |
| New node type | `src/sqrlly/compile/graph.py` (registration) + `src/sqrlly/compile/nodes.py` (factory) |
| New gate validator shape | `src/sqrlly/runtime/gates.py::_parse_script_output` |
| New WorkflowState field | `src/sqrlly/runtime/state.py` (TypedDict + REDUCERS) — beware of parity invariant in `compile/dynamic.py::_merge_updates` |
| Inline routing (route block, sender bindings, include_eval preamble) | `src/sqrlly/schema/models.py::Route`, `src/sqrlly/compile/graph.py::_make_inline_route_node`, `src/sqrlly/runtime/gates.py::build_eval_preamble` |
| Layer rule violation | `tests/architecture/test_layers.py` errors point to the offending file |
| Transient-error → `OverloadError` mapping | `src/sqrlly/runtime/executor/backends/_overload.py::maybe_raise_overload` (status-code set + `ACP_OVERLOAD_SUBSTRINGS`) |

When in doubt, read `TECHNICAL.md` Section 11 ("Key non-obvious
invariants") before changing compile or runtime layer code — five of
six edge cases bite contributors who haven't seen them before.
