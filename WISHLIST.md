# Wishlist

- [x] **Documentation** — _landed, post-Stage-5b._ README,
  TECHNICAL.md, and CLAUDE.md split by audience: README is the
  user/contributor entry point (install, quickstart, schema, examples);
  TECHNICAL.md is the architecture deep-dive (layers, pipeline, state
  model, invariants); CLAUDE.md is operator notes for Claude sessions.

## High-priority post-Stage-5c audit findings (2026-05-06)

Three parallel reviews (schema/compile, runtime, cli/tests/docs) ran
right after Stage 5c landed. Trivial dead-code and stale-doc fixes
already shipped in `e51d8b2`. Items 1 and 2 below are tackled in the
follow-up; items 3+ are real cleanup wins held for explicit decisions.

- [x] **(1) `_Capture` test backend violation** — _fixed._ Extracted `prepend_eval_preamble(rendered, context) -> str` as a pure helper in `runtime/executor/prompt.py`; unit-tested directly via `TestPrependEvalPreamble` (no backend involvement). The e2e test `tests/e2e/test_inline_route_with_eval.py` rewritten to assert on `state["_route_eval_preamble"]` and `state["_route_sender"]` only (real subprocess + real script gate; no `PromptBackend` instrumentation).
- [x] **(2) `apply_preamble` sum-type return** — _fixed._ `PromptExecutor.apply_preamble` now raises `FileNotFoundError` for missing preamble files and returns plain `str` on the success path. `_dispatch_prompt` catches the exception once at the boundary and translates to `ExecutionResult(success=False, error=...)`.
- [x] **(3) `auto_detect_executor` silent fallthrough on `ANTHROPIC_API_KEY`** — _delivered alongside StubBackend removal._ The dedicated `AnthropicBackend` (`runtime/executor/backends/anthropic.py`) now resolves first in the auto-detect chain (Anthropic key → DeepSeek key → ACP), so `ANTHROPIC_API_KEY` no longer silently falls through. Without any backend resolvable, `auto_detect_executor()` raises `RuntimeError` naming all three remediation paths.
- [ ] **(4) `Route.validate_shape` allows `else_` without `cases:`** (`schema/models.py:82-99`) — a bare `Route(else_=RouteElse(goto="x"))` validates as a silent unconditional redirect, identical to `goto: x` but with confusing structure. Test `test_route_empty_cases_with_else_is_legal` documents it as legal. Decide: reject with helpful error ("use `goto:` for unconditional"), or document the form as first-class.
- [ ] **(5) `EvaluationResult.score=0.0` for multi-dim evals** (`runtime/gates.py`) — when JSON omits top-level `"score"` and supplies only per-dim numbers, score defaults to 0.0. JSONL log events emit `score=0.0` even for passing multi-dim gates → operator confusion. Decide: derive from min/avg of dim scores, or stay 0 and document. Also: surface `score=min(dim_scores)` would mirror the "weakest-link" semantics of `dimensions[].min`.
- [x] **(6) `_PrefixingProxy.emit` duplicates `SubgraphLogger.emit` verbatim** — _delivered alongside `stream_mode="updates"`._ The `_PrefixingProxy` class was removed entirely; with the snapshot path gone, the prefix is applied directly to `node_name` before dispatch in `SubgraphLogger.log_update`, so there's only one place that knows about prefixing now.
- [ ] **(7) `FanOut.enabled: false` (or omitted) silently no-ops the block** (`schema/models.py:186-191`) — author-error footgun. A present `fan_out: { template: ... }` block should be self-evidently active. Decide: drop the `enabled` field (treat any non-null `template` as active) or add a model_validator that errors when `template` is set with `enabled: false`. Breaking schema change either way.
- [x] **(8) `_dispatch_prompt` stub fallback when `_prompt_executor is None` is unreachable in production** — _delivered alongside StubBackend removal._ The branch was replaced with a `RuntimeError` naming the offending node id and the missing-backend cause. No more silent fake output if a custom harness wires `DispatchExecutor` without a `prompt_backend`.
- [ ] **(9) `_make_final_fan_out_node` imports `_read_manifest` from `compile/graph.py` at function-body scope** (`compile/dynamic.py:321`) — cross-private-imports between sibling files. Extract `_read_manifest` to a shared helper (e.g., `compile/_manifest.py`) or pass as a parameter.
- [ ] **(10) `Graph.validate_node_references` does 4–5 unrelated checks in one 56-line function** (`schema/models.py:257-313`) — duplicate IDs, self-deps, missing dep refs, route-target resolution, depends_on-vs-route. Each is a distinct concern. Decompose into separate `@model_validator` methods. Maintainability, not correctness.
- [ ] **(11) Worktree `is_dir()` recheck in `ForemanExecutor._acquire_worktree`** (`runtime/foreman.py:76-83`) — YAGNI defense for an external-deletion race that can't happen during a single run. The "no auto cleanup" contract makes this a pessimistic guard. Drop the recheck or document.
- [ ] **(12) `monkeypatch.setattr` on `factory.shutil.which` in `test_factory.py`** (lines 92–139) — internals patching to bypass the real `shutil.which`. Gray area in the no-mock policy. Decide: accept as orchestration-instrumentation exception (and document in `feedback_no_fake_backends.md`), or refactor `auto_detect_executor` to accept a `which_fn` parameter for injection.

## Post-merge findings (2026-05-06, native-events + Anthropic + stubectomy branch)

