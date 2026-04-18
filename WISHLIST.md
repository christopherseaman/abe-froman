# Wishlist

- Documentation!
    - README with project overview, usage, and functionality
    - TECHNICAL.md with layout/breakdown of implementation

## Simplification candidates (surfaced by 2026-04-17 refactor-done review)

- **Unify gate-eval via outcome-as-routing-signal** — today `compile/nodes.py::evaluate_gate_and_outcome` (regular phases, full retry routing) and the inline gate block in `compile/dynamic.py::_make_subphase_node` (L91-123, record-only) are parallel implementations of "score → classify → update." Elegant fix: make the gate evaluator pure-shared and emit a `GateOutcome` enum (`pass | retry | fail_blocking | warn_continue | record_only | escalate`). Each node type owns a **router** that interprets the outcome against what's semantically valid for its position:
    - Regular-phase router: `{pass → continue, retry → self-loop, fail_blocking → END, warn_continue → continue}`
    - Subphase router (fan-out): every outcome → continue (record score; retry routing is meaningless inside a `Send`-fanned leaf)
    - Synthesis router (future, ties to multi-tier retry): `{pass → continue, retry → self, escalate → parent fan-out, super_escalate → upstream}`
    - Gate-only phase router: `{pass → continue, fail_blocking → END, warn_continue → continue}` (no execution → no retry semantics)
    - Drops ~33 LOC of duplicated `evaluate_gate` → `asyncio.wait_for` → `gate_feedback` update in `dynamic.py`. Cleans up WISHLIST item "Subphase quality gates with retries" by making it a router change, not a logic rewrite.
    - Also cleans up the current `outcome: str` string literal convention in `classify_gate_outcome` (nodes.py:159-176) — a proper enum catches typos at type-check time.

- **Collapse `runtime/executor/backends/` → `runtime/backends/`** — 4-level nesting (`runtime/executor/backends/acp.py`) for 4 small files. Semantic loss: current nesting signals that only `PromptExecutor` uses backends. If we land the anthropic/openai backends (below), the signal still holds but less strongly — multiple executor types might route through one backends/ module. Low value, low risk; defer until a second executor family justifies the flattening.

- **Fold `compile/dynamic.py` into `compile/nodes.py`** — 182 LOC would bring `nodes.py` to ~530 LOC. The split is defensive today: `_make_subphase_node` has legitimately divergent semantics (no dep check, no output contract, no retry routing). **Worth revisiting after** the gate-eval unification above — if the gate block is gone and the final remaining divergence is "Send-triggered vs. normal-invocation," the split stops earning its keep.

- **Move `_detect_cycles` + `_find_terminal_phases` → `schema/models.py`** — topology validation belongs with the config model. Blockers: `schema/` is currently langgraph-free Pydantic-only; moving these functions in would require no imports from `langgraph`, which they already don't have. Clean move. Low priority — they're stable and small.

## Test doctrine cleanup

- **Resolve MemoryBackend / ErrorBackend / SleepyBackend / TrackingBackend policy conflict** — `tests/unit/runtime/test_prompt.py` has `MemoryBackend` + `ErrorBackend` used by ~14 orchestration tests; `tests/unit/runtime/test_foreman.py::TestPerModelBackpressure` has `SleepyBackend` + `TrackingBackend`. All four are hand-written Protocol doubles that strict reading of `feedback_no_fake_backends.md` forbids. They instrument `PromptExecutor` / `ForemanExecutor` orchestration (template, preamble, timeout, token threading; per-model concurrency caps) — NOT Claude behavior — so the strict interpretation may be wrong.
    - Three options (detailed at `/home/christopher/.claude/plans/memory-backend-policy.md`):
        1. Extend `StubBackend` with `record=True` to produce one sanctioned recording path; migrate all doubles to it.
        2. Amend the policy memo to permit orchestration-testing doubles, making the existing code compliant.
        3. Move ~14 tests to `tests/acp/` and accept weaker assertions against real Claude.
    - **Recommended: (1) + (2) together** — one sanctioned recording path, policy clarifies the distinction between Claude-behavior simulation (forbidden) and orchestration instrumentation (permitted, via `StubBackend(record=True)` only).

