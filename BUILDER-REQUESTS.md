# Proposed additions to `sqrlly/WISHLIST.md`

Items Samus needs that are **not already in** `/home/christopher/projects/sqrlly/WISHLIST.md`. Formatted to match the existing WISHLIST style so they can be dropped in under their natural sections with minimal editing.

Priority tags reflect Samus's sequencing needs; the sqrlly maintainer's own priorities take precedence.

## Revision history

- **2026-05-05** — Triaged against the post-Stage-5b WISHLIST. Dropped three items originally proposed as orchestrator features that are better expressed as Samus-side consumers: Task event log + projector (the existing `stream_mode="updates"` wishlist item already covers what sqrlly needs to emit; Samus-side projector consumes it), Issue tracker abstraction (Samus subscribes to the event stream directly), FastAPI SSE endpoint (YAGNI; future Samus-side daemon if needed). Reframed Delta-replan as a wave-driven dynamic-task *pattern question* rather than a new phase type. Updated the priority-tag table to reflect items that have landed (Stage 3+3b, Stage 4b/c, Stage 5a/b, OpenAI backend, scope-aware settings, ACP process-tree fix, auto-detect executor).
- **2026-05-06** — Post-Stage-5c branch `feat/native-events-anthropic-stubectomy` lands three Samus-Phase-priority items together: native `stream_mode="updates"` event stream with the diff layer dropped (Phase 3 high), Direct Anthropic API backend (Phase 5 high — primary Samus path), and StubBackend removal from production (closes the no-fakes-in-production policy gap). Auto-detect order: Anthropic key → DeepSeek key → ACP/npx; raises `RuntimeError` if none. Breaking change: `--executor stub` and `executor: stub` no longer accepted.
- **2026-05-06** — Wave-driven dynamic-task pattern resolved as **shippable with no new primitives**. Spike traced `Command(goto=...)` re-entry of a fan-out parent; root cause of "loop spins but bodies don't re-execute" was a 3-line resume-mode guard from the pre-LangGraph-checkpointer era (commit `1ec9ed3`, 2026-03-20) that became dead code on 2026-04-17 when `AsyncSqliteSaver` replaced the homegrown JSON-envelope resume. With those guards removed, the pattern works end-to-end against existing primitives; closes Samus's Phase 4-5 ~4-hour-restart pathology with no schema change.

## How to use this document

For each net-new item: copy the bullet into the appropriate section of `sqrlly/WISHLIST.md`. For items already in the WISHLIST, consult the priority-tag table in [§Priority tags for Samus on items already in WISHLIST](#priority-tags-for-samus-on-items-already-in-wishlist) below and optionally update the entry with a Samus priority note.

---

## New items

### Under "Execution engines"

- **Agent Teams execution mode / backend**
    - Replaces claude-flow hive-mind (flaky, fragile — see Samus ADR-003) with the TeamCreate/SendMessage/TeamDelete pattern proven in Claude Code sub-phase prompts (Samus `builder/backpack/phases/phase-1-4-feature-plan-gen.md:20-28`, `phase-3-2-polish-audit.md`, `phase-5-1-qa-audit.md`)
    - Schema sketch: `execution: { type: agent_teams, team_name, teammate_count, teammate_prompt_file, required_outputs_per_teammate }`
    - Backend: one Claude Code subprocess per teammate, file-based output aggregation, fault isolation (one teammate's failure doesn't kill siblings), reuses existing Foreman concurrency caps
    - **May already be largely covered by Stage 5b.** Per-child subgraph fan-out gives orchestrator-managed N-way parallel Claude Code execution with fault isolation and output aggregation generically. The remaining gap is *within-Claude-Code* TeamCreate/SendMessage usage, which is a prompt-side concern (the phase prompt instructs Claude Code to spawn its own internal teammates via the existing ACP backend). If the prompt-side path is sufficient, the dedicated `agent_teams` execution type is redundant
    - Decision needed from sqrlly owner: does a dedicated execution type pay for itself given Stage 5b, or is the answer "use `fan_out:` with per-child subgraph + ACP execute, drive teammates from the prompt"?
    - Samus priority: **required for Phase 5 adapter migration** if the dedicated type wins; otherwise zero (covered by existing primitives + prompt patterns)

### Under "Features"

- **Wave-driven dynamic-task pattern (state-mutating fan_out loop)** — _shippable today, no new primitives needed (resolved 2026-05-06 via spike)._
    - The need: when a phase-2 task discovers a missing dependency or an AC modification mid-run, the orchestrator should pick up the new task in the next parallelizable wave without re-running completed work and without requiring a human interrupt. **This is explicitly not a HITL flow** — automatic wave-driven re-planning is the goal.
    - **Spike result**: end-to-end smoke (`.temp/wave_spike.py`) ran 2 waves cleanly with zero new sqrlly primitives:
        - Pattern: `planner → dispatcher (fan_out) → workers → reconcile → gate (route)` with `gate.route.cases[*].goto: dispatcher` for the loop edge
        - Existing reducer (`_merge_dicts` for `node_outputs`, append for `child_outputs` per-id) handles wave-to-wave state composition correctly
        - LangGraph's `Command(goto=...)` re-enters dispatcher; sqrlly re-reads the manifest from state on each entry; new tasks discovered in wave N get dispatched in wave N+1
        - The only blocker (a 3-line resume-mode holdover guard in `compile/nodes.py` / `compile/dynamic.py` that turned goto re-entry into a silent no-op) was removed once its archeology was understood — it pre-dated the LangGraph checkpointer migration and was dead defensive code
    - **Authoring constraint** (LangGraph reachability rule, not sqrlly policy): a node that's a goto target needs at least one declared static edge into it for the runtime to consider it reachable. Easiest formula: split entry from loop-target (planner has START → dep edge → dispatcher; gate's `goto: dispatcher` then resolves cleanly).
    - **Authoring example**: `examples/wave_driven/` would walk through the pattern. State channel design is up to the author — the spike used a JSON file on disk as the shared `tasks` state because predicates can't read disk; a top-level `tasks` field on `WorkflowState` with `_merge_dicts` reducer is also viable and slightly cleaner.
    - Samus priority: **Phase 4-5** — eliminates the ~4-hour phase-1 full-restart pathology observed in `build-fshq-a2`. Now unblocked.