- [ ] ~~**(13) `AnthropicBackend` doesn't surface `tokens_used`**~~ — _out of scope (decided 2026-05-06)._ Token-count surfacing on `ExecutionResult` was previously removed from scope; Samus's "Phase 5 (high)" priority on this is not a binding requirement on abe-froman. If a future consumer needs token counts, they can be threaded then; until that consumer exists, adding the field is YAGNI.
- [ ] **(14) `Settings.executor` accepts unknown strings without schema-time rejection** (`schema/models.py:194-202`) — typing `executor: stub` in YAML now passes Pydantic validation and only errors at `create_prompt_backend("stub")` time during `run`. Add a `Literal[...]` or model_validator listing the known set so typos surface at `validate` time. Coupled with the post-stubectomy choice list — `acp` / `anthropic` / `deepseek` / `openai`.
- [ ] **(15) `AnthropicBackend` empty-text-block edge case** (`runtime/executor/backends/anthropic.py:135-149`) — if the SDK ever returns a `type="text"` block with empty `text=""`, the current filter accepts it: `text_parts=[""]` is truthy, so we fall through to `ExecutionResult(output="")` instead of the explicit no-text-block failure. Unlikely (the SDK normalizes empty content to no-block-at-all), but the asymmetry between "no text block" → loud failure and "text block with empty text" → silent empty success is a footgun. Either tighten the filter (drop empty text blocks before the `if not text_parts:` check) or document.
- [ ] **(16) `_OVERLOAD_EXCEPTION_NAMES` includes speculative class names** (`runtime/executor/backends/anthropic.py:54-60`) — `OverloadedError` was added defensively (some SDK versions ship it) without confirming Anthropic SDK 0.99 actually raises this class. Keeping speculative names in the set is cheap (one frozenset lookup), but the comment "(newer SDK versions)" should either be replaced with a SDK-version note when verified, or the entry dropped. Track whether the entry ever fires in production telemetry.
- [x] **(17) `_is_overload_status` duplicated between `openai.py` and `anthropic.py`** — _delivered, post-merge audit._ Extracted `runtime/executor/backends/_overload.py::maybe_raise_overload(exc, *, class_names)` shared by both backends. Each backend keeps only its provider-specific class-name set as a frozenset constant.
- [x] **(18) Subgraph state isolation lacked unit-level coverage after `test_subgraph_isolation_no_parent_state_leak` was deleted in the stubectomy** — _delivered, post-merge audit._ Restored the test using `_ECHO`-rendered script-args (`leak={{parent_only}}|input={{from_parent}}`) as the observable. Mutation-tested: leaking parent's `node_outputs` into the subgraph's `node_inputs` flips the assertion with a clear "leak=PARENT_VALUE" diagnostic. Real subprocess only.
- [x] **(19) Resume-mode `# Skip if already completed` guard turned `Command(goto=...)` into silent no-op** — _delivered._ Three guards (`compile/nodes.py::_make_execution_node`, `compile/nodes.py::_make_evaluation_node`, `compile/dynamic.py::_make_subphase_node`) all originated in the pre-LangGraph-checkpointer era (commit `1ec9ed3`, 2026-03-20) when the homegrown JSON-envelope resume rehydrated `completed_phases` into state and re-ran the graph. After the migration to `AsyncSqliteSaver` on 2026-04-17, the guards became dead defensive code — checkpointer resume picks up at super-step boundaries, so already-completed nodes don't re-fire from dep edges. With Stage 5c routes, the guards became actively wrong: `Command(goto=X)` is a deliberate re-fire, but the guard suppressed body execution. Removing the three lines unlocks the wave-driven dynamic-task pattern (a fan-out parent re-entered via a route now re-reads its manifest from current state and dispatches the next wave). The `failed_nodes` checks are kept — those guard against re-attempting a hard-failed node and are semantically distinct.

## High-level Architectural

- [ ] Possible to offload orchastration piece to lightweight local tool/package instead of writing from scratch, similar to how we are leveraging langgraph? Dagster? Airflow and Kestra too heavy.

## Simplification candidates (surfaced by 2026-04-17 refactor-done review)

- [x] **Unify gate-eval via outcome-as-routing-signal** — _landed, Stages 3 + 3b._ Top-level phases use the data-driven model from `compile/evaluation.py` (`Criterion`, `Route`, `walk_routes`, `gate_to_routes`). `classify_gate_outcome` walks routes; `state.evaluations: {node_id: [EvaluationRecord]}` is the single source of truth for scores/feedback. Stage 3b (branch `stage-3b-evaluation-node`) completed the picture: gated top-level phases are a graph pair (`phase` execution node → `_eval_{phase}` evaluation node via `_make_evaluation_router`); gated subphase templates evaluate inline within the Send-dispatched subphase node (graph-level self-loops strip `_subphase_item` at the super-step boundary, so inline retry is the only shape that preserves per-branch identity); legacy `gate_scores`/`gate_feedback` state channels removed outright. Subphase gates honor `max_retries`, write `EvaluationRecord`s with real `invocation` counters, and log per-dimension scores.

- [x] **Phase → Node terminology + Recursive subgraphs + Join nodes** — _landed, Stage 4 (branch `stage-4-node-recursive-subgraphs`)._ Hard cutover on YAML and Python: `phases:`→`nodes:`, `Phase`→`Node`, `WorkflowConfig`→`Graph`, `dynamic_subphases:`→`fan_out:`, `final_phases:`→`final_nodes:`, `quality_gate:`→`evaluation:` (alias dropped). State channels and helpers renamed to match (`phase_outputs`→`node_outputs`, `_make_phase_node`→`_make_execution_node`, etc.). Stage 4b: `execution: { type: join }` no-op topology marker. Stage 4c: `Node.config:` references another graph YAML; recursively compiled via `add_node(node.id, compiled_subgraph)`. State projection via explicit `inputs:`/`outputs:` declarations; subgraph runs in isolation. Compile-time cycle detection + `settings.max_subgraph_depth=10` cap. 488 tests passing.

