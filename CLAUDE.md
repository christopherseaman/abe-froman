# Abe Froman — Operator Notes for Claude

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
                              Phase Node → ForemanExecutor → DispatchExecutor
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

uv run pytest tests/ --ignore=tests/acp -v   # ~754 tests, ~35s
uv run pytest tests/acp -v                   # ~14 ACP tests, ~2 min, requires npm package above
uv run pytest tests/architecture/test_layers.py  # layer rule enforcement

uv run abe-froman validate config.yaml
uv run abe-froman run config.yaml             # auto-detect backend
uv run abe-froman run config.yaml -e acp      # force ACP
uv run abe-froman run config.yaml --resume    # resume from checkpoint
uv run abe-froman run config.yaml --log out.jsonl
uv run abe-froman graph config.yaml           # Mermaid topology
uv run abe-froman migrate old.yaml --in-place # pre-Stage-4 → 5b transforms
```

## Project Layout

Three-layer split (enforced by `tests/architecture/test_layers.py`):

**`src/abe_froman/schema/`** — Pydantic models (no langgraph imports).
- `models.py` — `Graph`, `Node`, `Settings`, `Execute`, `Evaluation`,
  `RouteCase`, `RouteElse`, `Route`, `OutputContract`, `FanOut`,
  `FanOutTemplate`, `FanOutFinalNode`.
- `params.py` — `PromptParams`, `SubgraphParams`,
  `SubprocessParams` + `coerce_params()` resolver.

**`src/abe_froman/compile/`** — YAML → LangGraph (no cli imports).
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

**`src/abe_froman/runtime/`** — executors, backends, gates, foreman
(no compile/langgraph imports, except `url.py` which is also
langgraph-free).
- `state.py` — `WorkflowState` TypedDict + `REDUCERS`.
- `result.py` — `ExecutionResult`, `NodeExecutor` Protocol,
  `PromptBackend` Protocol, `OverloadError`.
- `runner.py` — `astream` loop, state-diff event detection.
- `logging.py` — `JsonlLogger`, `SubgraphLogger` (prefix decorator).
- `gates.py` — `run_evaluation_script`, `run_evaluation_llm`,
  output parsing, `EvaluationResult`.
- `foreman.py` — `ForemanExecutor` (semaphores + worktree pool).
- `settings_merge.py` — `merge_settings(parent, child)` for
  scope-aware inheritance.
- `url.py` — `resolve_url`, `fetch_url`, `_RemoteFetchCache`,
  remote URL gates.
- `executor/dispatch.py` — `DispatchExecutor` (10-row URL dispatch).
- `executor/prompt.py` — `PromptExecutor` (template render, model
  downgrade).
- `executor/backends/{acp,anthropic,openai,factory}.py`.

**`src/abe_froman/cli/`** — entry points.
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
  accumulate under `<workdir>/.abe-foreman/`. Clean up manually with
  `git worktree remove <path>`.
- **Checkpointer migration** — Pre-refactor `.abe-froman-state.json`
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
- **DeepSeek key location** — env `DEEPSEEK_API_KEY` first, then JSON
  at `~/.pi/agent/auth.json` (`{"deepseek": {"key": "..."}}`). The
  factory's `_resolve_deepseek_key()` is the source of truth.
- **`pyproject.toml`** marker for ACP tests: `acp` (used in
  `pytest -m`).

## Quick references

| Task | Where to look |
|---|---|
| New schema field | `src/abe_froman/schema/models.py` (mind layer rules) |
| New backend | `src/abe_froman/runtime/executor/backends/` + factory case |
| New CLI flag | `src/abe_froman/cli/main.py` `@click.option` decorators |
| New node type | `src/abe_froman/compile/graph.py` (registration) + `src/abe_froman/compile/nodes.py` (factory) |
| New gate validator shape | `src/abe_froman/runtime/gates.py::_parse_script_output` |
| New WorkflowState field | `src/abe_froman/runtime/state.py` (TypedDict + REDUCERS) — beware of parity invariant in `compile/dynamic.py::_merge_updates` |
| Inline routing (route block, sender bindings, include_eval preamble) | `src/abe_froman/schema/models.py::Route`, `src/abe_froman/compile/graph.py::_make_inline_route_node`, `src/abe_froman/runtime/gates.py::build_eval_preamble` |
| Layer rule violation | `tests/architecture/test_layers.py` errors point to the offending file |

When in doubt, read `TECHNICAL.md` Section 11 ("Key non-obvious
invariants") before changing compile or runtime layer code — five of
six edge cases bite contributors who haven't seen them before.
