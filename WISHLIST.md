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
