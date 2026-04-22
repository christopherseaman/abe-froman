# Wishlist

- [ ] **Documentation**
    - README with project overview, usage, and functionality
    - TECHNICAL.md with layout/breakdown of implementation

## Simplification candidates (surfaced by 2026-04-17 refactor-done review)

- [ ] **Unify gate-eval via outcome-as-routing-signal** — _partially landed, Stage 3._ Top-level phases now use the data-driven model: `compile/evaluation.py` introduces `Criterion {field, op, value}`, `Route {when, to, params}`, `walk_routes` (first-match-wins), and `gate_to_routes(QualityGate, max_retries)` compiles the DSL sugar. `classify_gate_outcome` walks routes instead of running if/elif, and `state.evaluations: {node_id: [EvaluationRecord]}` records full history. 36 new unit tests in `tests/unit/compile/test_evaluation.py`. **Still open:** the subphase gate block in `compile/dynamic.py:100` still calls `evaluate_gate` directly, writes to `gate_scores`/`gate_feedback` (not `state.evaluations`), and hardcodes `attempt_number=1`. Stage 3b (the two-LangGraph-node split — Execution + Evaluation pair — applied to subphases as well) is what actually closes this item.

- [ ] **Collapse `runtime/executor/backends/` → `runtime/backends/`** — 4-level nesting (`runtime/executor/backends/acp.py`) for 4 small files. Semantic loss: current nesting signals that only `PromptExecutor` uses backends. If we land the anthropic/openai backends (below), the signal still holds but less strongly — multiple executor types might route through one backends/ module. Low value, low risk; defer until a second executor family justifies the flattening.

- [ ] **Fold `compile/dynamic.py` into `compile/nodes.py`** — 182 LOC would bring `nodes.py` to ~530 LOC. The split is defensive today: `_make_subphase_node` has legitimately divergent semantics (no dep check, no output contract, no retry routing). **Worth revisiting after** the gate-eval unification above — if the gate block is gone and the final remaining divergence is "Send-triggered vs. normal-invocation," the split stops earning its keep.

- [ ] **Move `_detect_cycles` + `_find_terminal_phases` → `schema/models.py`** — topology validation belongs with the config model. Blockers: `schema/` is currently langgraph-free Pydantic-only; moving these functions in would require no imports from `langgraph`, which they already don't have. Clean move. Low priority — they're stable and small.

## Test doctrine cleanup

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

- [ ] **ACP process-tree leaks / zombie subprocesses under long runs**
    - Symptoms: after repeated ACP phases (especially at `max_parallel_jobs > 1`), user-session RSS grows and `node` / `claude` PIDs linger after `ACPBackend.close()` — contributed to the OOM kill observed on host `carnac` 2026-04-16 06:51:33 (`user@1002.service: The kernel OOM killer killed some processes`) on a no-swap VM with `ManagedOOMMemoryPressureLimit=50%`
    - Contributing factor: Python 3.14 `aclose()` warning from the ACP SDK — async-generator cleanup does not reliably reap the `npm exec → node → claude` child tree
    - Investigate: teardown ordering in `ACPBackend.close()`; use a process group or `PR_SET_PDEATHSIG` so the whole tree dies when the Python parent exits; SIGTERM → wait → SIGKILL escalation
    - Add a teardown assertion (test fixture or runtime check) that no ACP-spawned PIDs survive backend close
    - Decide: fix in-backend, or ship host-level mitigation (swap + relaxed oomd) as the supported recommendation

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
    - **Current workaround (see `examples/absurd-paper/gates/submission_check.py`):** a gate_only phase that runs *after* a persistence phase can read disk via `$WORKDIR` (injected env var) and validate whatever's on disk there. Works when the thing you want to check has already been written to `<workdir>/<path>`. Doesn't work pre-persistence.
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

- [ ] **Multi-tier retry escalation for fan-out + synthesis** — _infrastructure landed, Stage 3, 2026-04-17._ Routes now accept any destination + params and can use `{field: "invocation", op: ">=", value: N}` clauses, so tiered escalation is just a longer route list (no new enum, no new retry-counter channel). Still needed: (1) expose an `evaluation:` YAML block that lets authors write custom routes directly; (2) let route destinations name ancestor nodes (requires the Stage 3b graph-node split to properly re-enter upstream phases). Trunk/merge branch for synthesis merges (`settings.trunk_ref: main`) remains unbuilt.

- [ ] **Synthesis as first-class concept**
    - Today: `final_phases` is the implicit synthesis site for dynamic subphases; regular phases chain worktrees via `{{dep_worktree}}` context
    - Make synthesis explicit: a `synthesis_phase:` block with `merges_from: [...]` listing subphase ids, blocking gate, pre-merge worktree
    - Enables: synthesis-gate blocking merge (if gate fails, changes never fold back); reset semantics for the escalation tiers above