- [ ] **Split Evaluation from Decision** — _high priority._ Current `_eval_{id}` node runs the validator AND classifies the outcome (writes `completed_phases` / `failed_phases` / `retries`). Conflating the two forecloses non-routing consumers of an `EvaluationRecord`. Splitting unlocks:
    - **Refinement nodes** that consume an `EvaluationRecord` and produce a revised draft, without routing back to the original executor
    - **Multi-eval consensus** (two eval nodes → aggregator → decision)
    - **Human-in-the-loop review** slotted between eval and decision
    - **Cross-phase evaluation** (eval phase A's output, make a decision about phase B)

    Implementation: use LangGraph's `Command(update=..., goto=...)` to collapse the current "write state in node, read state in conditional-edge router" pattern into a single Decision node return. Removes `_make_evaluation_router`, `_subphase_id_resolver`, and most conditional-edge scaffolding in `compile/graph.py`. Pairs naturally with the `stream_mode="updates"` logging swap below — both unwind reimplementation of native LangGraph patterns.

- [ ] **Collapse `runtime/executor/backends/` → `runtime/backends/`** — 4-level nesting (`runtime/executor/backends/acp.py`) for 4 small files. Semantic loss: current nesting signals that only `PromptExecutor` uses backends. If we land the anthropic/openai backends (below), the signal still holds but less strongly — multiple executor types might route through one backends/ module. Low value, low risk; defer until a second executor family justifies the flattening.

- [ ] **Fold `compile/dynamic.py` into `compile/nodes.py`** — 182 LOC would bring `nodes.py` to ~530 LOC. The split is defensive today: `_make_subphase_node` has legitimately divergent semantics (no dep check, no output contract, no retry routing). **Worth revisiting after** the gate-eval unification above — if the gate block is gone and the final remaining divergence is "Send-triggered vs. normal-invocation," the split stops earning its keep.

- [ ] **Move `_detect_cycles` + `_find_terminal_phases` → `schema/models.py`** — topology validation belongs with the config model. Blockers: `schema/` is currently langgraph-free Pydantic-only; moving these functions in would require no imports from `langgraph`, which they already don't have. Clean move. Low priority — they're stable and small.

## Test doctrine cleanup

- [x] **Remove `StubBackend` from production code** — _delivered._ `runtime/executor/backends/stub.py` deleted; the factory's `"stub"` branch dropped; `auto_detect_executor()` raises `RuntimeError` with concrete remediation instead of falling back to fake output; `DispatchExecutor._dispatch_prompt` raises when no backend wired (no more silent `[prompt-stub]` placeholder). Stand-in tests migrated to `_ECHO` script execution; `prompt_length` substitution stand-ins deleted (Jinja rendering covered at the unit level by `tests/unit/runtime/test_prompt.py::TestRenderTemplate`). Breaking changes documented in CHANGELOG — `--executor stub` and `executor: stub` no longer accepted.

- [ ] **Resolve MemoryBackend / ErrorBackend / SleepyBackend / TrackingBackend policy conflict** — `tests/unit/runtime/test_prompt.py` has `MemoryBackend` + `ErrorBackend` used by ~14 orchestration tests; `tests/unit/runtime/test_foreman.py::TestPerModelBackpressure` has `SleepyBackend` + `TrackingBackend`. All four are hand-written Protocol doubles that strict reading of `feedback_no_fake_backends.md` forbids. They instrument `PromptExecutor` / `ForemanExecutor` orchestration (template, preamble, timeout, token threading; per-model concurrency caps) — NOT Claude behavior — so the strict interpretation may be wrong.
    - Three options (detailed at `/home/christopher/.claude/plans/memory-backend-policy.md`):
        1. Extend `StubBackend` with `record=True` to produce one sanctioned recording path; migrate all doubles to it.
        2. Amend the policy memo to permit orchestration-testing doubles, making the existing code compliant.
        3. Move ~14 tests to `tests/acp/` and accept weaker assertions against real Claude.
    - **Recommended: (1) + (2) together** — one sanctioned recording path, policy clarifies the distinction between Claude-behavior simulation (forbidden) and orchestration instrumentation (permitted, via `StubBackend(record=True)` only).

## Top priority after simplification refactor

- [ ] **Reconsider dependency/ordering model.** Possibly no more phases, just nodes and edges with next-node selection informed by node completion criteria. QualityGate becomes a node type and retry-with-context is the route chosen within a "failure context." Unclear this is actually simpler, but having retry / escalated / super-escalated tiers is definitely NOT the correct path forward.

- [ ] **ACP test flakiness**
    - `tests/acp/` tests fail or pass sporadically
    - Investigate: session lifecycle races, stdio buffering, stale `_session_id` on multi-prompt flows, Python 3.14 async-generator `aclose()` warning
    - Decide: fix root cause, or gate ACP tests behind a stricter pre-flight

- [x] **ACP process-tree leaks / zombie subprocesses under long runs** — _initial fix landed in post-Stage-5b. `ACPBackend.close()` now captures descendants from `/proc/<pid>/task/<pid>/children` BEFORE `__aexit__` (so re-parented orphans stay tracked), runs graceful shutdown under a 5s `wait_for`, then SIGTERM→0.5s→SIGKILLs each captured PID. Teardown assertion `tests/acp/test_acp_cleanup.py::test_close_reaps_descendant_tree` watches a 15-PID descendant tree disappear within 3s of close. **Open: soak-test under load** — needs a multi-hour run with `max_parallel_jobs > 1` against the absurd-paper workflow before this can be fully ticked off._

- [ ] **Worktree garbage collection**
    - Today: `ForemanExecutor` never removes trees under `<workdir>/.abe-foreman/` — disk + inodes accumulate indefinitely across runs
    - CLI: `abe-froman worktree list` — table of (phase_id, created, last_used, size, branch)
    - CLI: `abe-froman worktree prune [--older-than 7d] [--phase <id>] [--dry-run]` — `git worktree remove` + directory delete, with safety checks for uncommitted changes
    - Optional auto-GC: `settings.cleanup_worktrees_on_success: bool` — prune at `workflow_end` only when the final state is all-completed (preserve on partial failure so users can inspect)
    - Must preserve the across-retries reuse that foreman relies on (keys trees by `phase_id`); GC runs against _completed_ workflow threads only

## Observed during 2026-04-18 complex-demo build (examples/absurd-paper/)

Building the 13-phase demo surfaced issues not previously cataloged. Kept here as a group so their cross-relationships stay visible.

### ACP reliability

- [ ] **Write tool with `../../`-traversing paths hangs indefinitely**
    - Symptom: a minimal `persist` phase whose sole job was `Write(path="../../paper/paper.md", content="...")` timed out at 180s with no output, no error, no file written. Same behavior with `Bash` + heredoc.
    - The worktree contains no partial file; the staging dir (`<workdir>/paper/`, pre-scaffolded) stays empty. Claude is not refusing — no text response is returned at all; the ACP session just stalls.
    - Repro: any prompt phase asking Claude Code via ACP to Write or Bash-write to a path outside the session's apparent workdir (`cwd` = foreman worktree). Absolute paths may or may not behave differently — untested in this session.
    - Investigate: whether claude-code-acp enforces a path-allowlist silently; whether there's a permission dialog waiting that our auto-approver doesn't recognize; whether `_send_lock` is hiding a hang in the dispatch path.
    - Workflow-author workaround today: avoid Write/Bash to non-workdir paths; pass state via text outputs only. This blocks the documented "author-written merge phase" pattern in CLAUDE.md.

- [ ] **`acp.exceptions.RequestError: Internal error` appears under concurrent LLM calls even with `_send_lock` in place**
    - Stack trace fires from `acp/connection.py:237` via `acp/task/dispatcher.py:81`. Observed under `max_parallel_jobs=2` + `per_model_limits.sonnet=2` while the ACP backend serializes `send_prompt` via `_send_lock`.
    - Phases still complete in these runs — the error is logged but recovery happens somewhere. Suggests the SDK is raising and the dispatcher is retrying or dropping.
    - Needs root-cause diagnosis. Possibly related to background tasks per the supervisor traceback, or to in-flight session state while a new prompt arrives.

### Orchestrator join semantics

- [x] **Multi-gated-predecessor join bug** — _Stage 1, 2026-04-17._ `_make_phase_node::node_fn` now returns `{}` when any dep is missing from `completed_phases`. LangGraph re-fires the node on each subsequent pred completion; missing-pred returns turn the node into a natural join barrier. `examples/absurd-paper/` runs cleanly with natural topology (commit `593d1c3`). Regression test: `tests/e2e/test_orchestrator.py::TestParallelExecution::test_multi_gated_predecessor_joins_correctly`.

- [x] **Subphase context doesn't inherit parent's upstream deps** — _Stage 2a, 2026-04-17._ `_make_subphase_node` now calls `build_context(parent_phase, state)` before layering in item fields, so subphase templates see the full upstream chain. Regression test: `tests/e2e/test_dynamic.py::TestManifestFieldPropagation::test_subphase_context_inherits_parent_deps`.

- [x] **Final-phase output unreachable from downstream non-fan-out phases** — _Stage 2b, 2026-04-17._ `build_context` now synthesizes `{dep}_subphases` and `{dep}_subphase_worktrees` directly from `state.subphase_outputs` / `state.phase_worktrees`. Any downstream (final or otherwise) depending on a dynamic parent sees the same aggregate. `_make_final_phase_node` collapsed from ~25 LOC to a thin alias. Regression test: `tests/e2e/test_dynamic.py::TestManifestFieldPropagation::test_downstream_sees_subphase_aggregate`.

### Data-flow gaps

- [x] **Command phase `args` are not Jinja-templated** — _Stage 2c, 2026-04-17._ `CommandExecutor.execute` now renders each arg through `render_template(arg, context)` before building `cmd`. Plain strings pass through. `command` itself is not templated (security: keeps binary choice static). Regression tests: `tests/unit/runtime/test_command_executor.py::TestCommandExecutor::test_args_are_jinja_rendered` and `test_args_without_templating_render_literally`.
    - Separately: consider also templating `env` additions or piping dep outputs to stdin for command phases — would unlock simple Python-script "aggregator" phases.

- [ ] **Gate validators can't see dep outputs; gate-only phases have no useful signal**
    - Script gate stdin is the phase's own output (via `evaluate_gate_script(phase_output=...)`). For a `gate_only` phase the "output" is the hardcoded string `[gate-only] {id}` from `dispatch.py:48`. So a gate_only phase's validator can only match against that placeholder.
    - **Current workaround (see `examples/absurd-paper/gates/submission_check.py`):** a gate_only phase that runs _after_ a persistence phase can read disk via `$WORKDIR` (injected env var) and validate whatever's on disk there. Works when the thing you want to check has already been written to `<workdir>/<path>`. Doesn't work pre-persistence.
    - Fix: pass `state` (or at minimum `phase_outputs`) into the gate-eval context. Script gates could receive dep outputs as environment variables (like `PHASE_ID` today) or on stdin as JSON. LLM gates would need the same projection in their template context. This unlocks gate_only checkpoints that don't require a round-trip to disk.

### Observability

- [x] **Multi-dim gate `score` logged as 0.0 even when dimensions pass** — _Stage 3, 2026-04-17._ `gate_evaluated` events now source from `state.evaluations` (real evaluation records) and carry a `scores` dict with per-dimension values alongside the top-level `score`. Regression test: `tests/unit/workflow/test_logging.py::TestLogSnapshot::test_detects_multidim_gate`.

- [ ] **LLM gates inherit PromptBackend flakiness with misleading 0.0 fallback**
    - `runtime/gates.py::evaluate_gate_llm` returns `GateResult(score=0.0, feedback="gate backend error: ...")` when the backend call fails. The backend error rolls up as a gate failure (score=0.0) rather than a phase error. On a bad ACP turn, a phase with a passing output can be retried or failed purely due to gate-dispatch flake.
    - Observed in `abstract` phase across runs — same content, different LLM gate outputs (0.0 vs 0.92 dim scores) depending on whether the ACP call to the gate model returned parseable JSON.
    - Fix: distinguish between "gate eval failed" (infrastructure) and "gate scored 0.0" (content judgment). A failed gate eval should retry the GATE call, not fail the phase. Possibly: separate retry budgets for gate-eval-infra failures vs. gate-scored-low.

## Gate-evaluation extensibility

Multi-dim scoring with per-field `min` thresholds landed with the multi-dimension gate schema commit (`908a82f`). Remaining extensions:

- [ ] **Composite / weighted score expressions** — today dimensions are compared independently via per-field `min`. Next: support `{overall: weighted_sum(dims, weights)}` or a tiny expression language for cross-dim predicates (AND/OR, arithmetic). Low urgency — per-field mins cover the current demo needs.

- [ ] **Multi-tier retry escalation for fan-out + synthesis** — _infrastructure landed, Stages 3 + 3b._ Routes now accept any destination + params and can use `{field: "invocation", op: ">=", value: N}` clauses, so tiered escalation is just a longer route list (no new enum, no new retry-counter channel). Stage 3b confirmed that graph-level retry routing works for top-level gated phases; subphase retries go through inline loops within the Send-dispatched node body. Still needed: (1) expose an `evaluation:` YAML block that lets authors write custom routes directly; (2) let route destinations name ancestor nodes (cross-node re-entry via graph edges for top-level, via nested inline-loops for subphases). Trunk/merge branch for synthesis merges (`settings.trunk_ref: main`) remains unbuilt.

- [ ] **Synthesis as first-class concept**
    - Today: `final_phases` is the implicit synthesis site for dynamic subphases; regular phases chain worktrees via `{{dep_worktree}}` context
    - Make synthesis explicit: a `synthesis_phase:` block with `merges_from: [...]` listing subphase ids, blocking gate, pre-merge worktree
    - Enables: synthesis-gate blocking merge (if gate fails, changes never fold back); reset semantics for the escalation tiers above

## Forward-looking — surfaced during 2026-04-18 architecture plan

- [x] **Implicit Join + explicit JoinNode primitive** — _landed, Stage 4b._ Implicit join was already free via LangGraph's super-step semantics (multi-pred nodes naturally synchronize). Stage 4b added `execution: { type: join }` as the explicit form for author readability at fan-in points; dispatcher routes it to a no-op handler returning `ExecutionResult(success=True, output="")`. Composes with `evaluation:` (gates run against the empty join output) and downstream consumers (build_context reads the join's empty output like any other dep).

