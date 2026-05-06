# Abe Froman — Technical Architecture

How the orchestrator is structured. Reading guide for contributors and
LLMs that want to understand the internals beyond the README.

## 1. Three-layer architecture

The codebase splits into three sibling packages under
`src/abe_froman/`. Each layer has a hard import rule, enforced by AST
walking in `tests/architecture/test_layers.py`:

- **`schema/`** — Pydantic models. No langgraph, no compile, no
  runtime imports. The schema is the lingua franca: every layer can
  deserialize and reason about a `Graph`, but the schema itself
  doesn't know how the graph is compiled or executed.
- **`compile/`** — YAML → LangGraph `StateGraph`. May import
  `schema/`, `runtime/`, and `langgraph`. May not import `cli/`.
- **`runtime/`** — executors, backends, gates, foreman, logging,
  state, URL resolution. May import `schema/`. May **not** import
  `compile/` or `langgraph`. Every concrete executor / backend lives
  here; graph compilation knows nothing about the specific backend
  in use.

Two language-level carve-outs:

- `runtime/url.py` — pinned langgraph-free
  (`tests/architecture/test_layers.py:88-96`) so `schema/` and
  `compile/` can import URL resolution across layer boundaries.
- `compile/route.py` — pinned langgraph-free
  (`tests/architecture/test_layers.py:63-70`) so the simpleeval
  sandbox stays a pure state-shape utility.

```
   +----------------+
   |   schema/      |  Pydantic; no langgraph, runtime, or compile
   +-------+--------+
           ^
   +-------+--------+      +----------------+
   |  compile/      |----->|  runtime/      |  no langgraph,
   |  (langgraph,   |      |  no compile    |  no compile
   |   schema,      |      +----------------+
   |   runtime)     |
   +-------+--------+
           ^
   +-------+--------+
   |   cli/         |
   +----------------+
```

Why the split matters: testability (each layer unit-tests in
isolation), swapability (one file + one factory branch adds a new
backend), and schema-as-contract.

## 2. Pipeline overview

```
YAML
  | yaml.safe_load
  v
Graph (schema/models.py: Graph, Node, Settings, Execute, Evaluation, FanOut)
  | build_workflow_graph(config, executor=, checkpointer=, logger=,
  |                      effective_settings=)
  v
compiled StateGraph (langgraph)
  | astream(initial_state, config=run_config, stream_mode="values")
  v
runtime/runner.py::run_workflow → final state dict
```

Three injection points to `build_workflow_graph`:

- **`logger=`** (optional `JsonlLogger`) — passed so subgraph
  wrappers and per-Send fan-out invokers can stream their internal
  `astream(...)` snapshots through `SubgraphLogger(prefix=parent_id)`.
  Subgraph-internal events surface as `parent_id::child_id`; nested
  prefixes compose (`paper::reconcile::step1`).
- **`checkpointer=`** (optional, typically `AsyncSqliteSaver`) —
  forwarded to `builder.compile(checkpointer=...)`. The thread_id is a
  deterministic SHA1 of `(workflow_name, resolved_workdir)` (16 hex
  chars), wired in `cli/main.py`.
- **`executor=`** (`NodeExecutor` Protocol, typically
  `ForemanExecutor` wrapping `DispatchExecutor`) — the compiler
  threads `effective_settings` to `_make_execution_node`, which
  closure-captures it and forwards as `settings_override` on every
  `executor.execute(...)`.

## 3. State model (`runtime/state.py`)

All nodes consume and produce `WorkflowState` (TypedDict). Fields:

- `workflow_name: str` — top-level config name (no reducer).
- `completed_nodes: list[str]` / `failed_nodes: list[str]` /
  `errors: list[dict]` — append-on-merge via `operator.add`.
- `node_outputs: dict[str, Any]` (textual; subgraph dotted keys
  `parent.key` live here too) / `node_structured_outputs` /
  `retries: dict[str, int]` / `child_outputs` (per fan-out child
  `parent::item_id`) / `node_worktrees` — right-wins per-key
  via `_merge_dicts`.