## Top priority after simplification refactor

- Consider simpler dependency/ordering model. Possibly no more phases, just nodes and edges with next node selection informed by node completion criteria. QualityGate just becomes a node type and retry with context is the route chosen within a "failure context". Not sure this is actually simpler but having retry, escalated, and super-escalated is definitely NOT the correct path forward.
- **ACP test flakiness**
    - `tests/acp/` tests fail or pass sporadically
    - Investigate: session lifecycle races, stdio buffering, stale `_session_id` on multi-prompt flows, Python 3.14 async-generator `aclose()` warning
    - Decide: fix root cause, or gate ACP tests behind a stricter pre-flight

- **ACP process-tree leaks / zombie subprocesses under long runs**
    - Symptoms: after repeated ACP phases (especially at `max_parallel_jobs > 1`), user-session RSS grows and `node` / `claude` PIDs linger after `ACPBackend.close()` — contributed to the OOM kill observed on host `carnac` 2026-04-16 06:51:33 (`user@1002.service: The kernel OOM killer killed some processes`) on a no-swap VM with `ManagedOOMMemoryPressureLimit=50%`
    - Contributing factor: Python 3.14 `aclose()` warning from the ACP SDK — async-generator cleanup does not reliably reap the `npm exec → node → claude` child tree
    - Investigate: teardown ordering in `ACPBackend.close()`; use a process group or `PR_SET_PDEATHSIG` so the whole tree dies when the Python parent exits; SIGTERM → wait → SIGKILL escalation
    - Add a teardown assertion (test fixture or runtime check) that no ACP-spawned PIDs survive backend close
    - Decide: fix in-backend, or ship host-level mitigation (swap + relaxed oomd) as the supported recommendation

- **Worktree garbage collection**
    - Today: `ForemanExecutor` never removes trees under `<workdir>/.abe-foreman/` — disk + inodes accumulate indefinitely across runs
    - CLI: `abe-froman worktree list` — table of (phase_id, created, last_used, size, branch)
    - CLI: `abe-froman worktree prune [--older-than 7d] [--phase <id>] [--dry-run]` — `git worktree remove` + directory delete, with safety checks for uncommitted changes
    - Optional auto-GC: `settings.cleanup_worktrees_on_success: bool` — prune at `workflow_end` only when the final state is all-completed (preserve on partial failure so users can inspect)
    - Must preserve the across-retries reuse that foreman relies on (keys trees by `phase_id`); GC runs against _completed_ workflow threads only

## Observed during 2026-04-18 complex-demo build (examples/absurd-paper/)

Building the 13-phase demo surfaced issues not previously cataloged. Kept here as a group so their cross-relationships stay visible.

### ACP reliability

- **Write tool with `../../`-traversing paths hangs indefinitely**
    - Symptom: a minimal `persist` phase whose sole job was `Write(path="../../paper/paper.md", content="...")` timed out at 180s with no output, no error, no file written. Same behavior with `Bash` + heredoc.
    - The worktree contains no partial file; the staging dir (`<workdir>/paper/`, pre-scaffolded) stays empty. Claude is not refusing — no text response is returned at all; the ACP session just stalls.
    - Repro: any prompt phase asking Claude Code via ACP to Write or Bash-write to a path outside the session's apparent workdir (`cwd` = foreman worktree). Absolute paths may or may not behave differently — untested in this session.
    - Investigate: whether claude-code-acp enforces a path-allowlist silently; whether there's a permission dialog waiting that our auto-approver doesn't recognize; whether `_send_lock` is hiding a hang in the dispatch path.
    - Workflow-author workaround today: avoid Write/Bash to non-workdir paths; pass state via text outputs only. This blocks the documented "author-written merge phase" pattern in CLAUDE.md.