- [x] **Multi-step fan-out children** — _landed, Stage 5b
  (branch `stage-5b-execute-url`)._ Closed by the same URL-suffix
  dispatch model the rest of the orchestrator uses: a
  `fan_out.template.execute.url` ending in `.yaml`/`.yml` runs as a
  subgraph per Send branch; `.md`/`.txt`/`.prompt` as a prompt;
  `.py`/`.js`/`.sh` as a script; bare path as a binary. One field
  (`execute.url`), one rule (URL extension). No separate
  `fan_out.config:` shape needed — the `template:` block is the
  shape; the URL inside it picks the mode. Per-child subgraph e2e
  coverage in `tests/e2e/test_fan_out_subgraph.py`.

- [x] **Subgraph with defined entry/exit nodes as a first-class primitive** — _landed, Stage 4c._ A subgraph declared via `Node.config:` is loaded as a `Graph` (identical schema), recursively compiled, and added as a node in the parent via `add_node(node.id, compiled_subgraph)`. State projection across the boundary is explicit via `inputs:` / `outputs:` declarations. Reusable subgraph libraries are a real concept now: the same YAML runs both standalone and as a subgraph reference.

## Architectural moves

- [x] **Nodes as proper langgraph subgraphs** — _landed, Stage 4c._ A node with `config:` recursively compiles the referenced graph YAML and adds it as a node via LangGraph's native `add_node(name, compiled_subgraph)`. State projection is explicit (`inputs:` / `outputs:`); subgraph runs in isolation. Open questions resolved: subgraph never sees parent's full state, only what `inputs:` projects in; `{{dep}}` substitution works the same way at every level because graphs and subgraphs share one schema.