- `evaluations: dict[str, list[dict]]` — per-key list extension via
  `_merge_evaluations`; a node's history grows, never replaces.
- `workdir: str` / `dry_run: bool` — resolved working dir, dry-run
  flag.
- `_fan_out_item: dict` (NotRequired) — per-Send-branch manifest
  item.
- `node_inputs: dict[str, str]` (NotRequired) — rendered subgraph
  `inputs:` projected by `make_subgraph_node` before invocation.

The `REDUCERS` table at `runtime/state.py:30-40` is the single source
of truth for parallel super-step merging. Reducer parity is
load-bearing: see Section 11.1.

## 4. Compile layer

### `compile/graph.py`

Entry: `build_workflow_graph(config, executor, checkpointer, *,
logger, effective_settings, _depth, _base_dir)` at
`compile/graph.py:220`. Classifies each node, registers a node fn,
wires plain + conditional edges.

All edge-related code in this layer reads `node.depends_on` only.
Authors who write `next:` (forward-pointer) on a node get it
normalized into the targets' `depends_on:` lists at parse time —
`Graph.validate_node_references` in `schema/models.py` walks every
node's `next:`, appends the source id to each target's `depends_on:`
(de-duped), then clears `next:`. Compile and runtime layers see a
single canonical adjacency.

Helpers: `_find_terminal_nodes` (nothing-depends-on set; wires to
`END`), `_detect_cycles` (DFS coloring on `depends_on`),
`_is_subgraph_ref` (delegates to
`compile/subgraph.py::node_subgraph_path`), `_is_route`,
`_make_route_node` (async fn returning `Command(goto=)`),
`_make_evaluation_router` (state-reader returning
`END`/`pass_targets`/retry-id), `_register_evaluation_node` (adds
`_eval_<id>`), `_wire_evaluation_pair` (plain `exec → _eval_exec`
plus conditional retry/pass/fail), `_make_dynamic_router` (emits
`Send(template_id, {**state, _fan_out_item: item})` per manifest
item).

### `compile/nodes.py`

- `_make_execution_node(node, config, executor, *, effective_settings)`
  at `compile/nodes.py:392` — closure-captures `effective_settings`.
  Body order: skip-if-completed → `check_dep_failed` →
  `check_dry_run` → `all_deps_completed` join check → `build_context`
  → retry-backoff sleep → `inject_retry_reason` →
  `scaffold_output_directory` → `execute_with_timeout` →
  `validate_output_contract` → `assemble_success_update` (+
  `node_worktrees` from `executor.get_worktree(node.id)`).
- `_make_evaluation_node(...)` at `compile/nodes.py:488` — reads
  `node_outputs[node_id]`, defers via `{}` if upstream output
  absent, runs `run_evaluation_and_outcome`.

Pure helpers (langgraph-free): `check_dep_failed`,
`all_deps_completed`, `check_dry_run`, `build_context`,
`inject_retry_reason`, `execute_with_timeout`,
`make_failure_update`, `assemble_success_update`,
`classify_evaluation_outcome`, `run_evaluation_and_outcome`,
`build_evaluation_outcome_update`.

### `compile/dynamic.py`

- `_make_fan_out_node(...)` at `compile/dynamic.py:43` — per-Send
  template body. Retry loop runs **inline**, not via a graph
  self-loop. Reason: Send-dispatched branches lose `_fan_out_item`
  at any conditional-edge boundary (boundary merge strips per-Send
  keys). Inline retry preserves per-item state trivially.
- `_make_final_fan_out_node(..., is_first)` at
  `compile/dynamic.py:271` — the FIRST final is wrapped in a barrier
  that defers (`{}`) until every manifest item has settled
  (`completed_nodes ∪ failed_nodes`).
- `_merge_updates(base, extra)` at `compile/dynamic.py:25` — re-uses
  `state.REDUCERS` so inline accumulation matches super-step merge.
  See Section 11.1.

If `template.execute.url` ends in `.yaml`/`.yml`, each Send branch
invokes a compiled subgraph via `make_fan_out_subgraph_invoker`;
its terminal output flows back as `ExecutionResult.output` so
downstream gates/aggregation are agnostic to prompt-vs-subgraph
templates.