- **`acp.exceptions.RequestError: Internal error` appears under concurrent LLM calls even with `_send_lock` in place**
    - Stack trace fires from `acp/connection.py:237` via `acp/task/dispatcher.py:81`. Observed under `max_parallel_jobs=2` + `per_model_limits.sonnet=2` while the ACP backend serializes `send_prompt` via `_send_lock`.
    - Phases still complete in these runs — the error is logged but recovery happens somewhere. Suggests the SDK is raising and the dispatcher is retrying or dropping.
    - Needs root-cause diagnosis. Possibly related to background tasks per the supervisor traceback, or to in-flight session state while a new prompt arrives.

### Orchestrator join semantics

- **Multi-gated-predecessor join bug**
    - Gated phases use `add_conditional_edges` — the router emits ALL dependents on completion. If phase Y depends on multiple gated phases [X, Z], Y fires on whichever of {X, Z} completes first, with the other's context empty (`{{z}}` renders as "" by Jinja default). Y proceeds, hangs/fails, and LangGraph doesn't re-fire it when Z eventually completes.
    - Observed in `examples/absurd-paper/` before I restructured: reconcile depended on [abstract, intro, methods, results, discussion]; abstract (gated) completed first; reconcile ran with empty diamond inputs and timed out.
    - Workflow-author workaround: keep at most ONE gated predecessor per multi-dep phase. Push shared upstream content INTO the one gated predecessor's output (e.g., outline's JSON now includes the abstract verbatim).
    - Framework fix: either make gate routers emit via regular edges where possible, or have `_make_phase_node` detect missing deps and no-op / defer. Ties into the "Unify gate-eval via outcome-as-routing-signal" item above — the router design is the crux.

- **Subphase context doesn't inherit parent's upstream deps**
    - `compile/dynamic.py::_make_subphase_node` builds context as `{parent_phase.id: parent_output, **item_fields}` only. Upstream deps of the parent are not projected. So if the parent depended on `reconcile`, subphases cannot see `{{reconcile}}`.
    - Forced the workaround of embedding the paper summary in each manifest item, duplicating content across items.
    - Fix: project the parent's own `build_context` results into each subphase context, so subphases behave like direct children of the parent's deps. Small src change in `_make_subphase_node`.

- **Final-phase output unreachable from downstream non-fan-out phases**
    - `build_context` in `compile/nodes.py` only projects `{dep}`, `{dep}_structured`, `{dep}_worktree` for `phase.depends_on`. The `{dep}_subphases` / `{dep}_subphase_worktrees` synthetic keys only exist inside the final-phase context — they are not persisted to state.
    - Consequence: a phase P that depends on a dynamic-subphase parent X fires AFTER X's last final_phase (via `exit_node` wiring) but has no access to any of X's fan-out results — only X's own text output (usually the manifest JSON).
    - Current escape hatch: make P one of X's `final_phases` instead, so it gets the aggregated context. That forces downstream chaining to nest inside final_phases, which is not how most workflows want to compose.
    - Fix: persist `{parent_id}_subphases` and `{parent_id}_subphase_worktrees` to state after fan-out completes. Then any downstream depending on the parent can use them in its template.

### Data-flow gaps

- **Command phase `args` are not Jinja-templated**
    - `runtime/executor/command.py:25` constructs `cmd = [phase.execution.command, *phase.execution.args]` with no substitution. Command phases therefore cannot reach `{{dep}}` / `{{dep_worktree}}` in their arguments.
    - Blocks the canonical "merge" pattern from CLAUDE.md (a `cp`/`git merge-file` command phase that consumes an upstream worktree path).
    - Fix: render `command` + `args` through `render_template` with the same context prompt phases get. Minor change; may need to decide escaping for arguments with spaces/quotes.
    - Separately: consider also templating `env` additions or piping dep outputs to stdin for command phases — would unlock simple Python-script "aggregator" phases.