- [ ] **Flexible output contracts**
    - Glob patterns: `required_files: ["docs/*.md", "reports/**/*.pdf"]`
    - Size / non-empty checks: `{path: "out.json", min_bytes: 10}`
    - JSON-schema validation of structured outputs (replaces the removed `parse_output_as_json` silent-parse with loud validation)
    - Optional files (tracked but non-failing)
    - Templated paths resolved from dep outputs or vars
    - Forbidden files to catch leftover artifacts
    - Tree-shape constraints (e.g. "≥N files under `reports/`")

## Correctness

- [x] **Subphase quality gates with retries** — _landed, Stage 3b (branch `stage-3b-evaluation-node`)._ Subphase gates honor `max_retries` via an inline retry loop inside `_make_subphase_node`. Graph-level self-loops can't work for Send-dispatched branches (LangGraph merges branches at super-step boundaries, stripping `_subphase_item`), so the retry loop lives inside the node body. Evaluation records accumulate per-branch with real `invocation` counters; e2e test `tests/e2e/test_dynamic.py::TestDynamicGates::test_subphase_gate_triggers_retry` proves both `p::x` and `p::y` retry independently.

## Features

- [x] **Fan-out + recursive-subgraph composition** — _landed, Stage 5b
  (branch `stage-5b-execute-url`)._ A `fan_out.template.execute.url`
  ending in `.yaml`/`.yml` runs the referenced subgraph **per Send
  branch**: each manifest item drives one subgraph invocation, and the
  subgraph's terminal output flows back as that branch's
  `child_outputs[parent::item_id]`. Cycle detection walks the URL-
  reference DAG at parent compile time. Demo:
  `examples/absurd-paper/reviewer_pool` now runs draft → critique
  per reviewer via `subgraphs/single_review.yaml`. e2e coverage in
  `tests/e2e/test_fan_out_subgraph.py` (4 tests).

