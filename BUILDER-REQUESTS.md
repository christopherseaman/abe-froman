# Proposed additions to `abe-froman/WISHLIST.md`

Items Samus needs that are **not already in** `/home/christopher/projects/abe-froman/WISHLIST.md`. Formatted to match the existing WISHLIST style so they can be dropped in under their natural sections with minimal editing.

Priority tags reflect Samus's sequencing needs; the abe-froman maintainer's own priorities take precedence.

## Revision history

- **2026-05-05** — Triaged against the post-Stage-5b WISHLIST. Dropped three items originally proposed as orchestrator features that are better expressed as Samus-side consumers: Task event log + projector (the existing `stream_mode="updates"` wishlist item already covers what abe-froman needs to emit; Samus-side projector consumes it), Issue tracker abstraction (Samus subscribes to the event stream directly), FastAPI SSE endpoint (YAGNI; future Samus-side daemon if needed). Reframed Delta-replan as a wave-driven dynamic-task *pattern question* rather than a new phase type. Updated the priority-tag table to reflect items that have landed (Stage 3+3b, Stage 4b/c, Stage 5a/b, OpenAI backend, scope-aware settings, ACP process-tree fix, auto-detect executor).

## How to use this document

For each net-new item: copy the bullet into the appropriate section of `abe-froman/WISHLIST.md`. For items already in the WISHLIST, consult the priority-tag table in [§Priority tags for Samus on items already in WISHLIST](#priority-tags-for-samus-on-items-already-in-wishlist) below and optionally update the entry with a Samus priority note.

---

## New items

### Under "Execution engines"

- **Agent Teams execution mode / backend**
    - Replaces claude-flow hive-mind (flaky, fragile — see Samus ADR-003) with the TeamCreate/SendMessage/TeamDelete pattern proven in Claude Code sub-phase prompts (Samus `builder/backpack/phases/phase-1-4-feature-plan-gen.md:20-28`, `phase-3-2-polish-audit.md`, `phase-5-1-qa-audit.md`)
    - Schema sketch: `execution: { type: agent_teams, team_name, teammate_count, teammate_prompt_file, required_outputs_per_teammate }`
    - Backend: one Claude Code subprocess per teammate, file-based output aggregation, fault isolation (one teammate's failure doesn't kill siblings), reuses existing Foreman concurrency caps
    - **May already be largely covered by Stage 5b.** Per-child subgraph fan-out gives orchestrator-managed N-way parallel Claude Code execution with fault isolation and output aggregation generically. The remaining gap is *within-Claude-Code* TeamCreate/SendMessage usage, which is a prompt-side concern (the phase prompt instructs Claude Code to spawn its own internal teammates via the existing ACP backend). If the prompt-side path is sufficient, the dedicated `agent_teams` execution type is redundant
    - Decision needed from abe-froman owner: does a dedicated execution type pay for itself given Stage 5b, or is the answer "use `fan_out:` with per-child subgraph + ACP execute, drive teammates from the prompt"?
    - Samus priority: **required for Phase 5 adapter migration** if the dedicated type wins; otherwise zero (covered by existing primitives + prompt patterns)

### Under "Features"

- **Wave-driven dynamic-task pattern (state-mutating fan_out loop)**
    - Replaces the original "Delta-replan phase type" framing. The need: when a phase-2 task discovers a missing dependency or an AC modification mid-run, the orchestrator should pick up the new task in the next parallelizable wave without re-running completed work and without requiring a human interrupt. **This is explicitly not a HITL flow** — automatic wave-driven re-planning is the goal
    - Pattern sketch (using existing primitives where possible):
      1. Execute node mutates a `tasks` channel (per-task statuses + new task additions)
      2. Fan_out node reads the current `tasks` state, dispatches over all currently-ready tasks (deps satisfied, status=pending)
      3. Each task completes; reconcile execute node updates statuses + appends any newly-discovered tasks
      4. Route node decides "more ready tasks?" → loop back to fan_out, vs proceed to next phase
    - Open questions for abe-froman owner — does this require new primitives, or is it expressible with current Stage 5b + route node + a small reducer addition?
        - **Likely needed**: a custom reducer (similar to `add_messages`) for the `tasks` channel that supports per-key updates + new-key appends without trampling concurrent writes
        - **Likely needed**: confirmation that `fan_out:` re-reads its manifest on re-entry from a self-loop super-step (so post-update state is what the next dispatch wave sees). LangGraph super-step semantics suggest yes, but worth verifying explicitly
        - **Probably not needed**: dynamic graph recompilation. The shape is graph-static; only the manifest the fan_out reads is dynamic
    - Samus priority: **Phase 4-5** — eliminates the ~4-hour phase-1 full-restart pathology observed in `build-fshq-a2`

### Under "Simplification candidates" or new "Observability" section

- **Memory back-pressure in Foreman**
    - Requirement: when host memory exceeds a configurable threshold, block new node dispatches until either an in-flight job completes or memory drops back below the threshold. Existing `max_parallel_jobs` (count) and `per_model_limits` (per-model) gates are insufficient when individual nodes have variable memory footprints
    - Must NOT abort in-flight nodes — back-pressure only gates new dispatches
    - Must compose with existing concurrency caps (gate is OR — both count AND memory must allow dispatch)
    - Implementation hint (not prescriptive): `psutil.virtual_memory().percent` polled before each batch dispatch, integrated into Foreman's existing concurrency loop. Setting shape: `settings.parallelism.memory_threshold_pct: 80` (or equivalent absolute-bytes form)
    - Samus priority: **Phase 5** (needed before `max_workers` can safely rise above 2 given Samus's Docker 12g ceiling)

### Under "Architectural moves"

- **Foreman branch/commit/merge management**
    - Today `ForemanExecutor` creates per-node worktrees but doesn't manage branches or commits; phase prompts must handle git themselves. This bullet captures the requirements; specific code shape is the abe-froman owner's call. Scope is non-trivial and several design questions need to be settled before implementation begins
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
    - **Snapshot-naming convention stays caller-side**: Samus's `build-<N>-snapshot-<phase>-<MMDD>-<HHMM>` is a pattern Samus's wrapper supplies via `branch_pattern`; not an abe-froman concern
    - Samus priority: **required for Phase 9**; enables true worker isolation with preserved snapshot conventions

---

## Priority tags for Samus on items already in WISHLIST

These items are already drafted in `abe-froman/WISHLIST.md`. Samus's needs assign priorities against them; the maintainer's own priorities take precedence. Items marked "Landed" have shipped since this table was last refreshed.

| WISHLIST item (existing) | Location | Samus priority | Notes |
|---|---|---|---|
| Unify gate-eval via outcome-as-routing-signal | Simplification candidates | Landed (Stage 3 + 3b) | Was the mechanism for multi-tier retry routing; now the foundation for Samus's J-tier-equivalent route lists |
| Synthesis as first-class concept (`synthesis_phase:` block) | Gate-evaluation extensibility | **Phase 5 (high)** | Samus uses this as the batch-boundary primitive for phase-2 parallelization (see ADR-005) |
| Multi-tier retry escalation (tiered route lists) | Gate-evaluation extensibility | **Phase 5 (high)** | Infra landed (Stages 3 + 3b); Samus uses J-tier-equivalent for "retry only failed upstream tasks with feedback injected"; replaces nuclear-restart for most cases (see ADR-006). Remaining: `evaluation:` YAML block for author-written routes; cross-node re-entry by name |
| Direct Anthropic API backend | Execution engines → Backends to add | **Phase 5 (high)** | Becomes primary Samus backend (mitigates ACP process-tree leaks; direct Claude API token tracking) |
| OpenAI-compatible backend | Execution engines → Backends to add | Landed | Bonus: unlocks Ollama/vLLM/LiteLLM via `base_url` overrides for local dev and cost experiments |
| Scope-aware settings inheritance | Execution engines → Backend-selection ergonomics | Landed | Subgraph settings inherit cleanly; Samus's per-phase model overrides compose correctly |
| Default executor real, not stub | Execution engines → Backend-selection ergonomics | Landed | Auto-detect chooses real backend; stub is opt-in only |
| `stream_mode="updates"` in runner + logging | Langgraph adoption wins | **Phase 3 (high)** | Samus's task projector consumes this stream. **Consumer requirements**: stable per-node event identity (`{node_id, event_type, timestamp, payload}`), NDJSON line-oriented format, predictable schema across versions, multi-consumer-safe (file readable concurrently with appends). Samus's projector maps the stream to Samus-domain event types (`criteria-add/-modify/-supersede`, `worker-claim`, etc.) entirely on the consumer side |
| Stop diffing state in `runtime/logging.py` | Reimplementation debt | **Phase 3 (high)** | Pairs with the above; the diff-based approach has edge cases (new state channels confuse the diffing logic) that a Samus consumer can't safely build on. Net effect of the swap: clean per-node update events keyed on node identity, no inference required by downstream consumers |
| Flexible output contracts (glob/JSON-schema/size) | Architectural moves | **Phase 5 (high)** | Samus's `required_files` expectations break if gates fire before outputs stabilize; glob support specifically needed for adapter outputs that emit per-component files |
| Phase execution timeout | Schema (existing `timeout` field) | **Phase 5 (medium)** | Safety net; abe-froman already has `timeout` field on Node schema |
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

## Explicitly NOT requested from abe-froman (Samus handles these itself)

- **Issue tracker integration** — Samus subscribes to abe-froman's event stream (post-`stream_mode="updates"`) and triggers `gh issue create/update/close` on phase-start/complete/fail. Pure consumer; no abe-froman change. Was originally proposed as a `runtime/issue_tracker/` interface; YAGNI'd as not paying rent for non-Samus consumers
- **FastAPI SSE / real-time UX endpoint** — was originally proposed as `runtime/sse.py`. Samus's `app/backend` already uses Inngest realtime; if live-build-progress UX matures into a hard requirement, the right shape is a small Samus-side daemon that tails the JSONL event stream and forwards to Inngest. abe-froman stays library/CLI shaped, no FastAPI dep
- **Task event projector** — Samus consumes abe-froman's event stream and projects it into Samus-domain event types (`criteria-add/-modify`, `worker-claim`, etc.). Event-type vocabulary is Samus-domain, not orchestrator concerns. The projector is a Samus-side module that depends only on the WISHLIST `stream_mode="updates"` item being met
- **Issue tracker implementations** beyond any optional interface — Samus implements `github.py` as part of its `platform-plan` work
- **The 7 directive schemas** (`handoff.json`, `task-graph.json`, etc.) — Samus-domain contracts, not orchestrator concerns. abe-froman just consumes generic Node entries; Samus's adapter phase-3-8 emits them. Format stays JSON for canonical-hashing semantics (RFC 8785/JCS)
- **Continuous testing integration** — Samus-specific, lives in build prompt template
- **GitHub-specific `snapshot_branch` naming convention** — stays in the Samus wrapper that invokes abe-froman; abe-froman's Foreman just exposes generic `branch_pattern` + `commit_on_success` config

## Summary of net scope

- **4 net-new items** to add to `abe-froman/WISHLIST.md`:
    1. **Agent Teams execution mode** — pending decision (may already be covered by Stage 5b)
    2. **Wave-driven dynamic-task pattern** — open question on whether existing primitives suffice or a new reducer is needed
    3. **Memory back-pressure in Foreman** — small focused requirement
    4. **Foreman branch/commit/merge management** — substantial; requirements + open design questions enumerated
- **22 existing WISHLIST items** tagged with Samus priorities (a meaningful fraction now landed since the prior table revision)
- **6 items** explicitly kept in Samus's own codebase (not requested of abe-froman)

The bulk of Samus's platform needs are **already on the abe-froman WISHLIST** (the maintainer's own roadmap). This document largely prioritizes that backlog against Samus's specific Phase-by-Phase needs. The post-Stage-5b refresh dropped three originally-proposed orchestrator features that, on second look, are better expressed as Samus-side consumers of the abe-froman event stream rather than abe-froman concerns.