- **Gate validators can't see dep outputs; gate-only phases have no useful signal**
    - Script gate stdin is the phase's own output (via `evaluate_gate_script(phase_output=...)`). For a `gate_only` phase the "output" is the hardcoded string `[gate-only] {id}` from `dispatch.py:48`. So a gate_only phase's validator can only match against that placeholder.
    - This tanked the originally-planned `integrity_check` gate_only — it wanted to validate word count of `word_count`'s output, but couldn't see it.
    - Fix: pass `state` (or at minimum `phase_outputs`) into the gate-eval context. Script gates could receive dep outputs as environment variables (like `PHASE_ID` today) or on stdin as JSON. LLM gates would need the same projection in their template context.

### Observability

- **Multi-dim gate `score` logged as 0.0 even when dimensions pass**
    - `build_gate_outcome_update` writes `GateResult.score` into `gate_scores[phase_id]`. For dimension-based gates, `GateResult.score` is 0.0 by default (the parse only extracts dim values into `scores`). The JSONL `gate_evaluated` event and the CLI print both show 0.0 for passing multi-dim gates, which looks like a failure.
    - Real dim scores live in `gate_feedback[phase_id]["scores"]`, not surfaced in logs.
    - Fix: when the gate has dimensions, either (a) log dim scores alongside `score`, or (b) compute a synthetic summary score (`min(scores.values())` or weighted avg) for the top-level display.

- **LLM gates inherit PromptBackend flakiness with misleading 0.0 fallback**
    - `runtime/gates.py::evaluate_gate_llm` returns `GateResult(score=0.0, feedback="gate backend error: ...")` when the backend call fails. The backend error rolls up as a gate failure (score=0.0) rather than a phase error. On a bad ACP turn, a phase with a passing output can be retried or failed purely due to gate-dispatch flake.
    - Observed in `abstract` phase across runs — same content, different LLM gate outputs (0.0 vs 0.92 dim scores) depending on whether the ACP call to the gate model returned parseable JSON.
    - Fix: distinguish between "gate eval failed" (infrastructure) and "gate scored 0.0" (content judgment). A failed gate eval should retry the GATE call, not fail the phase. Possibly: separate retry budgets for gate-eval-infra failures vs. gate-scored-low.

## Gate-evaluation extensibility (after structured-feedback MVP lands)

- **Customizable gate return schemas**
    - Default MVP: `{score: float, feedback: str | None}` — single dimension with threshold check
    - Extension: gate YAML declares the shape and acceptance predicate
        - Independent dimensions: `{correctness: 0.8, style: 0.6}` with per-dimension thresholds
        - Composite scores with weighting: `{overall: weighted_sum(dims, weights)}`
        - No overall score: pure criteria-based pass/fail over named fields
    - Predicate language: start with a list of `{field, min}` checks; consider a small expression form later
    - State shape change: `gate_scores: dict[str, float]` generalizes to `gate_values: dict[str, dict]` once dimensions land

- **Multi-tier retry escalation for fan-out + synthesis**
    - Retry tiers:
        - **I** — synthesis phase retries on its own (existing behavior; local to the synthesis phase)
        - **J (escalated)** — after `I` exhausted, re-run fan-out subphases with synthesis-gate feedback injected into `_retry_reason`; subphase worktrees are preserved and reused. Reset `I` counter on escalation.
        - **K (super-escalated)** — after `J` exhausted, re-run further upstream (phase dependency chain); reset `J` counter.
        - Exhausted: policy flag (hard fail vs. warn-continue vs. human-in-the-loop)
    - Requires:
        - Retry counter per tier in state (`retries_tier: dict[phase_id, dict[tier, int]]`)
        - Gate outcome can signal tier-escalation (`outcome: "retry" | "escalate" | "super_escalate" | "fail"`)
        - Worktree pool already retains per-phase trees across attempts — reuse is free
    - Trunk/merge branch declared in YAML: `settings.trunk_ref: main` to define the base for worktrees and the target for synthesis merges