- [ ] **Tournament pattern — divergent fan-out + synthesizing merge**
    - Pattern: spawn N candidate solutions in parallel, each potentially with **different params/modes** (different model tier, different temperature, different persona/prompt, different provider), then synthesize: a judge picks one as the winning baseline, and a merger incorporates the strongest parts of the losing candidates into that baseline before returning the final result.
    - Today's fan-out almost gets there: a `fan_out:` block already gives N parallel branches with shared template, and `final_nodes:` already sees the aggregate `{{parent_subphases}}` map. What's missing is **per-candidate execution config** — each branch needs to be able to override its own model, temperature, prompt, agent vs prompt mode, etc., not just receive different per-item context. Today the manifest can rename the persona via `{{style}}` template substitution but can't make candidate A run on opus while candidate B runs on haiku, because `fan_out.template.execute` is a single shape applied to every branch.
    - **Schema gap to close**: let `fan_out.template.execute.params` reference manifest fields, e.g. `params.llm.model: "{{model}}"` so the manifest can supply per-candidate execution config alongside per-candidate context. Pairs with the three-axis llm config above — once `params.llm` is a recognized shape with extra="forbid", per-candidate llm config slots in cleanly.
    - **Synthesis step shape** — two final_nodes:
      1. **judge**: prompt that reads all `{{parent_subphases}}`, picks a winner, and emits structured output `{winner: "candidate_b", reasoning: "...", strengths_in_losers: [{candidate_a: "the conclusion is sharper"}, ...]}`.
      2. **merger**: prompt that reads the winner output + the strengths_in_losers list and produces the merged final output. This is the chosen winner.
    - **Why "tournament" is the right name**: the pattern fundamentally treats the divergent attempts as competing entries that get judged, not as items in a fixed manifest to process uniformly. A tournament implies a winner; fan-out is symmetric.
    - **Iteration**: optional second round where the merged output competes against the original winner — refines until score plateaus. Wraps neatly with the existing route node pattern (`when: "history['judge'][-1]['score'] >= 0.9 → __end__; else: tournament"`).
    - **Demo to deliver alongside**: a small `examples/tournament/` workflow generating 3 alternative drafts of the same paragraph (one with sonnet+formal, one with sonnet+playful, one with haiku+terse), judging which lead reads best, and merging the winning lead with the better-detail-paragraphs from the losers.
    - **Pairs with**: per-node llm config (so per-candidate `params.llm` works); scope-aware settings (so a tournament-as-subgraph inherits sensibly); `add_messages` reducer (for in-phase refinement loops if iterating).

- [ ] **Output caching / skip-if-unchanged**
    - Make-style incrementality (not provided by langgraph checkpointers)
    - Skip when `required_files` still exist and input fingerprint (dep outputs + prompt hash + vars) matches
    - New `cache: bool` field on `Phase`, fingerprint persisted alongside state

- [ ] **CLI variable overrides**
    - `abe-froman run --var key=value` (repeatable)
    - `{{vars.key}}` namespace in prompt templates
    - Optional `${var}` substitution in YAML at config-load time

- [ ] **Conditional phases (`run_if`)**
    - Pre-execution skip on a predicate over `phase_outputs` / env / vars
    - Compiles to a conditional edge at phase entry
    - Distinct from `QualityGate` with `blocking: false`, which only skips dependents _after_ execution

- [ ] **Workflow cancellation**
    - `asyncio.CancelledError` handling in `runtime/runner.py`
    - Propagate to executors, persist partial state, clean up ACP subprocesses

- [ ] **`abe-froman status` / `dump-state`**
    - Pretty-print persisted state: completed/failed phases, retry counts, gate scores, token usage
    - Works against state file or a langgraph checkpointer if adopted

## Refactoring

- [ ] **Unified `ExecutionResult` type**
    - Merge `PhaseResult` + `PromptBackendResult` (overlapping `output`, `structured_output`, `tokens_used`)
    - Document "executor owns retry policy, backend owns transport"

- [ ] **State shape cleanup**
    - Group phase data into `phases: dict[str, PhaseRunData]` with one merge reducer
    - Document `_subphase_item` as an explicit transient channel
    - Split `PhaseState` (phase-visible) from `WorkflowState` (runner-level)

## Execution engines

### Backend-selection ergonomics (high priority)

