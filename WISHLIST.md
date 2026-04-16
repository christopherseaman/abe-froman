# Wishlist

## Architectural moves

- **Phases as proper langgraph subgraphs**
    - Each `Phase` compiles to a compiled `StateGraph` used as a node in the parent, replacing the inline expansion in `engine/builder.py:568-755`
    - Enables encapsulation, reusability, nested workflows, cleaner retry semantics, explicit parent/child state boundary
    - Dynamic subphases collapse to "spawn N instances of the subgraph"
    - Open questions: state projection vs. full sharing, reducer composition at boundary, how `{{dep}}` template substitution resolves across subgraph boundaries

- **Flexible output contracts**
    - Glob patterns: `required_files: ["docs/*.md", "reports/**/*.pdf"]`
    - Size / non-empty checks: `{path: "out.json", min_bytes: 10}`
    - JSON-schema validation of structured outputs (today `parse_output_as_json` only attempts a `json.loads`, no schema check)
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