- **Synthesis as first-class concept**
    - Today: `final_phases` is the implicit synthesis site for dynamic subphases; regular phases chain worktrees via `{{dep_worktree}}` context
    - Make synthesis explicit: a `synthesis_phase:` block with `merges_from: [...]` listing subphase ids, blocking gate, pre-merge worktree
    - Enables: synthesis-gate blocking merge (if gate fails, changes never fold back); reset semantics for the escalation tiers above

## Architectural moves

- **Phases as proper langgraph subgraphs**
    - Each `Phase` compiles to a compiled `StateGraph` used as a node in the parent, replacing the inline expansion in `engine/builder.py:568-755`
    - Enables encapsulation, reusability, nested workflows, cleaner retry semantics, explicit parent/child state boundary
    - Dynamic subphases collapse to "spawn N instances of the subgraph"
    - Open questions: state projection vs. full sharing, reducer composition at boundary, how `{{dep}}` template substitution resolves across subgraph boundaries

- **Flexible output contracts**
    - Glob patterns: `required_files: ["docs/*.md", "reports/**/*.pdf"]`
    - Size / non-empty checks: `{path: "out.json", min_bytes: 10}`
    - JSON-schema validation of structured outputs (replaces the removed `parse_output_as_json` silent-parse with loud validation)
    - Optional files (tracked but non-failing)
    - Templated paths resolved from dep outputs or vars
    - Forbidden files to catch leftover artifacts
    - Tree-shape constraints (e.g. "≥N files under `reports/`")

## Correctness

- **Subphase quality gates with retries**
    - `_make_subphase_node` (`engine/builder.py:340-464`) records score but never retries
    - Unify with `_make_gate_router` retry loop (`builder.py:281-302`)
    - CLAUDE.md known limitation #3

- **Parallel execution at same DAG level**
    - Verify first — langgraph supersteps should already parallelize sibling phases for free
    - If broken, check for unnecessary sync nodes in the builder or serialization in `runner.py` `astream`
    - Lock in behavior with `tests/test_parallel.py`

- **Prompt-based gate validators**
    - Wire through `PromptExecutor` with a tight context budget to evaluate gate quality via Claude against a rubric
    - Previously stubbed in `runtime/gates.py`; removed pending real implementation

## Features

- **Output caching / skip-if-unchanged**
    - Make-style incrementality (not provided by langgraph checkpointers)
    - Skip when `required_files` still exist and input fingerprint (dep outputs + prompt hash + vars) matches
    - New `cache: bool` field on `Phase`, fingerprint persisted alongside state

- **CLI variable overrides**
    - `abe-froman run --var key=value` (repeatable)
    - `{{vars.key}}` namespace in prompt templates
    - Optional `${var}` substitution in YAML at config-load time

- **Conditional phases (`run_if`)**
    - Pre-execution skip on a predicate over `phase_outputs` / env / vars
    - Compiles to a conditional edge at phase entry
    - Distinct from `QualityGate` with `blocking: false`, which only skips dependents _after_ execution

- **Workflow cancellation**
    - `asyncio.CancelledError` handling in `engine/runner.py`
    - Propagate to executors, persist partial state, clean up ACP subprocesses

- **`abe-froman status` / `dump-state`**
    - Pretty-print persisted state: completed/failed phases, retry counts, gate scores, token usage
    - Works against state file or a langgraph checkpointer if adopted

## Refactoring