- [x] **Scope-aware settings resolution (prerequisite for everything below)** — _landed post-Stage-5b. `runtime/settings_merge.merge_settings` uses Pydantic v2 `model_fields_set` so child YAML's explicit fields win, parent's flow through. `NodeExecutor.execute` Protocol gained `settings_override: Settings | None`; threaded through `DispatchExecutor` (every read site of `self._settings`), `ForemanExecutor` (per-model semaphore selection), and `PromptExecutor` (`apply_preamble`, `execute_rendered.model_downgrade_chain`). Compile layer: `build_workflow_graph(effective_settings=)`, `_make_execution_node`, `_make_evaluation_node`, `run_evaluation_and_outcome`, fan-out factories all take and propagate. `make_subgraph_node` and `make_fan_out_subgraph_invoker` accept `parent_settings` and call `compile_fn(..., effective_settings=merge_settings(parent, sub))`. Tested by 10 unit tests + 6 e2e (incl. real-subprocess `default_timeout` proof: subgraph override lets `sleep 1.5` pass under parent's 1s; reverse polarity kills `sleep 2`). Critical bug caught en route — the inner `compile_fn` closure was forwarding the OUTER scope's settings into recursive build calls instead of the inner scope's; the artifact-driven sleep test pinned it._

- [x] **Default executor should be real, not stub** — _landed post-Stage-5b. `auto_detect_executor()` in `factory.py` walks `ANTHROPIC_API_KEY` (placeholder) → DeepSeek key (env or `~/.pi/agent/auth.json`) → `npx` on PATH → `stub` with a `UserWarning` naming concrete remediation. `Settings.executor: str | None = None` (was `"stub"`); the CLI does `executor or settings.executor or auto_detect_executor()`. Explicit `-e stub` or `executor: stub` in YAML never triggers the fallback warning — stub stays usable for offline testing, just no longer the default. Manual artifact gate: `abe-froman run examples/jokes/workflow.yaml` (no `-e` flag) now auto-picks DeepSeek (since the key is on disk) and produces real output (not `[prompt-stub]`)._

- [ ] **Three orthogonal axes for LLM execution, configurable in YAML**
    - Depends on scope-aware settings resolution above — without it, a subgraph's `settings.llm:` block would silently lose to the parent's, which is exactly the footgun the resolution-order fix exists to close.
    - Today's `settings.executor: "stub" | "acp"` collapses three independent decisions into one enum. Splitting them lets workflows declare their interaction model at authoring time and lets per-node overrides exist.
    - **Axis 1 — Interaction mode**: `agent` (multi-turn, tool-using session like Claude Code via ACP) vs `prompt` (single-shot completion via API). Same `{{var}}` template; different runtime semantics — agents can read/write files, run tools, take multiple turns; prompts return one response and exit.
    - **Axis 2 — Protocol/transport**: `acp` (subprocess + stdio JSON-RPC), `api` (HTTP via SDK), `stub` (no network). Today this is conflated with axis 1 because the only `agent` option ships over ACP and the only `api` option is hypothetical.
    - **Axis 3 — Provider/model**: `anthropic+sonnet`, `anthropic+opus`, `openai+gpt-4`, `local+llama-3.3` via Ollama, etc. Today `settings.default_model` only picks Claude tiers and is implicitly tied to whatever `executor` decided.
    - Schema sketch (workflow-level defaults + per-node override):
      ```yaml
      settings:
        llm:
          mode: agent            # default for prompt nodes
          protocol: acp          # default transport
          provider: anthropic
          model: sonnet
      nodes:
        - id: research
          execute:
            url: prompts/research.md
            params:
              # Per-node override: this one wants the cheap fast prompt-and-response,
              # not a full agent session
              llm:
                mode: prompt
                provider: anthropic
                model: haiku
      ```
    - Mode selection drives backend wiring: `mode=agent + protocol=acp` → ACPBackend; `mode=prompt + protocol=api + provider=anthropic` → AnthropicBackend; `mode=prompt + protocol=api + provider=openai` → OpenAIBackend.
    - Per-node `params.llm` lives inside `PromptParams` (already extra="forbid" so typos surface loudly).

### Backends to add (lower priority once axes above land)

- [x] **Direct Anthropic API backend** — _landed alongside StubBackend removal._ `runtime/executor/backends/anthropic.py` (~160 LOC) implements `PromptBackend` via `AsyncAnthropic`. Generic model alias table (`sonnet` / `opus` / `haiku` → vendor IDs) with pass-through for explicit pins. `OverloadError` mapping for transient failures (status 429 / 502 / 503 / 504 / 529 + class-name fallback `RateLimitError` / `APIConnectionError` / `APITimeoutError` / `InternalServerError` / `OverloadedError`). Optional dep — install with `uv sync --extra anthropic`. Auto-detect picks it first; explicit `--executor anthropic` always wins. Token-count surfacing on `ExecutionResult` is still TODO.

- [x] **OpenAI-compatible backend** — _landed post-Stage-5b. `runtime/executor/backends/openai.py` (~105 LOC) implements the `PromptBackend` Protocol via the `openai` SDK with overridable `base_url`. Validated end-to-end against DeepSeek (`base_url=https://api.deepseek.com/v1`, model `deepseek-v4-flash`) — auto-detect picks it up via `~/.pi/agent/auth.json`. Maps 429/502/503/504/529 + `RateLimitError` + `APIConnectionError` → `OverloadError` (activates the existing model-downgrade chain). Optional dep — install with `uv sync --extra openai`. Three-axis LLM config sketch above is still TODO; this backend is the first concrete demo that decouples provider/model from the ACP transport. Unlocks OpenAI, Azure OpenAI, Ollama, vLLM, llama.cpp, LM Studio, LiteLLM via the same backend with `base_url` overrides._

- [ ] **Wire `OverloadError` through `ACPBackend`**
    - Translate ACP 429 / 529 / overload codes so the existing downgrade path fires with ACP too

- [ ] **Streaming on `PromptBackend`**
    - Optional `async stream_prompt(...) -> AsyncIterator[str]`
    - Live progress in CLI and JSONL log

## Langgraph adoption wins

- [ ] **`Command` objects for node-level routing** — paired with the Evaluation/Decision split (top of file). A node returns `Command(update=..., goto=...)` instead of writing state and being routed by a downstream conditional edge. Removes router closures across `compile/graph.py` and makes the topology self-describing — the destination lives in the node return, not in a separate function reading state we just wrote.

- [x] **`stream_mode="updates"` in runner + logging** — _delivered._ `runtime/runner.py` now drives logging from `astream(stream_mode=["updates", "values"])` (tuple form supported in LangGraph 1.0.7); `compile/subgraph.py` switches its two `astream` call sites the same way. `JsonlLogger.log_snapshot(prev, curr)` was replaced with `log_update(node_name, update)`; `_PrefixingProxy` deleted (the prefix is now applied to `node_name` before dispatch). The 6 emitted event types and their schemas are unchanged for downstream consumers.

- [ ] **Interrupts / human-in-the-loop** — `interrupt()` + `Command(resume=...)` from langgraph. Free on the existing checkpointer; enables author/operator approval nodes, manual quality gates, draft review. New execution type (`type: human_review`) or `evaluation.mode: human` schema option.

- [x] **Subgraphs with declared entry/exit** — _landed, Stage 4c (no separate execution type)._ User clarified during planning: graphs and subgraphs are definitionally identical, so a node references another graph YAML via `config:` rather than getting tagged as a `subgraph` type. Recursion falls out naturally. See "Nodes as proper langgraph subgraphs" above.

- [ ] **`add_messages` reducer for in-phase refinement loops** — multi-turn draft → critique → revise within a single phase using LangGraph's native message-list reducer. Phase-local `messages` channel; no ACP round-trip per turn for pure model revision.

- [ ] **Time-travel replay in CLI** — `abe-froman replay <thread-id> --from <checkpoint>`. Checkpointer already persists every super-step; we just don't expose it. Enables A/B of executor changes against the same past state, bisecting regressions, reproducing flakes.

- [ ] **Static breakpoints** — `compile(interrupt_before=[...], interrupt_after=[...])`. Pairs with `--break-before <node>` / `--break-after <node>` CLI flags for step-through debugging of production workflows.

- [ ] **`ToolNode` as a new execution type** — when a phase should hand the model a tool list and have LangGraph route tool calls natively, rather than running a single prompt through ACP. New `execution: { type: tool, tools: [...] }`.

- [ ] **`BaseStore` for cross-run memory** — distinct from checkpointer (per-thread). Shared memory across workflow runs — e.g., "last week's gate was lenient, tighten this week." Optional store wired alongside `AsyncSqliteSaver`.

- [ ] **`RetryPolicy` for transport-level retries** — layer `RetryPolicy(max_attempts=N, retry_on=OverloadError)` on executor-invoking nodes. Complements our eval-score-driven semantic retries; separates infrastructure flakes (rate limits, ACP drops) from content judgment. Closely related to "LLM gates inherit PromptBackend flakiness" above — fixes the same class of bug from a different angle.

## Stage 5c — inline route (delivered + deferred)

- [x] **Inline `Node.route` forward-edge dispatch** — _landed,
  Stage 5c (branch `feat/inline-route`)._ `Route` is a first-class
  block on `Node`; goto-shorthand (`goto: <str | list[str]>`) and
  conditional ladder (`cases: + else:`) shapes. Compiles to
  `Command(goto=...)` (scalar) or `Command(goto=[...])` (list →
  static fan-out via LangGraph 1.x native multi-edge). Standalone
  form (route + no execute) replaces the legacy
  `execute: { type: route, ... }`; synthetic post-execute form
  (route + execute) registers a `_route_<id>` dispatcher fired
  after the execute body (and eval, if present). `migrate.py`
  lifts old YAML automatically. `EvaluationResult.reasons` (per-
  dimension `<dim>_reason` capture) and `build_eval_preamble`
  (neutral structural formatter, no "failed" framing) live in
  `runtime/gates.py` and feed both the same-node retry path
  (via `inject_retry_reason` → `{{_retry_reason}}`) and the
  inline-route goto path (via synthetic dispatcher →
  `state._route_eval_preamble` → `_dispatch_prompt` auto-prepend).
  Sender bindings (`{{sender_id}}`, `{{sender}}`,
  `{{sender_structured}}`, `{{sender_worktree}}`) and the
  `{{evals}}` always-on global are surfaced by `build_context`.
  Route namespace adds `evals[id]`, `passed(id)`, `score(id)`,
  `scores(id)` helpers (`compile/route.py::build_safe_funcs`).
  760 tests green; example `examples/pipeline_style/workflow.yaml`
  demonstrates pipeline-style forward-edge authoring.

### Deferred from Stage 5c

- [ ] **Per-case `params.inputs` projection** — let a route case
  shape the goto target's input bindings beyond the always-on
  sender vars. Symmetric with subgraph `params.inputs`. Today
  every goto target sees the same sender/eval bindings.

- [ ] **Route inside fan-out child template** — currently forbidden
  by the `compile/dynamic.py` composition rules (per-Send branch
  loses `_fan_out_item` at any conditional-edge boundary).
  Resolving would require either (a) inline route dispatch within
  the per-Send body without a graph-level edge crossing, or (b)
  per-branch state-preservation across the boundary.

- [ ] **`passes:` / `fails:` shorthand on Evaluation** — declarative
  ladder form for the common "if pass go here, if fail go there"
  shape, sugar over an inline `route:` with `passed(id)` predicates.
  Lower priority — the helper-function form is already concise.

- [ ] **`_route_sender` cleanup question** — last-write-wins state
  field. Templates that reference `{{sender_id}}` should guard with
  `{% if sender_id %}` if they could be reached via static edge AFTER
  an inline-route hop earlier in the same workflow (rare topology).
  Open question: scope `_route_sender` to the goto target only
  (clear after consumption), or document the guard as the canonical
  pattern? Current state: no auto-clear; the field persists until
  another Command emission overwrites it.

## Stage 5a hooks (deferred from the route-node design)

These are forward-looking items surfaced during Stage 5a planning;
landed alongside or after Stage 5c's `evaluation:`-block desugaring
unless flagged otherwise.

- [ ] **Multi-target parallel fan-out as a general primitive** — `goto:
  [a, b, c]` returns from any node that decides flow (LangGraph
  supports list-return conditional edges natively). Not route-specific:
  applies to evaluation routers too, and could subsume some `fan_out:`
  cases. Compatible with the existing `Command(goto=...)` API.

- [ ] **Output specification unification** — one `output:` field on
  Node taking `schema` | `contract` | (none). Today `output_contract:`
  is free-floating. Folding the three modes under one field makes them
  symmetric and pairs with the `schema:` work below.

- [ ] **Schema enforcement at backend boundary** — `ACPBackend` and
  stub backends populate `ExecutionResult.structured_output` when a
  Node has `schema:` set. The field exists end-to-end already; today
  no backend writes to it. Unblocks Stage 5b-style "route on producer
  output without going through an evaluate gate."

- [ ] **Schema-first templates** — `{{judge.score}}` resolves against
  structured outputs; `{{judge}}` falls back to raw string. Pairs with
  schema enforcement above. Today templates are flat string
  substitution.

- [ ] **Schema sources** — inline JSON schema dict OR `schema_file:`
  path OR `schema_class: my_module.GateScore` for Pydantic. Three
  shapes for one concept; symmetric with how `validator:` accepts
  .py/.js/.md.

- [ ] **Per-node delay primitive** — wrapping concern (orthogonal to
  route) for backoff between attempts when authoring retry-via-route
  patterns. Today `settings.retry_backoff` is the only knob and it's
  coupled to evaluation-driven retries.

- [ ] **Goto-target reachability validation** — schema validator
  rejects `route → ship` configurations where `ship` is also reached
  by a static dep edge from somewhere else (silent double-firing). Lo
  priority — currently the runtime simply double-runs the target,
  which is observable but ugly.

## Reimplementation debt (drop in favor of native LangGraph)

Audit of where we shadow LangGraph functionality. Most of our code is genuinely complementary (timeouts, semantic retries, concurrency caps, custom reducers) — these two items are not.

- [x] **Stop diffing state in `runtime/logging.py`** — _delivered._ Snapshot-compare path deleted. Events key directly on the `node_name → update` pairs the stream emits. Removed `_PrefixingProxy` along with `log_snapshot`; the new `log_update(node_name, update)` is shared by `JsonlLogger` and `SubgraphLogger` via a free helper.
- [ ] **Stop hand-writing router closures** — pairs with `Command` objects above. Delete `_make_evaluation_router`, `_subphase_id_resolver`, the dynamic-router closure, and the conditional-edge scaffolding they feed in `compile/graph.py`. Decision nodes return `Command(goto=...)` directly.

**Not reimplementation** (clarified during audit, kept for reference):

- Eval-score-driven retries (ours) vs `RetryPolicy` (exception-driven) — complementary, not duplicative.
- `_merge_dicts` / `_merge_evaluations` reducers — LangGraph offers no dict-merge or per-key list-append natively.
- Timeouts (`asyncio.wait_for`), concurrency caps (`asyncio.Semaphore`), worktree pool — outside LangGraph's scope.
- Thread ID derivation from `(workflow_name, workdir)` — policy choice, not a feature we shadow.