### `compile/subgraph.py`

`load_graph`, `make_subgraph_node` (compiles subgraph at parent
build time; renders `inputs:` against parent context, projects
terminal output back via `outputs:`),
`make_fan_out_subgraph_invoker` (per-Send-branch invoker for
YAML-template fan-out), `_terminal_node_output`,
`node_subgraph_path` / `execute_subgraph_path` (single source of
truth for "is this a subgraph reference?"), `detect_config_cycle`
(walks the YAML reference DAG once at depth 0; raises
`SubgraphCycleError` on revisit), `SubgraphDepthError` (raised on
`max_subgraph_depth` exceeded; default 10).

### `compile/route.py`

Simpleeval sandbox; langgraph-free.

- `build_route_namespace(state, deps)` — binds each dep to
  `node_structured_outputs[dep]` (else `node_outputs[dep]`), plus
  `history` (`state.evaluations`) and `state`.
- `evaluate_case(when, namespace)` — runs
  `simpleeval.EvalWithCompoundTypes` with safe functions
  `{len, any, all, min, max, sum}`. Dunders/imports/statements
  blocked by the evaluator.

### `compile/evaluation.py`

Desugars Evaluation YAML → first-match route ladder.
`Criterion(field, op, value)` (dotted field; ops
`== != > >= < <= in not_in has`), `Route(when, to, params)` (`to` ∈
`{pass, retry, fail_blocking, warn_continue}`),
`EvaluationRecord.now`, `evaluation_to_routes(evaluation, max_retries)`
(pass route + N retry routes; one per dimension or one for
`score >= threshold`), `walk_routes`, `evaluation_fallback`
(`warn_continue` if non-blocking else `fail_blocking`).

## 5. Runtime layer

### `runtime/executor/dispatch.py::DispatchExecutor`

URL extension/scheme → handler. The dispatch table:

| URL extension / scheme | Mode override | Handler | Params shape |
|---|---|---|---|
| `*.md`, `*.txt`, `*.prompt` (file://) | — | `_dispatch_prompt` | `PromptParams` |
| `*.md` (https://, allow_remote_urls) | — | `_dispatch_prompt` | `PromptParams` |
| `*.py`, `*.js`, `*.mjs`, `*.ts`, `*.sh` | — | `_dispatch_script` | `SubprocessParams` |
| any | `mode: python\|node\|tsx\|bash` | `_dispatch_script` (forced interpreter) | `SubprocessParams` |
| extensionless / unrecognized (file://) | — | `_dispatch_binary` | `SubprocessParams` |
| any (file://) | `mode: exec` | `_dispatch_binary` | `SubprocessParams` |
| `*.yaml`, `*.yml` (file://) | — | error (compile-time only) | — |
| any | `mode: subgraph` | error (compile-time only) | — |
| `execute.type == "join"` | — | no-op `ExecutionResult(success=True, output="")` | — |
| `execute.type == "route"` | — | error (compile-time only) | — |

Per-mode params validation flows through
`schema.params.coerce_params(resolved, raw, mode)` so a typo like
`arg:` on a prompt URL surfaces as a clear `ValidationError` at
schema parse time. Remote URL gates and per-compile fetch caching go
through `runtime.url.fetch_url(resolved, settings, cache)`.

### `runtime/executor/prompt.py::PromptExecutor`

Two-step: `apply_preamble(template, settings)` prepends
`settings.preamble_file` (or returns `ExecutionResult` on
missing-file); `execute_rendered(rendered, model, workdir, timeout,
settings)` runs the `send_prompt` loop with `OverloadError`-driven
downgrade chain (`settings.model_downgrade_chain`, default
`["opus", "sonnet", "haiku"]`).

`resolve_model(node, settings)` is the foreman-side selector
(`node.model or settings.default_model`). PromptParams.model is a
runtime-only override handled inside `dispatch._dispatch_prompt` and
deliberately invisible to foreman so the per-model semaphore reserves
the slot for the *declared* model.

### `runtime/executor/backends/`

`PromptBackend` Protocol (`runtime/result.py:40`): `async
send_prompt(prompt, model, workdir, timeout)` and `async close()`.

- `stub.py::StubBackend` — deterministic placeholder used as the
  auto-detect last-resort fallback. Returns `[prompt-stub] model=...
  prompt_length=...` so prompt-substitution can be observed without a
  real backend. Open question (per the no-fakes rule): whether this
  belongs in production code at all — see `WISHLIST.md`.
- `acp.py::ACPBackend` — `npx @zed-industries/claude-code-acp`.
  See Section 9.
- `openai.py::OpenAIBackend` — OpenAI-compatible client; reused for
  DeepSeek (`base_url=https://api.deepseek.com/v1`).
- `factory.py::create_prompt_backend(executor_type, **kwargs)` —
  string → instance. `auto_detect_executor()` picks DeepSeek key →
  `"deepseek"`, else `npx` on PATH → `"acp"`, else warns + `"stub"`.

### `runtime/gates.py`

- `EvaluationResult` (dataclass) — `score`, `scores`, `feedback`,
  `pass_criteria_met`, `pass_criteria_unmet`.
- `_parse_evaluation_output(raw, *, allow_bare_float, require_score)`
  — accepts bare float (script gates only), JSON `{"score": ...}`,
  full feedback JSON, or multi-dimension JSON (numeric fields not in
  `_NON_SCORE_KEYS` extracted as dimension scores). Loud failure on
  malformed: `score=0.0` with diagnostic feedback.
- `run_evaluation_script(...)` — subprocess; stdin-fed
  `node_output`. Env: `NODE_ID`, `WORKFLOW_NAME`, `ATTEMPT_NUMBER`,
  `WORKDIR`.
- `run_evaluation_llm(...)` — `.md` template with `output`,
  `node_id`, `attempt`; dispatched to the `PromptBackend`.
- `run_evaluation(...)` — `.py`/`.js` → script, `.md` → LLM.
- `scaffold_output_directory` / `validate_output_contract` —
  pre-create `base_directory`; return list of missing required
  files (empty = pass).

## 6. Stage 5b unified Execute shape

`Execute` at `schema/models.py:19` carries three orthogonal shapes;
exactly one must be active per node (validated by `validate_shape`):
URL mode (`url:` set, optional `mode:` override), join sentinel
(`type: join`), route ladder (`type: route` + `cases:` + `else:`).

### URL resolution (`runtime/url.py::resolve_url`)

Three rules in order: (1) explicit protocol passthrough — `url`
containing `"://"` is returned canonicalized; (2) absolute path —
`url` starting with `/` becomes `file://<path>`; (3) relative —
if `settings.base_url` is set, resolve via `urljoin` (path-only
base is promoted to `file://` first), else use
`Path(workdir).resolve()`.

`canonical(url)` at `runtime/url.py:58`: `urlsplit` → lowercase
host (preserve port, userinfo, path, query, fragment) →
`urlunsplit`. Trailing-slash variance and case-different hosts
compare equal in cycle detection and cache keys.

### Remote URL gates

Non-`file://` URLs go through four gates in `fetch_url`:

1. `allow_remote_urls: bool` (default False) — master switch.
2. `allowed_url_hosts: list[str]` (fnmatch; `[]` = no filter) —
   host allow-list.
3. `allow_remote_scripts: bool` (default False) — extra opt-in for
   `.py`/`.js`/`.sh`.
4. `max_remote_fetch_bytes: int` (default 5_000_000) — body cap.

Per-prefix `url_headers` with `${VAR}` env expansion (via
`_expand_vars`) attach auth headers. Missing env var raises
`ValueError` (config error, not network error).

### Per-compile fetch cache

`_RemoteFetchCache` is one dict per `DispatchExecutor`. A single
`build_workflow_graph` shares its parent's cache via the same
executor instance, so the same canonical URL is fetched once across
all subgraph compilations. Fresh CLI invocation = fresh cache; no
on-disk cache.

## 7. Scope-aware settings

Each scope (top-level Graph, every nested subgraph) carries its own
effective `Settings`. `runtime/settings_merge.py::merge_settings(parent,
child)` uses Pydantic v2's `model_fields_set` to distinguish authored
fields from defaults: only the child's *explicitly authored* fields
override the parent.

Resolution order (lowest → highest): `Settings()` defaults →
outermost `Graph.settings` → each nested subgraph's `Graph.settings`
→ per-node fields (`node.timeout`, `node.evaluation.max_retries`,
PromptParams.model — resolved at dispatcher time, not by
`merge_settings`).

Compile-time threading: `build_workflow_graph(config, *,
effective_settings=None)` is `None` at top level; subgraph wrappers
compute `merge_settings(parent_eff, sub_config.settings)` and pass
it via recursive `compile_fn(...)`. `_make_execution_node(...,
effective_settings=settings)` closure-captures it; every
`executor.execute(..., settings_override=settings)` call site
forwards; `ForemanExecutor` forwards unchanged to its inner
executor.

`merge_settings` docstring at `runtime/settings_merge.py:29-46`
documents the round-trip caveat: the returned `Settings` has every
field marked as set, so feeding it back as `child` would shadow
the parent. Subgraph callers always parse a fresh child from YAML.

## 8. Foreman + worktrees

`runtime/foreman.py::ForemanExecutor` wraps an inner `NodeExecutor`
and adds: global semaphore (`Semaphore(max_parallel_jobs)`, default
4); per-model semaphores layered inside; per-`node.id` git worktree
at `<workdir>/.abe-foreman/wt-<safe_id>-<uuid8>/`. Worktrees are
allocated on first `execute()` and retained across retries (subphases
use composite keys `parent_id::item_id`).

`_acquire_worktree` takes the per-foreman `asyncio.Lock`, returns
the existing path if still on disk, else calls `git -C <base>
worktree add -q <dest> HEAD` (failure raises `RuntimeError`
loudly). Per-model semaphore selection uses
`resolve_model(node, settings_override or self._settings)` — a
subgraph that overrides `default_model` accounts under its own tier.

Foreman never auto-cleans worktrees: retries reuse the tree,
failed runs leave it intact for inspection, and a wrong cleanup
decision is unrecoverable. Authors write explicit reconciliation
nodes; manual `git worktree remove <path>` for stragglers.

Resume: `ForemanExecutor(..., rehydrate=...)` seeds `_worktrees`
from a `dict[node_id, path]`. The CLI passes the
checkpointer-stored `state.node_worktrees` so retries after
`--resume` land back in the same tree.

## 9. ACP process-tree cleanup

The ACP adapter spawns `npx → node → claude`. The SDK's `__aexit__`
returns before descendants settle; long-running orchestrators
otherwise accumulate zombies (observed: 30 leftovers, 2.4 GB RSS,
OOM-prone).

Why `os.killpg` alone misses: `npx` re-spawns `node` and `claude`
in their own process groups, so killing the spawn-group hits `npx`
only. Once `npx` exits during `__aexit__`, descendants reparent to
PID 1 — and `/proc/<spawn_pid>/task/<spawn_pid>/children` no longer
reaches them.

`ACPBackend.close()` at `runtime/executor/backends/acp.py:141`:
(1) capture descendants BEFORE shutdown via
`_collect_descendants(pid)` (walks
`/proc/<pid>/task/<pid>/children` recursively); (2) graceful
shutdown via `__aexit__` with 5s timeout (SDK failures swallowed);
(3) hard-kill captured PIDs — SIGTERM each, sleep 0.5s, SIGKILL
survivors. `(ProcessLookupError, PermissionError)` swallowed per
call.

Pinned by
`tests/acp/test_acp_cleanup.py::test_close_reaps_descendant_tree`
— spawns the backend, captures the descendant tree (~15 PIDs),
calls `close()`, asserts every PID is dead within 3s.

## 10. Observability

`runtime/logging.py::JsonlLogger` emits one JSON object per line.
Event types (from `log_snapshot` diffing two state snapshots):
`workflow_start` / `workflow_end` (emitted by `run_workflow` when it
owns the logger); `node_completed` (set diff
`curr_completed - prev_completed`); `node_failed` (set diff plus
matching `errors[].error`); `gate_evaluated` (new entries appended
to `evaluations[node_id]`; carries `invocation`, `score`, and
per-dimension `scores` for multi-dim gates); `node_retried`
(`curr_retries[node] > prev_retries[node]`).

`SubgraphLogger(base, prefix)` decorates by rewriting `event["node"]`
to `f"{prefix}::{event['node']}"`. Subgraph wrappers
(`make_subgraph_node`, `make_fan_out_subgraph_invoker`) feed their
own `astream(stream_mode="values")` snapshots through this
decorator. Nested subgraphs compose prefixes
(`paper::reconcile::step1`). `_PrefixingProxy` reuses
`JsonlLogger.log_snapshot`'s diffing rules without duplication.

Why state-diff per snapshot rather than per executor call: every
super-step in LangGraph emits one snapshot; diffing aligns
naturally with reducer merging. A single snapshot can cover
multiple parallel branches' completions, all surfacing as separate
events.

## 11. Key non-obvious invariants

These are easy to miss from any single file in isolation.

### 11.1. Reducer parity across inline accumulation

`compile/dynamic.py::_merge_updates` MUST use `state.REDUCERS`. The
fan-out node accumulates state inline across its retry loop; once
it returns, LangGraph's super-step reducers fold the aggregate into
prior state. Inventing private merge semantics would cause inline
behavior to diverge from boundary behavior — e.g.
`completed_nodes` deduplicating inline but append-merging at the
boundary, surfacing the same id twice.

### 11.2. Route module is langgraph-free by policy

`compile/route.py` imports no langgraph; pinned by
`tests/architecture/test_layers.py::test_route_is_langgraph_free`.
The simpleeval sandbox is footgun prevention (no dunders, no
statements, no imports), not adversarial sandboxing — workflow YAML
is treated as author-checked-in code, not untrusted input.

### 11.3. Gated fan-out template retry runs inline

`_make_fan_out_node` runs its retry loop in the Python node body,
not via a graph self-loop. Reason: Send-dispatched branches lose
`_fan_out_item` at any conditional-edge boundary. A graph-level
retry edge would re-enter without per-branch item context.

### 11.4. Settings scope threads via `settings_override`

Every `executor.execute(...)` call site receives the scope's
effective `Settings` as `settings_override`. `_make_execution_node`
captures it in closure; foreman forwards; `DispatchExecutor` uses
it for `base_url`, `default_model`, `model_downgrade_chain`,
`preamble_file`. A subgraph node sees its own settings, not the
parent's — even though foreman, dispatcher, and prompt executor
are the same singleton across scopes.

### 11.5. Cycle detection at compile-time, depth 0 only

`detect_config_cycle` walks the URL DAG once when `_depth == 0`.
Nested `build_workflow_graph` calls (recursive subgraph compilation)
skip cycle detection and rely on `settings.max_subgraph_depth`
(default 10). Same rule for fan-out subgraph templates in
`make_fan_out_subgraph_invoker`.

### 11.6. Per-compile fetch cache scope

`_RemoteFetchCache` lives one-per-`DispatchExecutor`. A single
`build_workflow_graph` call shares its parent's cache via the same
executor instance — same canonical URL fetched once across all
subgraph compilations. Fresh CLI invocation = fresh cache.

## 12. Where to start reading

1. `tests/architecture/test_layers.py` — layer rules in AST walking.
2. `src/abe_froman/runtime/state.py` — `WorkflowState` + `REDUCERS`.
3. `src/abe_froman/compile/graph.py` — `build_workflow_graph`.
4. `src/abe_froman/compile/nodes.py` — `_make_execution_node` and
   `_make_evaluation_node`.
5. `src/abe_froman/runtime/runner.py` — `astream` loop + logger
   integration; the only file that owns workflow lifecycle.
6. `src/abe_froman/runtime/executor/dispatch.py` — URL extension →
   handler dispatch table. URL resolution and remote-fetch gates
   live in `runtime/url.py`.
