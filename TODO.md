# TODO

The single consolidated backlog: open feature wants and deferred
defects/cleanups (each with a diagnosis). Completed work lives in
`CHANGELOG.md` + git history — this file tracks only what's still open.

## Worktree composition (design model)

The shipped model (v1 0.5.3, v2 0.5.4–0.5.6) is **fork → produce →
read/share → promote → GC**: foreman forks a per-node worktree;
`output_contract` validates produced files where the node runs;
shared/named `worktree_group` trees give cross-node reads;
`Node.promote` applies a single worktree's git delta to the base
(discover-by-default, glob-filterable via `output_contract`);
`settings.worktree_gc: on_success` reclaims trees end-of-run.
Isolation is chosen per scope via `settings.worktree` / `Node.worktree`
(`auto`/`isolated`/`off`) or `worktree_group` (the two mutually
exclusive per scope), resolved node → subgraph → graph.

The one **deferred** piece is reconciling *multiple* isolated trees
whose diffs **overlap** (true 3-way / octopus merge) — see **B3**
below; revisited only when a concrete workflow proves it unavoidable,
and even then likely a content-aware acceptance gate rather than blind
`git merge`.

## Post-Stage-5d audit findings (2026-05-08, eval/decision-split branch)

- [ ] 🤞 **(29) `_make_combined_eval_decide_node` is residual debt from the eval/decision split** (`compile/nodes.py`) — top-level gated nodes use the clean Eval + Decision pair, but **dynamic gated parents** stayed on the combined factory because their downstream `_make_dynamic_router` needs `completed_nodes`/`failed_nodes` already in state when it issues `Send(...)` for fan-out. Folding fan-out branch eval into the new pattern would let us delete the combined factory entirely, but requires either (a) graph-level loops over Send branches (LangGraph doesn't support) or (b) a parallel inline-Decision loop that duplicates the new node-factory logic. Defer until fan-out branch authoring patterns surface real pain.

## Post-Phase-B audit findings (2026-05-08, framework alignment + test doctrine sweep)

> **Two closures kept as "don't re-attempt this" notes** (both closed
> 2026-05-20 as not-a-defect): **(30)** there is no native LangGraph
> count-based "wait for N `Send` branches" barrier (`defer=True` waits
> for the whole graph, not one fan-out), so the hand-rolled poll in
> `_make_final_fan_out_node` is the only option. **(33)**
> `NamedBarrierValue` joins work only for static `add_edge`
> predecessors — `Command(goto)` and conditional edges bypass them, and
> `goto` does NOT suppress a node's static out-edges — so
> `all_deps_completed`'s manual barrier is correct, not removable
> (the native-join alternative needs a synthetic marker node per gated
> node: strictly more complexity).

- [ ] 🚨 **(31) `--resume` discards the checkpointer instead of trusting it** (`cli/main.py:370-392`). Reads `channel_values` from prior checkpoint, builds a cleaned state dict, calls `cp.adelete_thread(thread_id)`, then re-streams from initial-state-like dict. Effectively replays the whole graph (the runs-counter in `test_resume_fan_out.py` still pins this: `_read_runs("a") == 2` after resume). The visible symptom of `completed_nodes` accumulating duplicates was masked by the set-union reducer (2026-05-19), but bodies still re-execute. Re-reading the design landscape: the LangGraph-native "pass thread_id to astream" pattern assumes the graph paused mid-execution (via `interrupt()`); a graph that returned terminal-with-failures has nothing to resume from natively. Fully resolving the DAG case requires picking one of three API shapes in TODO #26 (skip-completed-via-prior-run channel, `--resume-from <node>`, JSONL-driven skip). Defer until that design call lands.

## Hardening — structural footgun checks (2026-05-20)

Inspired by a "structural backpressure" review: deterministic,
machine-checkable constraints beat behavioral instructions. Several
entries in CLAUDE.md's "Known limitations" are *author footguns* —
gotchas the workflow author must remember, with nothing in `validate`
to catch a violation. The structural fix is to move each check into
the compile/validate boundary. Warn at minimum; hard-error only where
the construct is unambiguously broken.

- [~] **(34) Compile-time footgun checks for documented gotchas** —
  partial. Warning channel (`compile/lint.py::collect_warnings`) +
  hyphenated-node-id check delivered 2026-05-20. Remaining:
  - [ ] 🤞 **`{{sender_id}}` on a non-goto-reachable node** —
    `_route_sender` is last-write-wins; a node reached by a static
    `depends_on` edge *after* an inline-route hop elsewhere can
    observe a stale `sender_id`. CLAUDE.md tells authors to guard with
    `{% if sender_id %}`. Deferred — needs topology reachability
    analysis and is lower confidence (may be noisy).

## Transport / backend design

- [~] **(35) ACP retirement decision** — `transport: cli` landed 0.3.0;
  both transports coexist (jokes example defaults to cli, acp via
  `--preset acp`). ACP retirement is **deferred** pending real workflow
  soak. ACP earns its weight today only if (a) we surface mid-flight
  `session_update` events to the JSONL log (not consumed today — we keep
  only final text) or (b) another vendor ships an ACP server (only
  `claude-code-acp` exists as of 2026-05). Until either, retiring
  `transport: acp` would delete the backend + conftest pre-flight + npm
  dep note + soak concerns (~400 lines). Open sub-question for the soak:
  does ACP's `new_session()` reset context in-process (cheap) or fork a
  fresh process (collapses the warmth advantage over CLI)? Settle by
  measurement before committing either way.

- [ ] **(36) `transport: cli` provider expansion** — cheap future
  wins once item 35's `provider: anthropic` (`claude -p`) lands.
  Each is roughly a factory row + an argv builder + minimal tests;
  most cost is in pinning the CLI's actual print-mode syntax and
  authenticating against it. None block on schema changes.

  - **`provider: openai`** → `codex exec` (or whatever the current
    print-mode flag is — pin at impl time against the installed
    Codex CLI). Different argv shape than `claude -p`; needs an
    argv-builder per provider.
  - **`provider: google`** → `gemini -p` (tentative; gemini-cli's
    print-mode surface is still maturing as of 2026-05). Add
    `"google"` to the `provider` literal at the same time.
  - **`provider: custom`** + `cli_command: "<template>"` — escape
    hatch mirroring the existing `api_base_url` constraint pattern.
    Lets users wire `aider --message {{prompt}}`,
    `opencode --prompt {{prompt}}`, or any in-house agent CLI
    without us shipping per-tool code. Template tokens: `{{prompt}}`
    on stdin OR as an argv segment; `{{model}}` if needed.

  Per-provider validation: `cli_command` only valid for
  `transport: cli` + `provider: custom`, same constraint shape as
  `api_base_url` had when api transport existed. Auth stays per-CLI
  (the user's `claude /login` / `codex auth` / `gemini auth login`),
  per the README's "auth is per-CLI, not sqrlly's job" framing.

## Coverage gaps from post-Stage-5c audit (2026-05-06)

- [ ] 🚨 **(26) `--resume` semantics are underspecified for goto-driven
  workflows** — _design question, not a bug; surfaced by the
  resume-from-checkpoint e2e (`tests/e2e/test_resume_fan_out.py`)._
  Today `--resume` is a bare boolean: state comes from the SQLite
  checkpoint at `<workdir>/.sqrlly-checkpoint.db`, thread_id is
  derived from `(config.name, workdir)`. cli/main.py loads the
  saved `channel_values`, resets `failed_nodes`/`retries`/`errors`,
  calls `cp.adelete_thread(thread_id)`, then re-streams. Every node
  fires again; completed_nodes accumulates duplicates via
  `operator.add` (`["a"]` → `["a", "a"]`).

  This is wrong for dependency-driven workflows (the canonical
  resume case: 2-hour run completed 5 expensive LLM nodes, failed
  on node 6, user expects `--resume` to retry only node 6). It's
  ambiguous for goto-driven workflows: "completed" isn't a clean
  boundary because goto re-fires (correctly!) accumulate, so a
  wave-pattern dispatcher dying mid-loop has no obvious "where I
  left off." Adding a `if node.id in completed_nodes: return {}`
  guard would fix the DAG case but break the goto re-fire we
  deliberately enabled in audit fix #19.

  Three viable API shapes once a primary use case is picked:
    1. **`--resume` (skip completed)** — DAG-first. Document that
       wave-pattern workflows need idempotent dispatcher bodies.
       Implementation: a `_prior_run_completed: list[str]` channel
       set only at resume entry; node guard checks ONLY that list,
       not `completed_nodes` (so mid-run goto re-fires aren't
       blocked).
    2. **`--resume-from <node_id>`** — explicit cherry-pick.
       Treats upstream as done, restarts at `<node_id>`. Author
       chooses the restart point.
    3. **`--resume --log <jsonl>`** — read the JSONL completion
       log we already emit (`--log out.jsonl`) and skip exactly
       those node ids.

  Defer until a real workflow surfaces the pain — the existing
  test (`tests/e2e/test_resume_fan_out.py::
  test_resume_after_mid_chain_failure`) pins current behavior so
  unintended changes show up. When the design lands, flip the
  runs-counter assertion and update both the test docstring and
  the resume comment in cli/main.py.

  Cross-reference: this question integrates with item 391
  (`replay --from <checkpoint>` CLI) and the unbuilt trunk/merge
  half of item 226 (per-node worktree commits). See "Author-
  declared checkpoints + worktree commits + cross-run resume
  semantics" in the Forward-looking section — the three wishes
  share one underlying mechanism, and pursuing any one in
  isolation risks half-designs the other two work around. The
  visualization tool is a separate forward-looking item with no
  hard deps on these three; its output likely informs the design.

## High-level Architectural

- [ ] Possible to offload orchastration piece to lightweight local tool/package instead of writing from scratch, similar to how we are leveraging langgraph? Dagster? Airflow and Kestra too heavy.

## Simplification candidates (surfaced by 2026-04-17 refactor-done review)

- [ ] **Collapse `runtime/executor/backends/` → `runtime/backends/`** — 4-level nesting (`runtime/executor/backends/acp.py`) for 4 small files. Semantic loss: current nesting signals that only `PromptExecutor` uses backends. If we land the anthropic/openai backends (below), the signal still holds but less strongly — multiple executor types might route through one backends/ module. Low value, low risk; defer until a second executor family justifies the flattening.

- [ ] **Fold `compile/dynamic.py` into `compile/nodes.py`** — 182 LOC would bring `nodes.py` to ~530 LOC. The split is defensive today: `_make_fan_out_node` has legitimately divergent semantics (no dep check, no output contract, no retry routing). **Worth revisiting after** the gate-eval unification above — if the gate block is gone and the final remaining divergence is "Send-triggered vs. normal-invocation," the split stops earning its keep.

- [ ] **Move `_detect_cycles` + `_find_terminal_phases` → `schema/models.py`** — topology validation belongs with the config model. Blockers: `schema/` is currently langgraph-free Pydantic-only; moving these functions in would require no imports from `langgraph`, which they already don't have. Clean move. Low priority — they're stable and small.

## Test doctrine cleanup

- [ ] **Resolve MemoryBackend / ErrorBackend / SleepyBackend / TrackingBackend policy conflict** — `tests/unit/runtime/test_prompt.py` has `MemoryBackend` + `ErrorBackend` used by ~14 orchestration tests; `tests/unit/runtime/test_foreman.py::TestPerModelBackpressure` has `SleepyBackend` + `TrackingBackend`. All four are hand-written Protocol doubles that strict reading of `feedback_no_fake_backends.md` forbids. They instrument `PromptExecutor` / `ForemanExecutor` orchestration (template, preamble, timeout, token threading; per-model concurrency caps) — NOT Claude behavior — so the strict interpretation may be wrong.
    - Three options (detailed at `/home/christopher/.claude/plans/memory-backend-policy.md`):
        1. Extend `StubBackend` with `record=True` to produce one sanctioned recording path; migrate all doubles to it.
        2. Amend the policy memo to permit orchestration-testing doubles, making the existing code compliant.
        3. Move ~14 tests to `tests/acp/` and accept weaker assertions against real Claude.
    - **Recommended: (1) + (2) together** — one sanctioned recording path, policy clarifies the distinction between Claude-behavior simulation (forbidden) and orchestration instrumentation (permitted, via `StubBackend(record=True)` only).

## Top priority after simplification refactor

> ACP process-tree cleanup landed (post-Stage-5b `ACPBackend.close()`
> reaps the descendant tree; flakiness closed 2026-05-15). The **open**
> residual is the soak-test gate: a multi-hour run with
> `max_parallel_jobs > 1` against the absurd-paper workflow (also TODO
> 49 / the ACP-retirement soak in #35).

- [ ] **Worktree garbage collection**
    - Today: `ForemanExecutor` never removes trees under `<workdir>/.sqrlly/` — disk + inodes accumulate indefinitely across runs
    - CLI: `sqrlly worktree list` — table of (phase_id, created, last_used, size, branch)
    - CLI: `sqrlly worktree prune [--older-than 7d] [--phase <id>] [--dry-run]` — `git worktree remove` + directory delete, with safety checks for uncommitted changes
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

### Data-flow gaps

- [ ] **Template `env` additions / pipe dep outputs to stdin for command nodes** — command-node `args` are Jinja-rendered today; extend the same to `env` values and add optional stdin piping of dep outputs, which would unlock simple Python-script "aggregator" nodes.

### Observability

- [ ] **LLM gates inherit PromptBackend flakiness with misleading 0.0 fallback**
    - `runtime/gates.py::evaluate_gate_llm` returns `GateResult(score=0.0, feedback="gate backend error: ...")` when the backend call fails. The backend error rolls up as a gate failure (score=0.0) rather than a phase error. On a bad ACP turn, a phase with a passing output can be retried or failed purely due to gate-dispatch flake.
    - Observed in `abstract` phase across runs — same content, different LLM gate outputs (0.0 vs 0.92 dim scores) depending on whether the ACP call to the gate model returned parseable JSON.
    - Fix: distinguish between "gate eval failed" (infrastructure) and "gate scored 0.0" (content judgment). A failed gate eval should retry the GATE call, not fail the phase. Possibly: separate retry budgets for gate-eval-infra failures vs. gate-scored-low.

## Gate-evaluation extensibility

Multi-dim scoring with per-field `min` thresholds landed with the multi-dimension gate schema commit (`908a82f`). Remaining extensions:

- [ ] **Composite / weighted score expressions** — today dimensions are compared independently via per-field `min`. Next: support `{overall: weighted_sum(dims, weights)}` or a tiny expression language for cross-dim predicates (AND/OR, arithmetic). Low urgency — per-field mins cover the current demo needs.

- [ ] **Multi-tier retry escalation for fan-out + synthesis** — _infrastructure landed, Stages 3 + 3b._ Routes now accept any destination + params and can use `{field: "invocation", op: ">=", value: N}` clauses, so tiered escalation is just a longer route list (no new enum, no new retry-counter channel). Stage 3b confirmed that graph-level retry routing works for top-level gated nodes; fan-out branch retries go through inline loops within the Send-dispatched node body. Still needed: (1) expose an `evaluation:` YAML block that lets authors write custom routes directly; (2) let route destinations name ancestor nodes (cross-node re-entry via graph edges for top-level, via nested inline-loops for fan-out branches). Trunk/merge branch for synthesis merges (`settings.trunk_ref: main`) remains unbuilt.

- [ ] **Synthesis as first-class concept**
    - Today: `final_nodes:` is the implicit synthesis site for fan-out branches; regular nodes chain worktrees via `{{dep_worktree}}` context
    - Make synthesis explicit: a `synthesis_node:` block with `merges_from: [...]` listing branch ids, blocking gate, pre-merge worktree
    - Enables: synthesis-gate blocking merge (if gate fails, changes never fold back); reset semantics for the escalation tiers above

## Sharing readiness (surfaced 2026-05-27, post-0.3.x publish)

- [ ] **ASCII box-art workflow viz (`sqrlly graph --ascii`)** —
  terminal-renderable alternative to the current Mermaid output for
  SSH sessions, PR-description snapshots, and environments where the
  HTML viewer isn't reachable.

  Topology shapes vary — linear chains, DAGs with `depends_on`,
  route ladders, fan-out parents with branches, subgraph references.
  Simple cases (linear, small DAG) render cleanly; complex cases
  (deep nesting + fan-out) may degrade to a less-useful tall layout.

  Implementation options:
  - (a) custom renderer in `compile/graph.py::draw_ascii()` — full
    control, scope-bounded
  - (b) shell out to `graph-easy` (Perl; common but not universal)
  - (c) generate via `graphviz` → ASCII export

  Lower priority than live progress above; complementary —
  progress shows runtime state, viz shows topology.

## Forward-looking — surfaced during 2026-04-18 architecture plan

> Shipped here (see CHANGELOG): implicit join + `execution: { type:
> join }` (Stage 4b); multi-step fan-out children via `execute.url`
> extension dispatch (Stage 5b); subgraphs with declared entry/exit via
> `Node.config:` (Stage 4c); the `sqrlly view` HTML run-visualization
> tool (2026-05-08 MVP). Viz **iteration 2** is still deferred:
> time-slider replay, `--follow` live mode, explicit
> `goto_fired`/`send_dispatched` events for animated arrows, and
> drill-down to per-node worktree commits (depends on the checkpoint
> story below).

- [ ] **Author-declared checkpoints + worktree commits + cross-run
  resume semantics** — _surfaced 2026-05-08 while reframing (26)._
  Three currently-separate wishes share one underlying mechanism:

    - **(26)** `--resume` semantics for goto-driven workflows.
      "Where I left off" isn't well-defined when re-fires accumulate.
    - **391** `sqrlly replay <thread-id> --from <checkpoint>`.
      The super-step checkpointer already persists every step; we
      just don't expose them.
    - **226** Trunk/merge branch for synthesis merges
      (`settings.trunk_ref: main`). The unbuilt half of foreman —
      per-node commits and merge management.

  Integration: an author marks specific nodes as **checkpoint
  boundaries** in YAML (e.g. `checkpoint: true` on `paper` after
  the multi-section synthesis). At runtime, sqrlly commits the
  worktree state at each checkpoint boundary into a per-node branch
  and tags the commit with the super-step id. Two things become
  natural:

    1. **`--resume` becomes well-defined for both styles**: restart
       at the most recent completed checkpoint. DAG workflows
       checkpoint after every gate-passed node by default; goto
       workflows checkpoint only at author-marked boundaries
       (typically post-reconcile or post-synthesis).
    2. **`replay --from <checkpoint>`** stops being a CLI affordance
       on opaque super-step ids and becomes "checkout the
       worktree commit tagged with this checkpoint id" — concrete,
       browsable, debuggable.

  The viz tool above is NOT on the dependency path here, but its
  output makes a natural driver: same view that shows checkpoint
  markers + commit hashes can drive `--resume-from <checkpoint>`
  selection interactively. Likely informs the design once viz
  ships.

  Open design questions if pursued:
    - Schema: `Node.checkpoint: bool` vs `settings.checkpoint_after:
      list[str]` vs implicit (every gate-passed node). The first
      keeps the marker local to the node it applies to; the second
      lets a workflow author override defaults without editing each
      node; the third is zero-config but loud.
    - Worktree commit lifecycle: do commits accumulate forever in
      `.sqrlly/wt-<id>/` or get GC'd after N runs? Tying
      checkpoints to the existing JSONL log (item 391's input
      shape) might let GC be log-driven.

  Defer until a real workflow surfaces the pain. Right framing
  matters more than implementation order: pursuing any one of
  (26)/(391)/(226) in isolation will likely accrete a half-design
  that the other two then have to work around.

## Architectural moves

- [ ] **Flexible output contracts**
    - Glob patterns: `required_files: ["docs/*.md", "reports/**/*.pdf"]`
    - Size / non-empty checks: `{path: "out.json", min_bytes: 10}`
    - JSON-schema validation of structured outputs (replaces the removed `parse_output_as_json` silent-parse with loud validation)
    - Optional files (tracked but non-failing)
    - Templated paths resolved from dep outputs or vars
    - Forbidden files to catch leftover artifacts
    - Tree-shape constraints (e.g. "≥N files under `reports/`")

## Features

- [ ] **Agent skills draft creation primitive** — surfaced 2026-05-06. Authors today encode "small reusable instruction modules" (skill drafts — short Markdown bundles describing a tool/role/workflow + example invocations) inline as prompt files plus per-node Jinja context. As more workflows reuse the same skill across nodes, three patterns emerge: (a) duplicate the .md across prompts, (b) `{% include %}` it, (c) handcraft a meta-prompt that asks Claude to first synthesize a skill from constraints and then apply it. (c) is the interesting case — it's the "draft creation" half of an agent-skill lifecycle (draft → apply → critique → revise) that doesn't have a first-class shape today. **What an `agent_skill:` block could look like**: a node-level declaration that the prompt produces a structured skill artifact (path, name, description, invocation example), and downstream nodes can reference it as `{{skills.<name>}}` for inclusion. Pairs with output_contract for the artifact-on-disk form, and with the TODO "Schema enforcement at backend boundary" item for typing the draft. **Open questions**: should the skill be persisted across runs (cross-thread `BaseStore`?) or is it per-workflow scratch? Should the schema enforce a draft → apply → critique → revise loop, or stay loose and let authors compose? Probably investigate against a concrete example workflow (e.g., `examples/agent_skill_draft/` writing a "research-summary" skill once and reusing it across multiple summarize nodes) before designing the schema.

- [ ] **Tournament pattern — divergent fan-out + synthesizing merge**
    - Pattern: spawn N candidate solutions in parallel, each potentially with **different params/modes** (different model tier, different temperature, different persona/prompt, different provider), then synthesize: a judge picks one as the winning baseline, and a merger incorporates the strongest parts of the losing candidates into that baseline before returning the final result.
    - Today's fan-out almost gets there: a `fan_out:` block already gives N parallel branches with shared template, and `final_nodes:` already sees the aggregate `{{parent_branches}}` map. What's missing is **per-candidate execution config** — each branch needs to be able to override its own model, temperature, prompt, agent vs prompt mode, etc., not just receive different per-item context. Today the manifest can rename the persona via `{{style}}` template substitution but can't make candidate A run on opus while candidate B runs on haiku, because `fan_out.template.execute` is a single shape applied to every branch.
    - **Schema gap to close**: let `fan_out.template.execute.params` reference manifest fields, e.g. `params.llm.model: "{{model}}"` so the manifest can supply per-candidate execution config alongside per-candidate context. Pairs with the three-axis llm config above — once `params.llm` is a recognized shape with extra="forbid", per-candidate llm config slots in cleanly.
    - **Synthesis step shape** — two final_nodes:
      1. **judge**: prompt that reads all `{{parent_branches}}`, picks a winner, and emits structured output `{winner: "candidate_b", reasoning: "...", strengths_in_losers: [{candidate_a: "the conclusion is sharper"}, ...]}`.
      2. **merger**: prompt that reads the winner output + the strengths_in_losers list and produces the merged final output. This is the chosen winner.
    - **Why "tournament" is the right name**: the pattern fundamentally treats the divergent attempts as competing entries that get judged, not as items in a fixed manifest to process uniformly. A tournament implies a winner; fan-out is symmetric.
    - **Iteration**: optional second round where the merged output competes against the original winner — refines until score plateaus. Wraps neatly with the existing route node pattern (`when: "history['judge'][-1]['score'] >= 0.9 → __end__; else: tournament"`).
    - **Demo to deliver alongside**: a small `examples/tournament/` workflow generating 3 alternative drafts of the same paragraph (one with sonnet+formal, one with sonnet+playful, one with haiku+terse), judging which lead reads best, and merging the winning lead with the better-detail-paragraphs from the losers.
    - **Pairs with**: per-node llm config (so per-candidate `params.llm` works); scope-aware settings (so a tournament-as-subgraph inherits sensibly); `add_messages` reducer (for in-phase refinement loops if iterating).

- [ ] **Mode-per-node / per-branch execution config in fan-out** —
  _carve-out of the Tournament-pattern item above (2026-06-09)._ Today
  `fan_out.template.execute` is a single shape applied to every Send
  branch: the manifest can vary per-branch *context* (`{{style}}`) but
  not per-branch *mode* — model tier, temperature, prompt-vs-agent-vs-
  script dispatch, provider/preset. Open question: let each branch
  select its own mode by sourcing it from the manifest, e.g.
  `params.llm.model: "{{model}}"` or a per-branch
  `execute.preset: "{{preset}}"`. This is the exact schema gap the
  Tournament pattern names ("per-candidate execution config"); pulling
  it out as its own item because mode-per-node is useful beyond
  tournaments (mixed-tier fan-out for cost, A/B of prompt vs agent
  shape per branch). Pairs with per-node llm config + scope-aware
  settings. Depends on the "agent definition as an execution shape"
  question below if "mode" is to include an agent shape.

- [ ] **Output caching / skip-if-unchanged**
    - Make-style incrementality (not provided by langgraph checkpointers)
    - Skip when `required_files` still exist and input fingerprint (dep outputs + prompt hash + vars) matches
    - New `cache: bool` field on `Phase`, fingerprint persisted alongside state

- [ ] **CLI variable overrides**
    - `sqrlly run --var key=value` (repeatable)
    - `{{vars.key}}` namespace in prompt templates
    - Optional `${var}` substitution in YAML at config-load time

- [ ] **Conditional phases (`run_if`)**
    - Pre-execution skip on a predicate over `phase_outputs` / env / vars
    - Compiles to a conditional edge at phase entry
    - Distinct from `QualityGate` with `blocking: false`, which only skips dependents _after_ execution

- [ ] **Workflow cancellation**
    - `asyncio.CancelledError` handling in `runtime/runner.py`
    - Propagate to executors, persist partial state, clean up ACP subprocesses

- [ ] **`sqrlly status` / `dump-state`**
    - Pretty-print persisted state: completed/failed nodes, retry counts, gate scores
    - Works against state file or a langgraph checkpointer if adopted

## Refactoring

- [ ] **Unified `ExecutionResult` type**
    - Merge `PhaseResult` + `PromptBackendResult` (overlapping `output`, `structured_output`)
    - Document "executor owns retry policy, backend owns transport"

- [ ] **State shape cleanup**
    - Group node data into `nodes: dict[str, NodeRunData]` with one merge reducer
    - Document `_fan_out_item` as an explicit transient channel
    - Split `NodeState` (node-visible) from `WorkflowState` (runner-level)

## Execution engines

### Backend-selection ergonomics (high priority)

- [ ] **Soft-deprecate `execute.mode`'s interpreter values** — follow-up
  to command presets. `execute.mode` accepts `python` / `node` / `tsx` /
  `bash` (force a script interpreter) alongside `prompt` / `subgraph` /
  `exec` (handler-family disambiguation). The interpreter values are now
  redundant with command presets — a `command: "python3"` preset does
  the same job, more flexibly. The handler-family values stay (they
  disambiguate URL-extension-ambiguous nodes). Consider documenting the
  interpreter values as soft-deprecated and steering authors to command
  presets; eventual removal is a breaking change, defer.

- [ ] **Revisit `Settings.base_url` naming** — after the command-preset
  work lands. `Settings.base_url` resolves relative `execute.url` paths
  to local files; the name reads as an API-endpoint URL (and now
  collides conceptually with `Preset.api_base_url`). Candidates:
  `url_base`, `workflow_root`, `file_base`. Captured per the
  command-preset discussion; not urgent.

### Backends to add

- [ ] **Streaming on `PromptBackend`**
    - Optional `async stream_prompt(...) -> AsyncIterator[str]`
    - Live progress in CLI and JSONL log

## Langgraph adoption wins

- [ ] **`Command` objects for node-level routing** — paired with the Evaluation/Decision split (top of file). A node returns `Command(update=..., goto=...)` instead of writing state and being routed by a downstream conditional edge. Removes router closures across `compile/graph.py` and makes the topology self-describing — the destination lives in the node return, not in a separate function reading state we just wrote.

- [ ] **Interrupts / human-in-the-loop** — `interrupt()` + `Command(resume=...)` from langgraph. Free on the existing checkpointer; enables author/operator approval nodes, manual quality gates, draft review. New execution type (`type: human_review`) or `evaluation.mode: human` schema option.

- [ ] **`add_messages` reducer for in-phase refinement loops** — multi-turn draft → critique → revise within a single phase using LangGraph's native message-list reducer. Phase-local `messages` channel; no ACP round-trip per turn for pure model revision.

- [ ] **Time-travel replay in CLI** — `sqrlly replay <thread-id> --from <checkpoint>`. Checkpointer already persists every super-step; we just don't expose it. Enables A/B of executor changes against the same past state, bisecting regressions, reproducing flakes.

- [ ] **Static breakpoints** — `compile(interrupt_before=[...], interrupt_after=[...])`. Pairs with `--break-before <node>` / `--break-after <node>` CLI flags for step-through debugging of production workflows.

- [ ] **`ToolNode` as a new execution type** — when a phase should hand the model a tool list and have LangGraph route tool calls natively, rather than running a single prompt through ACP. New `execution: { type: tool, tools: [...] }`.

- [ ] **Agent definition as an execution shape — and is it still a
  thing in LangGraph?** — _surfaced 2026-06-09._ Two coupled open
  questions. **(1) Within modes/presets:** can an "agent" (system
  prompt + tool list + persona + model) be declared once and selected
  the way a node selects a preset today — i.e. *agent definition within
  a mode*? That would let nodes (and fan-out branches, see "Mode-per-
  node" in Features) pick an agent shape instead of only prompt/script/
  subgraph/binary. **(2) Verify-first:** is "agent definition" still a
  first-class concept in current LangGraph? The prebuilt agent
  abstraction (`create_react_agent`) has moved across `langgraph.prebuilt`
  / `langchain` between versions — pin the actual surface in the
  installed LangGraph (1.0.x) before designing anything. If it still
  ships a usable agent primitive, decide whether sqrlly adopts it as a
  new execution type (pairs with the `ToolNode` item above) or keeps
  the URL-extension dispatch table. Research-gated; no schema change
  until the LangGraph surface is confirmed.

- [ ] **`BaseStore` for cross-run memory** — distinct from checkpointer (per-thread). Shared memory across workflow runs — e.g., "last week's gate was lenient, tighten this week." Optional store wired alongside `AsyncSqliteSaver`.

- [ ] **`RetryPolicy` for transport-level retries** — layer `RetryPolicy(max_attempts=N, retry_on=OverloadError)` on executor-invoking nodes. Complements our eval-score-driven semantic retries; separates infrastructure flakes (rate limits, ACP drops) from content judgment. Closely related to "LLM gates inherit PromptBackend flakiness" above — fixes the same class of bug from a different angle.

## Stage 5c — inline route (deferred items)

Inline `Node.route` forward-edge dispatch shipped in Stage 5c (see
CHANGELOG). Remaining deferred follow-ups:

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

- [ ] 🚨 **Schema enforcement at backend boundary** — `ACPBackend` and
  stub backends populate `ExecutionResult.structured_output` when a
  Node has `schema:` set. The field exists end-to-end already; today
  no backend writes to it. Unblocks Stage 5b-style "route on producer
  output without going through an evaluate gate."

- [ ] 🚨 **Schema-first templates** — `{{judge.score}}` resolves against
  structured outputs; `{{judge}}` falls back to raw string. Pairs with
  schema enforcement above. Today templates are flat string
  substitution.

- [ ] 🚨 **Schema sources** — inline JSON schema dict OR `schema_file:`
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

- [~] **Stop hand-writing router closures** — partial. `_make_evaluation_router` deleted (Stage 5d Eval/Decision split — Decision nodes emit `Command(goto=...)` directly). `_subphase_id_resolver` deleted (2026-05-15, was dead code). Still open: the dynamic-router closure and conditional-edge scaffolding it feeds in `compile/graph.py` for dynamic gated parents (blocked on item #29).

> **Not reimplementation — don't "fix" these:** eval-score retries vs `RetryPolicy` (complementary, exception- vs content-driven); `_merge_dicts`/`_merge_evaluations` reducers (no native dict-merge / per-key list-append); timeouts / concurrency caps / worktree pool (outside LangGraph's scope); thread-id derivation from `(workflow_name, workdir)` (policy, not a shadowed feature).


---

# Deferred defects & cleanups

Review-surfaced fixes with a diagnosis attached, deferred because the fix
is non-trivial or a judgment call. (The feature-wants above were folded
in from the former `WISHLIST.md` in the 2026-06-03 consolidation.)

## Builder-required functionality gaps

Prioritized missing functionality blocking the downstream **builder**.
These are feature gaps, not defects — listed here for visibility
alongside the deferred work; several overlap items elsewhere in this
file. This is the builder-facing view.

### B1 — No direct-API backend

Planning docs claim it "landed," but the repo stripped Anthropic /
OpenAI / DeepSeek backends in the 0.2.x transport rework — only Claude
Code via `acp` / `cli` remains; `transport: api` is roadmap. Fine for
the builder today (already direct-CLI), but the "use the direct SDK to
dodge ACP process leaks" plan is **not currently available**.

### B2 — `--resume` re-executes completed nodes

Fault-recovery, not skip-completed: completed nodes re-run (LLM phases
diverge on re-run), expensive for multi-hour builds. Known limitation —
`TODO.md` items 26/31 (the `--resume` rewrite). Behavior now
documented in README/SKILLS/CLAUDE; the skip-completed fix is the open
work.

### B3 — Octopus-merge of overlapping isolated trees (DEFERRED — the one rabbit hole)

The 2026-06-02 design narrowed this to its true residual: reconciling
**multiple** independently-isolated worktrees whose diffs **overlap**
(real 3-way / octopus merge). Single-source consolidation is handled by
**promotion** (git-delta apply, see below), so B3 is no longer "the
join engine" — it is only the overlap case. Deferred until a concrete
workflow proves it unavoidable; even then likely a content-aware
acceptance gate, not blind `git merge`. The `build-<N>-snapshot-*`
branch convention is a builder Phase-9 concern, not v2.

### ✅ Promotion + auto-GC — SHIPPED 0.5.4–0.5.6

`Node.promote` (git-delta apply, discover-by-default, glob-filterable
via `output_contract`, top-level nodes only, runs before GC) and opt-in
end-of-run `settings.worktree_gc: never|on_success` both shipped (closes
the old B4 auto-GC gap; the `sqrlly worktree list/prune` CLI subcommands
remain open — see "Worktree garbage collection" above). Open residuals:
(1) shared `worktree_group` + per-member `promote` promotes the tree
once per member (idempotent double-copy) — dedupe by tree path if it
matters; (2) `promote` on fan-out children / subgraph inner nodes is
unwired (a `collect_warnings` advisory would close the footgun).
Advisory test gaps: promote+GC in one run; `worktree_group` across
`--resume`.

### B5 — `output_contract` *validation* globs are literal (promotion filter shipped glob-aware)

`required_files` are checked verbatim by `output_contract` *validation*
(`gates.validate_output_contract` → literal `Path.exists()` per entry).
The promotion filter-mode shipped glob-aware: `runtime/promote.discover_changes`
applies git pathspec `:(glob)` filters over `OutputContract.required_paths()`
(base_directory-prepended). REMAINING: make *validation* glob-aware too —
a glob should mean "at least one match exists" (a literal path is still a
valid glob matching itself, so existing contracts are unaffected).

### B6 — Isolated worktrees are source-only; gitignored deps break in-branch gates

_Surfaced 2026-06-09, before the expensive builder run._ Isolated
worktrees fork **source only** — `node_modules` (and any gitignored
build deps) don't come along. So an in-branch mechanical gate like
`tsc --noEmit` has nothing to compile against: the type-check fails for
lack of dependencies, not for a real type error. Blocks the build-smoke
gate that's meant to run *before* the multi-hour LLM build. Open
decision (shapes the in-branch gate design — resolve before kicking off
B1 build-smoke):

- **Share base `node_modules` into each branch worktree** (symlink, or
  ACP `--add-dir` / cwd extension) — **current lean.** Cheap; keeps the
  gate in-branch where the edited source actually lives, so the check
  sees uncommitted edits.
- **Run the mechanical gate from the base post-fork** — base has its
  `node_modules`, but it sees committed source only; a branch's
  uncommitted edits aren't visible unless promoted first, so the signal
  is weaker / out of sync with the branch.

Cross-ref: this is a **read/share** gap (worktree-composition north
star: fork → produce → read/share → promote → GC) specific to
*non-source* deps — promotion (git-delta) handles source, not
gitignored artifacts. Pairs with B1 scaffolding + build-smoke.

## Low-priority / judgment calls

### 🤞 U1 (residual) — `file://` URLs still bypass script + workdir gates

`runtime/url.py`. The `max_remote_fetch_bytes` size cap now applies to
`file://` reads, but two gaps remain: `file://` still skips
`allow_remote_scripts`, and there is no path-within-workdir
confinement — `url: /etc/passwd` reads unconfined. Low severity under
the current trust model (a workflow author already controls what
executes), but a real robustness gap if workflow YAML ever comes from
a less-trusted source. Workdir confinement is the larger of the two
and deserves its own design pass.

The behavior is now **explicitly documented** as trusted-input (no
`file://` confinement) in `SCHEMA.md`,
and `SKILLS.md` — so it's no longer a silent gap; only the
confinement *fix* remains deferred here.

### 🤞 V1 — terminal-compatible graph visualization

`sqrlly graph` emits raw Mermaid (needs an external renderer) and
`view` writes HTML — neither renders in a terminal, and both show only
static topology (route `goto` edges are runtime `Command(goto=...)`,
not drawn). Explore rendering a terminal-friendly diagram, e.g. via
[beautiful-mermaid](https://github.com/lukilabs/beautiful-mermaid), and
ideally overlay the route edges the static graph omits. DX nice-to-have.

Package it as an **optional `[viz]` extra**, mirroring `[acp]`: the
renderer dep stays out of the core install, the `graph`/`view` command
lazy-imports it (clear "install `sqrlly[viz]`" error if absent), and
the layer/lazy-import pattern follows `factory._build_acp`.

---

## Adapter-port findings (2026-05-30)

Surfaced by using sqrlly **as a tool** to run a real workflow (claude-flow
adapter spine on pawswipe EARS inputs) with native LLM gates + routing.
Source detail: the consumer repo's `adapter-sqrlly/FINDINGS.md`. **No
remaining blockers** — the shared-FS port runs green. These are the
sqrlly-side improvements that would let **file-producing fan-out run
under worktree isolation** + assorted papercuts. Resolved upstream
(verified): H1 permission → 0.5.0 `permission_mode`; per-node model
logging → 0.5.1 `node_model`; fan-out `final_nodes` gate retry → 0.5.0;
per-gate model → `evaluation.model`.

> Shipped from this port (see CHANGELOG): **AP2** worktree isolation
> control (`settings.worktree`/`Node.worktree`, 0.5.3); **AP3**
> cross-worktree output collection (superseded by git-delta promotion);
> **AP4** fan-in worktree pairing (`{{<parent>_branch_map}}` + per-branch
> subgraph worktrees, 0.5.4–0.5.5); **C1** `output_contract` on
> `FanOutFinalNode` (0.5.2); the `--version` flag (0.5.2).

### 🤞 AP1 — worktree-aware fan-out manifest source (highest value)

`_read_manifest` now tolerates a fenced/embedded JSON array in parent
stdout (shipped 0.5.2). The **remaining, higher-value half**: a
worktree-aware `manifest_path` / `manifest_from_file:` that resolves
against the **parent's worktree** (today `manifest_path` resolves
against the base workdir, `compile/_manifest.py`), so a fan-out can
consume a manifest the parent *writes* as a structured artifact under
isolation rather than emitting it as whole-stdout JSON.

### 🤞 AP5 — schema papercut (C5)

- **C5:** `depends_on` can't name an inline fan-out final-node id
  (`_validate_depends_on` only knows top-level ids; workaround: depend on
  the fan-out parent). Fix: allow/document depending on a final-node id.

### 🤞 AP6 — smaller papercuts

- **Gate result schema** — add optional `retry_recommended`
  (fail-fast, no retry) and `critical` (immediate hard fail) keys to the
  gate JSON.
- **`required_files` globs** — literal today; support globs (== B5 /
  the "flexible output contracts" builder want).
- **Py 3.14 langchain warning** — `Core Pydantic V1 functionality…` on
  every `uv run`; cosmetic now, real if langchain drops the V1 shim;
  pin/track the dep.
- **`DEPS_JSON` arg-size (usage note, optional hardening)** — script
  gates (`gates.py::run_evaluation_script`) pass all upstream outputs as
  the `DEPS_JSON` **env var**; on a large fan-out final node this can
  exceed `MAX_ARG_STRLEN` (~128 KiB) → `execve E2BIG`. Workaround: use
  `.md` LLM gates (read files), or pass `DEPS_JSON` via tempfile/stdin.
