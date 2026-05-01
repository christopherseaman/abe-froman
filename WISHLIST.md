# Wishlist

- [ ] **Documentation**
    - README with project overview, usage, and functionality
    - TECHNICAL.md with layout/breakdown of implementation

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

- [ ] **Default executor should be real, not stub**
    - Today: `settings.executor` defaults to `"stub"`, so a workflow with prompt nodes silently emits `[prompt-stub] {id}: {url}` placeholders unless the author either declares `executor: "acp"` in YAML OR passes `-e acp` on every CLI invocation. That's a footgun — running an absurd-paper or jokes workflow without `-e acp` produces convincing-looking output that is fake.
    - Want: detect available backends at startup; default to the first real one (anthropic API key in env → anthropic; ACP adapter on PATH → ACP). Fall back to stub only when no real backend is available, and emit a warning when stub is selected. CLI flag stays as an explicit override.
    - Companion change: rename or remove `--executor stub` since "fake responses" should require an opt-in like `--no-network` or `--dry-run`, not be the path of least resistance.

- [ ] **Three orthogonal axes for LLM execution, configurable in YAML**
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

- [ ] **Direct Anthropic API backend**
    - `runtime/executor/backends/anthropic.py` using the `anthropic` SDK — no ACP process
    - Removes ACP as a hard runtime dependency for prompt-mode nodes
    - Map 429 / 529 / rate-limit to `OverloadError` (activates the dormant model-downgrade path in `runtime/executor/prompt.py`)
    - Expose input/output token counts via `PromptBackendResult.tokens_used`
    - Selected by `settings.llm.protocol: api` + `settings.llm.provider: anthropic`

- [ ] **OpenAI-compatible backend**
    - `runtime/executor/backends/openai.py` using the `openai` SDK with configurable `base_url`
    - Unlocks OpenAI, Azure OpenAI, Ollama, vLLM, llama.cpp, LM Studio, LiteLLM
    - Separate model-downgrade chain
    - Selected by `settings.llm.protocol: api` + `settings.llm.provider: openai` (+ optional `base_url`)

- [ ] **Wire `OverloadError` through `ACPBackend`**
    - Translate ACP 429 / 529 / overload codes so the existing downgrade path fires with ACP too

- [ ] **Streaming on `PromptBackend`**
    - Optional `async stream_prompt(...) -> AsyncIterator[str]`
    - Live progress in CLI and JSONL log

## Langgraph adoption wins

- [ ] **`Command` objects for node-level routing** — paired with the Evaluation/Decision split (top of file). A node returns `Command(update=..., goto=...)` instead of writing state and being routed by a downstream conditional edge. Removes router closures across `compile/graph.py` and makes the topology self-describing — the destination lives in the node return, not in a separate function reading state we just wrote.

- [ ] **`stream_mode="updates"` in runner + logging** — `runtime/logging.py` currently re-derives per-node events by diffing successive state snapshots from `astream`. LangGraph natively emits `{node_name: partial_update}` per super-step under `stream_mode="updates"`. Swapping modes lets us drop the diffing code and key events on node identity directly instead of guessing from state shape. Lowest-risk reimplementation removal we have.

- [ ] **Interrupts / human-in-the-loop** — `interrupt()` + `Command(resume=...)` from langgraph. Free on the existing checkpointer; enables author/operator approval nodes, manual quality gates, draft review. New execution type (`type: human_review`) or `evaluation.mode: human` schema option.

- [x] **Subgraphs with declared entry/exit** — _landed, Stage 4c (no separate execution type)._ User clarified during planning: graphs and subgraphs are definitionally identical, so a node references another graph YAML via `config:` rather than getting tagged as a `subgraph` type. Recursion falls out naturally. See "Nodes as proper langgraph subgraphs" above.

- [ ] **`add_messages` reducer for in-phase refinement loops** — multi-turn draft → critique → revise within a single phase using LangGraph's native message-list reducer. Phase-local `messages` channel; no ACP round-trip per turn for pure model revision.

- [ ] **Time-travel replay in CLI** — `abe-froman replay <thread-id> --from <checkpoint>`. Checkpointer already persists every super-step; we just don't expose it. Enables A/B of executor changes against the same past state, bisecting regressions, reproducing flakes.

- [ ] **Static breakpoints** — `compile(interrupt_before=[...], interrupt_after=[...])`. Pairs with `--break-before <node>` / `--break-after <node>` CLI flags for step-through debugging of production workflows.

- [ ] **`ToolNode` as a new execution type** — when a phase should hand the model a tool list and have LangGraph route tool calls natively, rather than running a single prompt through ACP. New `execution: { type: tool, tools: [...] }`.

- [ ] **`BaseStore` for cross-run memory** — distinct from checkpointer (per-thread). Shared memory across workflow runs — e.g., "last week's gate was lenient, tighten this week." Optional store wired alongside `AsyncSqliteSaver`.

- [ ] **`RetryPolicy` for transport-level retries** — layer `RetryPolicy(max_attempts=N, retry_on=OverloadError)` on executor-invoking nodes. Complements our eval-score-driven semantic retries; separates infrastructure flakes (rate limits, ACP drops) from content judgment. Closely related to "LLM gates inherit PromptBackend flakiness" above — fixes the same class of bug from a different angle.

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

- [ ] **Stop diffing state in `runtime/logging.py`** — pairs with `stream_mode="updates"` above. Delete the snapshot-compare path; key events directly on the `node_name → update` pairs the stream already emits. Removes ~40 LOC of diff inference and removes a class of bugs where new state channels confuse the diffing logic.
- [ ] **Stop hand-writing router closures** — pairs with `Command` objects above. Delete `_make_evaluation_router`, `_subphase_id_resolver`, the dynamic-router closure, and the conditional-edge scaffolding they feed in `compile/graph.py`. Decision nodes return `Command(goto=...)` directly.

**Not reimplementation** (clarified during audit, kept for reference):

- Eval-score-driven retries (ours) vs `RetryPolicy` (exception-driven) — complementary, not duplicative.
- `_merge_dicts` / `_merge_evaluations` reducers — LangGraph offers no dict-merge or per-key list-append natively.
- Timeouts (`asyncio.wait_for`), concurrency caps (`asyncio.Semaphore`), worktree pool — outside LangGraph's scope.
- Thread ID derivation from `(workflow_name, workdir)` — policy choice, not a feature we shadow.