## Forward-looking — surfaced during 2026-04-18 architecture plan

- [ ] **Implicit Join (QoL, after Evaluation-as-node refactor lands)** — when that refactor lands, JoinNode exists as an internal primitive emitted whenever a phase has >1 predecessor. Next QoL step: make it implicit for any node with multiple incoming edges, so authors never have to think about join semantics. JoinNode stays available as a primitive for power users; 99% of workflows get correct join behavior for free.

- [ ] **Multi-step subphase sequences inside fan-out** — today `dynamic_subphases.template` is a single `prompt_file`; each fan-out item traverses exactly one execution node before gather. There's no way to say "for each item, run step A → evaluate → step B → gather." Natural fit for the EvaluationNode world (step-A's eval routes into step-B, then B routes into gather), but requires DSL surface: probably `template: [phase-A, phase-B, ...]` or a new `subgraph:` key. Closely related to the subgraph-primitive item below.

- [ ] **Subgraph with defined entry/exit nodes as a first-class primitive** — related to multi-step subphase above, and to the existing "Phases as proper langgraph subgraphs" item. A subgraph declares its **entry** node (what upstream routes into) and **exit** node (what routes to downstream); the body can be any shape. Dynamic subphases become "fan out N calls into the subgraph's entry, gather at exit." Reusable subgraph libraries become a real concept. Requires: schema for declaring entry/exit, context projection rules across the boundary, and deciding whether subgraphs get their own checkpointer / state scope.

## Architectural moves

- [ ] **Phases as proper langgraph subgraphs**
    - Each `Phase` compiles to a compiled `StateGraph` used as a node in the parent, replacing the flat expansion in `compile/nodes.py`
    - Enables encapsulation, reusability, nested workflows, cleaner retry semantics, explicit parent/child state boundary
    - Dynamic subphases collapse to "spawn N instances of the subgraph"
    - Open questions: state projection vs. full sharing, reducer composition at boundary, how `{{dep}}` template substitution resolves across subgraph boundaries

- [ ] **Flexible output contracts**
    - Glob patterns: `required_files: ["docs/*.md", "reports/**/*.pdf"]`
    - Size / non-empty checks: `{path: "out.json", min_bytes: 10}`
    - JSON-schema validation of structured outputs (replaces the removed `parse_output_as_json` silent-parse with loud validation)
    - Optional files (tracked but non-failing)
    - Templated paths resolved from dep outputs or vars
    - Forbidden files to catch leftover artifacts
    - Tree-shape constraints (e.g. "≥N files under `reports/`")

## Correctness

- [ ] **Subphase quality gates with retries**
    - Subphase gates currently use the Stage 3 record-only route (`when: []` → continue). Retries aren't wired because `_make_subphase_node` doesn't yet emit the execution+evaluation node pair (that's Stage 3b).
    - Unblocked by: splitting subphase compilation to match the top-level Execution + Evaluation pattern, at which point routes with `invocation` clauses give retries for free.
    - CLAUDE.md known limitation #3.

## Features

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

- [ ] **Direct Anthropic API backend**
    - `runtime/executor/backends/anthropic.py` using the `anthropic` SDK — no ACP process
    - Removes ACP as a hard runtime dependency
    - Map 429 / 529 / rate-limit to `OverloadError` (activates the dormant model-downgrade path in `runtime/executor/prompt.py`)
    - Expose input/output token counts via `PromptBackendResult.tokens_used`
    - `settings.executor: "anthropic"`

- [ ] **OpenAI-compatible backend**
    - `runtime/executor/backends/openai.py` using the `openai` SDK with configurable `base_url`
    - Unlocks OpenAI, Azure OpenAI, Ollama, vLLM, llama.cpp, LM Studio, LiteLLM
    - Separate model-downgrade chain
    - `settings.executor: "openai"` + `settings.openai_base_url`

- [ ] **Wire `OverloadError` through `ACPBackend`**
    - Translate ACP 429 / 529 / overload codes so the existing downgrade path fires with ACP too

- [ ] **Streaming on `PromptBackend`**
    - Optional `async stream_prompt(...) -> AsyncIterator[str]`
    - Live progress in CLI and JSONL log

## Langgraph adoption wins

- [ ] **Interrupts / human-in-the-loop**
    - `interrupt()` + `Command(resume=...)` from langgraph
    - Free once on a checkpointer (which now exists); enables "pause for operator approval, resume"