- Relationship with LangChain
    - **Separate the three layers explicitly**
        - DSL layer (Pydantic schema, YAML, CLI) — _wraps_: users see `Phase`/`QualityGate`/`OutputContract`, never hear "StateGraph"
        - Compilation layer (`build_*_subgraph`, node/router factories) — _extends_: direct langgraph calls, reads like a tutorial
        - Runtime layer (executors, backends, gate validators) — _composes_: abe_froman concepts slot into langgraph nodes as peers, not replacements
        - Every file should be answerable at a glance: "which layer am I in?"
        - Current code fails this because `_make_phase_node` mixes DSL concepts (output contracts, retry policy) with direct `StateGraph.add_node` / `add_conditional_edges` collapse
    - **Rules for the refactor**
        - Never hide langgraph imports behind abe_froman shims (no `from abe_froman.graph import StateGraph`)
        - Never subclass or decorate langgraph primitives (no `AbeFromanStateGraph`, no `@phase_node` wrapper around `add_node`)
        - Keep Pydantic DSL types as the top-level surface — they must not import from `langgraph`
        - Inherit langgraph features rather than reinvent them (checkpointers, interrupts, visualization, subgraphs)
        - `Send` already leaks in `builder.py:508` — a partial wrapper is worse than an honest extension
        - Stability contract is the YAML schema, not the Python API underneath
    - **Tests a refactor PR must pass**
        - YAML schema has zero langgraph terminology
        - Compilation layer has zero abe_froman shim over langgraph
        - Any abe_froman feature can be explained to a langgraph user in one sentence (e.g., "a quality gate is an `add_conditional_edges` with a score-based router")
        - A new contributor who knows langgraph can read `build_phase_subgraph` and immediately recognize the primitives
        - Swapping `SqliteSaver` for `PostgresSaver` requires zero DSL changes
        - Adding a new langgraph feature (e.g. `interrupt()`) is a localized compilation-layer change

- **Split `engine/builder.py` (755 lines)**
    - `builder/phases.py` — phase node factory + gate router
    - `builder/dynamic.py` — subphase template, dynamic router, final phase
    - `builder/graph.py` — top-level `build_workflow_graph`

- **Extract `build_phase_subgraph` helper**
    - Factor single-phase compile out of the top-level builder
    - Stepping stone to "phases as subgraphs"

- **Unified `ExecutionResult` type**
    - Merge `PhaseResult` + `PromptBackendResult` (overlapping `output`, `structured_output`, `tokens_used`)
    - Document "executor owns retry policy, backend owns transport"

- **State shape cleanup**
    - Group phase data into `phases: dict[str, PhaseRunData]` with one merge reducer
    - Document `_subphase_item` as an explicit transient channel
    - Split `PhaseState` (phase-visible) from `WorkflowState` (runner-level)

- **Test reorganization**
    - `tests/unit/` per-module, `tests/e2e/` joke-workflow-through-ACP, `tests/builder/` graph-shape assertions

## Execution engines

- **Direct Anthropic API backend**
    - `executor/backends/anthropic.py` using the `anthropic` SDK — no ACP process
    - Removes ACP as a hard runtime dependency
    - Map 429 / 529 / rate-limit to `OverloadError` (activates the dormant model-downgrade path in `executor/prompt.py:94-110`)
    - Expose input/output token counts via `PromptBackendResult.tokens_used`
    - `settings.executor: "anthropic"`

- **OpenAI-compatible backend**
    - `executor/backends/openai.py` using the `openai` SDK with configurable `base_url`
    - Unlocks OpenAI, Azure OpenAI, Ollama, vLLM, llama.cpp, LM Studio, LiteLLM
    - Separate model-downgrade chain
    - `settings.executor: "openai"` + `settings.openai_base_url`

- **Wire `OverloadError` through `ACPBackend`**
    - Translate ACP 429 / 529 / overload codes so the existing downgrade path fires with ACP too

- **Streaming on `PromptBackend`**
    - Optional `async stream_prompt(...) -> AsyncIterator[str]`
    - Live progress in CLI and JSONL log

## Langgraph adoption wins

- **Dry-run DAG visualization**
    - `compiled.get_graph().draw_mermaid()` / `draw_ascii()` / `draw_mermaid_png()` are built-in
    - Wire into `abe-froman graph` and `abe-froman run --dry-run`

- **Checkpointer-based resume**
    - Replace `.abe-froman-state.json` with `SqliteSaver` (or `PostgresSaver`)
    - Unlocks thread-level resume, parallel-run isolation, and interrupts

- **Interrupts / human-in-the-loop**
    - `interrupt()` + `Command(resume=...)` from langgraph
    - Free once on a checkpointer; enables "pause for operator approval, resume"