### Under "Simplification candidates" or new "Observability" section

- **Memory back-pressure in Foreman**
    - Requirement: when host memory exceeds a configurable threshold, block new node dispatches until either an in-flight job completes or memory drops back below the threshold. Existing `max_parallel_jobs` (count) and `per_model_limits` (per-model) gates are insufficient when individual nodes have variable memory footprints
    - Must NOT abort in-flight nodes — back-pressure only gates new dispatches
    - Must compose with existing concurrency caps (gate is OR — both count AND memory must allow dispatch)
    - Implementation hint (not prescriptive): `psutil.virtual_memory().percent` polled before each batch dispatch, integrated into Foreman's existing concurrency loop. Setting shape: `settings.parallelism.memory_threshold_pct: 80` (or equivalent absolute-bytes form)
    - Samus priority: **Phase 5** (needed before `max_workers` can safely rise above 2 given Samus's Docker 12g ceiling)

### Under "Architectural moves"

- **Foreman branch/commit/merge management**
    - Today `ForemanExecutor` creates per-node worktrees but doesn't manage branches or commits; phase prompts must handle git themselves. This bullet captures the requirements; specific code shape is the sqrlly owner's call. Scope is non-trivial and several design questions need to be settled before implementation begins
    - **Auto-branching at worktree acquisition** (requirement):
        - Foreman exposes `branch_pattern: str` (templated against node_id, run_id, etc.); caller picks the convention. Default `None` preserves current behavior
        - Open question: branches scoped per-node or per-graph/subgraph? Today worktrees are per-node and reused across retries — does a branch follow that (multiple retry commits on one branch), or does each retry get its own branch?
        - Open question: how does this interact with subgraphs (Stage 4c)? Does a subgraph node share its parent's branch, or get its own? Decision predates implementation and shapes downstream questions
    - **Auto-commit on success** (requirement):
        - Per-node opt-in via `commit_on_success: bool`. On success: stage + commit with templated message. On failure: leave dirty for inspection (do NOT revert)
        - Atomicity for multi-step nodes: partial-success commits are footguns. Either commit at clean exit only, or expose explicit checkpoint hooks for nodes that want intermediate commits — author's call which shape to expose
        - Templated commit message needs sufficient context access: at minimum node-id, run-id, dep summaries
    - **Octopus-merge primitive for synthesis nodes** (requirement):
        - Synthesis nodes that consume N upstream worker branches need a way to perform `git merge --octopus -m <msg>`. On conflict, return a structured conflict report (file list + conflict markers per file) to the synthesis evaluator (NOT a hard fail — the evaluator decides retry vs escalate)
        - Open question: is this a new `execution: { type: octopus_merge }` primitive, a Python helper available to script-execution nodes, or a Foreman API surface that any synthesis node can call?
    - **Worktree + branch lifetime** (requirement):
        - Branches must outlive worktrees (caller may want to read post-run). Worktree GC (already on WISHLIST) prunes worktree directories but should NOT delete branches without explicit `--include-branches`
        - Snapshot history needs to be preservable independently of working-tree cleanup
    - **Snapshot-naming convention stays caller-side**: Samus's `build-<N>-snapshot-<phase>-<MMDD>-<HHMM>` is a pattern Samus's wrapper supplies via `branch_pattern`; not an sqrlly concern
    - Samus priority: **required for Phase 9**; enables true worker isolation with preserved snapshot conventions

---

## Priority tags for Samus on items already in WISHLIST

These items are already drafted in `sqrlly/WISHLIST.md`. Samus's needs assign priorities against them; the maintainer's own priorities take precedence. Items marked "Landed" have shipped since this table was last refreshed.

| WISHLIST item (existing) | Location | Samus priority | Notes |
|---|---|---|---|
| Unify gate-eval via outcome-as-routing-signal | Simplification candidates | Landed (Stage 3 + 3b) | Was the mechanism for multi-tier retry routing; now the foundation for Samus's J-tier-equivalent route lists |
| Synthesis as first-class concept (`synthesis_phase:` block) | Gate-evaluation extensibility | **Phase 5 (high)** | Samus uses this as the batch-boundary primitive for phase-2 parallelization (see ADR-005) |
| Multi-tier retry escalation (tiered route lists) | Gate-evaluation extensibility | **Phase 5 (high)** | Infra landed (Stages 3 + 3b); Samus uses J-tier-equivalent for "retry only failed upstream tasks with feedback injected"; replaces nuclear-restart for most cases (see ADR-006). Remaining: `evaluation:` YAML block for author-written routes; cross-node re-entry by name |
| Direct Anthropic API backend | Execution engines → Backends to add | Landed | Becomes primary Samus backend (mitigates ACP process-tree leaks) |
| OpenAI-compatible backend | Execution engines → Backends to add | Landed | Bonus: unlocks Ollama/vLLM/LiteLLM via `base_url` overrides for local dev |
| Scope-aware settings inheritance | Execution engines → Backend-selection ergonomics | Landed | Subgraph settings inherit cleanly; Samus's per-phase model overrides compose correctly |
| Default executor real, not stub | Execution engines → Backend-selection ergonomics | Landed | Auto-detect chooses real backend; stub is now removed from production entirely (post-2026-05-06 — `--executor stub` and `executor: stub` no longer accepted) |
| StubBackend removed from production code | Test doctrine cleanup | Landed | No more silent `[prompt-stub]` fallback. Auto-detect raises `RuntimeError` with concrete remediation when no backend resolves |
| `stream_mode="updates"` in runner + logging | Langgraph adoption wins | Landed | Native stream switched on (LangGraph 1.0.7 `stream_mode=["updates", "values"]` tuple). 6 emitted event types and schemas unchanged for downstream consumers — Samus's projector contract is preserved |
| Stop diffing state in `runtime/logging.py` | Reimplementation debt | Landed | `JsonlLogger.log_snapshot(prev, curr)` replaced with `log_update(node_name, update)`; `_PrefixingProxy` deleted. Multi-event-per-update emission order preserved (gate_evaluated before node_completed) |
| Flexible output contracts (glob/JSON-schema/size) | Architectural moves | **Phase 5 (high)** | Samus's `required_files` expectations break if gates fire before outputs stabilize; glob support specifically needed for adapter outputs that emit per-component files |
| Phase execution timeout | Schema (existing `timeout` field) | **Phase 5 (medium)** | Safety net; sqrlly already has `timeout` field on Node schema |
| Worktree garbage collection | Top priority after simplification | **Phase 9 (high)** | Samus's `build-<name>-snapshot-*` branch proliferation must not leak disk across builds. Pairs with the new Foreman branch/commit/merge item above |
| ACP process-tree leaks / zombie subprocesses | Top priority after simplification | Initial fix landed | Soak-test under load remains; Samus prefers Direct Anthropic API backend primary to avoid ACP entirely for prompt-mode nodes |
| Implicit + explicit Join | Forward-looking | Landed (Stage 4b) | Useful at Samus's phase-2 fan-in points |
| Multi-step fan-out children | Forward-looking | Landed (Stage 5b) | Closed by `execute.url` URL-suffix dispatch model |
| Subgraph with declared entry/exit | Forward-looking | Landed (Stage 4c) | Useful for Samus's eventual decomposition of phase-2 into a reusable per-task subgraph |
| Nodes as proper langgraph subgraphs | Architectural moves | Landed (Stage 4c) | Same — recursive `Node.config:` references compile cleanly |
| Fan-out + recursive subgraph composition | Features | Landed (Stage 5b) | Per-child subgraph fan-out covers Samus's phase-2 dynamic dispatch pattern generically |
| Interrupts / human-in-the-loop | Langgraph adoption wins | **Future (medium)** | Useful for eventual human-gated approval flows; **not** the path Samus uses for automatic re-planning (see "Wave-driven dynamic-task pattern" above) |
| `add_messages` reducer for in-phase refinement loops | Langgraph adoption wins | **Future (low)** | Reference shape for the per-key-update reducer Samus needs in the wave-driven pattern |
| Time-travel replay in CLI | Langgraph adoption wins | **Phase 4 (low)** | Helpful for diagnosing phase-2 anomalies post-hoc |
| Dry-run DAG visualization | Langgraph adoption wins | **Phase 0 (low)** | Helpful for documenting Samus workflows in the platform-plan docs |

## Explicitly NOT requested from sqrlly (Samus handles these itself)

- **Issue tracker integration** — Samus subscribes to sqrlly's event stream (post-`stream_mode="updates"`) and triggers `gh issue create/update/close` on phase-start/complete/fail. Pure consumer; no sqrlly change. Was originally proposed as a `runtime/issue_tracker/` interface; YAGNI'd as not paying rent for non-Samus consumers
- **FastAPI SSE / real-time UX endpoint** — was originally proposed as `runtime/sse.py`. Samus's `app/backend` already uses Inngest realtime; if live-build-progress UX matures into a hard requirement, the right shape is a small Samus-side daemon that tails the JSONL event stream and forwards to Inngest. sqrlly stays library/CLI shaped, no FastAPI dep
- **Task event projector** — Samus consumes sqrlly's event stream and projects it into Samus-domain event types (`criteria-add/-modify`, `worker-claim`, etc.). Event-type vocabulary is Samus-domain, not orchestrator concerns. The projector is a Samus-side module that depends only on the WISHLIST `stream_mode="updates"` item being met
- **Issue tracker implementations** beyond any optional interface — Samus implements `github.py` as part of its `platform-plan` work
- **The 7 directive schemas** (`handoff.json`, `task-graph.json`, etc.) — Samus-domain contracts, not orchestrator concerns. sqrlly just consumes generic Node entries; Samus's adapter phase-3-8 emits them. Format stays JSON for canonical-hashing semantics (RFC 8785/JCS)
- **Continuous testing integration** — Samus-specific, lives in build prompt template
- **GitHub-specific `snapshot_branch` naming convention** — stays in the Samus wrapper that invokes sqrlly; sqrlly's Foreman just exposes generic `branch_pattern` + `commit_on_success` config

## Summary of net scope

- **4 net-new items** to add to `sqrlly/WISHLIST.md`:
    1. **Agent Teams execution mode** — pending decision (may already be covered by Stage 5b)
    2. **Wave-driven dynamic-task pattern** — _shippable today (resolved 2026-05-06 via spike). Existing primitives suffice once the resume-mode `# Skip if already completed` holdover guard is removed; that 3-line removal landed alongside the resolution._
    3. **Memory back-pressure in Foreman** — small focused requirement
    4. **Foreman branch/commit/merge management** — substantial; requirements + open design questions enumerated
- **22 existing WISHLIST items** tagged with Samus priorities; as of 2026-05-06, six of the highest-priority Samus items have landed (Direct Anthropic backend, `stream_mode="updates"`, stop diffing state, OpenAI backend, scope-aware settings, default-executor-real-not-stub).
- **6 items** explicitly kept in Samus's own codebase (not requested of sqrlly)

The bulk of Samus's platform needs are **already on the sqrlly WISHLIST** (the maintainer's own roadmap). This document largely prioritizes that backlog against Samus's specific Phase-by-Phase needs. The post-Stage-5b refresh dropped three originally-proposed orchestrator features that, on second look, are better expressed as Samus-side consumers of the sqrlly event stream rather than sqrlly concerns.
