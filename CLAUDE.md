# sqrlly — Operator Notes for Claude

Workflow orchestrator using LangGraph for graph topology and Claude /
DeepSeek / scripts for execution.

This file is operational guidance for Claude Code working inside this
repo. Narrative documentation lives elsewhere:

- **`README.md`** — what the project is, install, quickstart, full
  schema reference, CLI reference, examples gallery, contributing.
- **`TECHNICAL.md`** — three-layer architecture, pipeline flow, state
  model, key invariants, contributor reading order.
- **`WISHLIST.md`** — open work, prioritized + non-prioritized.
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
uv sync --extra openai                       # add OpenAI/DeepSeek backend
npm i -g @zed-industries/claude-code-acp     # for ACP backend / acp tests

uv run pytest tests/ --ignore=tests/acp -v   # ~878 tests, ~50s
uv run pytest tests/acp -v                   # ACP tests, ~2 min, requires npm package above
uv run pytest -m live                        # live-backend tests (skipped per-key when absent)
uv run pytest tests/architecture/test_layers.py  # layer rule enforcement

uv run sqrlly validate config.yaml
uv run sqrlly run config.yaml             # auto-detect backend
uv run sqrlly run config.yaml -e acp      # force ACP (choices: acp | anthropic | custom | deepseek | openai)
uv run sqrlly run config.yaml --resume    # resume from checkpoint
uv run sqrlly run config.yaml --log out.jsonl
uv run sqrlly graph config.yaml           # Mermaid topology
uv run sqrlly migrate old.yaml --in-place # pre-Stage-4 → 5b transforms
```

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
- `executor/backends/{acp,anthropic,openai,factory}.py`.
- `executor/backends/_lazy_client.py` — `LazyClientMixin` (lazy SDK
  client init + idempotent close) and `await_with_timeout` helper.
  Shared by anthropic + openai backends.
- `executor/backends/_overload.py` — `maybe_raise_overload` +
  per-provider `ANTHROPIC_OVERLOAD_NAMES` / `OPENAI_OVERLOAD_NAMES`
  frozensets. Both backends map transient SDK errors to
  `OverloadError` through this.

**`src/sqrlly/cli/`** — entry points.
- `main.py` — Click CLI; wires `AsyncSqliteSaver`, `ForemanExecutor`,
  `thread_id`, `JsonlLogger`, auto-detect.
- `migrate.py` — pre-Stage-4 → 4 → 5b YAML transforms (idempotent;
  preserves comments + anchors).

## Testing principles

These guide all new tests; violations should be flagged in review.

1. **No mocks of external systems.** Tests use real subprocess / real
   ACP / real DeepSeek API (gated on key) / real validators.
   `MockExecutor` (`tests/mock_executor.py`) is a custom test double
   implementing the `NodeExecutor` Protocol — NOT `unittest.mock`.
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
  substitution.
- **Subphase quality gates record but don't retry** — retry routing
  only works for top-level node gates, not fan-out children.
- **Per-model backpressure under downgrade** — Foreman acquires the
  semaphore for the node's *original* model. If `PromptExecutor`
  downgrades opus → sonnet mid-call (on `OverloadError`), the sonnet
  semaphore is not acquired for that call. Intent, not enforcement
  under downgrade.
- **No automatic worktree cleanup** — Foreman never removes
  worktrees. Authors write reconciliation nodes; stray trees
  accumulate under `<workdir>/.sqrlly/`. Clean up manually with
  `git worktree remove <path>`.
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
- **API key resolution** — generic resolver at
  `runtime/secrets.py::resolve_secret(name, *, settings, settings_attr)`.
  Layers: workflow YAML setting → `os.environ[name]` → project-local
  `.env` file (auto-discovered by walking up from CWD). sqrlly
  never reads from machine-global keystores. Both `_resolve_deepseek_key`
  and `_resolve_anthropic_key` are thin wrappers over this resolver.
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
| Live-backend regression test | `tests/e2e/test_live_backend_roundtrip.py` (parametrized over 4 backends; `pytest.mark.live`, skipif per-key) |
| Backend lazy-init + close | `src/sqrlly/runtime/executor/backends/_lazy_client.py::LazyClientMixin` — subclass + implement `_create_client()` |
| Transient-error → `OverloadError` mapping | `src/sqrlly/runtime/executor/backends/_overload.py::maybe_raise_overload` (status-code set + per-provider class-name frozensets) |

When in doubt, read `TECHNICAL.md` Section 11 ("Key non-obvious
invariants") before changing compile or runtime layer code — five of
six edge cases bite contributors who haven't seen them before.
