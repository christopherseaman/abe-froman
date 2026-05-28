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
- [x] **(4) `Route.validate_shape` rejects `else_` without `cases:`** — _delivered (commit `f563935`)._ `schema/models.py::Route.validate_shape` now raises `ValueError("Route with 'else:' requires 'cases:' — use 'goto:' shorthand for unconditional dispatch")` for the bare-else_ form. Authors pick the unambiguous `goto:` shorthand or the full `cases:` + `else:` ladder.
- [x] **(5) `EvaluationResult.score=0.0` for multi-dim evals** — _delivered (decided 2026-05-06)._ The threshold check is well-formed for multi-dim — it uses per-dim `result.scores.<field>` clauses and never reads `result.score` for routing. The JSONL-log confusion is purely cosmetic: a passing multi-dim gate emits `score=0.0` in events. Bonus runtime fix in `_parse_evaluation_output`: when JSON has dim_scores but no top-level `score`, derive `score = min(dim_scores)` so the headline matches the weakest-link semantics already in `dimensions[].min`. Two existing tests pinning the 0.0 default flipped to assert the derived value.
- [x] **(6) `_PrefixingProxy.emit` duplicates `SubgraphLogger.emit` verbatim** — _delivered alongside `stream_mode="updates"`._ The `_PrefixingProxy` class was removed entirely; with the snapshot path gone, the prefix is applied directly to `node_name` before dispatch in `SubgraphLogger.log_update`, so there's only one place that knows about prefixing now.
- [x] **(7) `FanOut.enabled: false` silently no-ops the block** — _delivered (decided 2026-05-06)._ Dropped the `enabled` field entirely; the presence of the `fan_out:` block IS the activation. To disable fan-out, remove the block. `FanOut` got `extra="forbid"` so legacy YAML carrying `enabled: true|false` fails at `validate` time with a clear ValidationError pointing at the unsupported field. Breaking schema change documented in CHANGELOG; author migration is "delete the line."
- [x] **(8) `_dispatch_prompt` stub fallback when `_prompt_executor is None` is unreachable in production** — _delivered alongside StubBackend removal._ The branch was replaced with a `RuntimeError` naming the offending node id and the missing-backend cause. No more silent fake output if a custom harness wires `DispatchExecutor` without a `prompt_backend`.
- [x] **(9) `_make_final_fan_out_node` imports `_read_manifest` from `compile/graph.py` at function-body scope** — _delivered._ Extracted `_read_manifest` to `compile/_manifest.py`. Both `compile/graph.py` (fan-out router) and `compile/dynamic.py` (final-node aggregator) import from the shared module; no more cross-private imports between sibling files. Test import path updated.
- [x] **(10) `Graph.validate_node_references` does 4–5 unrelated checks in one 56-line function** — _delivered._ Decomposed into four `@model_validator(mode="after")` methods: `_validate_unique_ids`, `_validate_depends_on`, `_validate_route_targets`, `_validate_routes_not_depended_on`. Each carries one concern; failure messages are unchanged so existing tests stay green.
- [x] **(11) Worktree `is_dir()` recheck in `ForemanExecutor._acquire_worktree`** — _kept (decided 2026-05-06 during audit triage)._ The recheck looked YAGNI within a single run, but it's load-bearing for the rehydrate-resume path: when checkpoint state carries a worktree path that was manually deleted between runs, the recheck triggers re-creation rather than handing the runner a stale path. `tests/unit/runtime/test_foreman.py::test_rehydrated_but_deleted_worktree_is_recreated` pins the behavior. Closing as kept-by-design rather than removed.
- [x] **(12) `monkeypatch.setattr` on `factory.shutil.which` in `test_factory.py`** — _delivered (decided 2026-05-06)._ Closed as document-only: the patch is sanctioned orchestration instrumentation (we choose which environment-shape branch the resolver sees; we are not faking what an external system would respond). Updated `feedback_no_fake_backends.md` with the narrow exception language. Bonus: added `test_npx_resolved_via_real_shutil_which_against_test_artifact` that exercises the same auto-detect path using a real `monkeypatch.setenv("PATH", ...)` pointing at a tmp dir with a non-functional `npx` script — runs against real `shutil.which`. New tests should prefer this PATH-manipulation style when a single binary is the gate; the function-patch style stays available for tests that gate multiple binaries simultaneously.

## Post-merge findings (2026-05-06, native-events + Anthropic + stubectomy branch)

- [x] **(14) `Settings.executor` rejects unknown strings at schema time** — _delivered (commit `f563935`)._ `schema/models.py::Settings.executor` is now `Literal["acp", "anthropic", "custom", "deepseek", "openai"] | None`. Typo'd values fail at `validate` time rather than late at `run`. The `custom` choice covers OpenRouter / Ollama / LM Studio / LiteLLM / Azure OpenAI / vLLM via `CUSTOM_API_KEY` + `CUSTOM_API_BASE_URL`.
- [x] **(15) `AnthropicBackend` empty-text-block edge case** — _delivered (commit `f563935`)._ The text-block filter now drops empty-text blocks before the `if not text_parts:` check (`anthropic.py:128-133`: `... and getattr(block, "text", "")`). An all-empty-text response now triggers the loud "no non-empty text block" failure path instead of silently returning `ExecutionResult(output="")`.
- [x] **(16) `_OVERLOAD_EXCEPTION_NAMES` includes speculative class names** — _delivered (commit `f563935`)._ Dropped the speculative `OverloadedError` entry from the frozenset and replaced the "(newer SDK versions)" comment with a verified-against-0.99 note. Final set: `RateLimitError` / `APIConnectionError` / `APITimeoutError` / `InternalServerError` — all observed in the live SDK.
- [x] **(17) `_is_overload_status` duplicated between `openai.py` and `anthropic.py`** — _delivered, post-merge audit._ Extracted `runtime/executor/backends/_overload.py::maybe_raise_overload(exc, *, class_names)` shared by both backends. Each backend keeps only its provider-specific class-name set as a frozenset constant.
- [x] **(18) Subgraph state isolation lacked unit-level coverage after `test_subgraph_isolation_no_parent_state_leak` was deleted in the stubectomy** — _delivered, post-merge audit._ Restored the test using `_ECHO`-rendered script-args (`leak={{parent_only}}|input={{from_parent}}`) as the observable. Mutation-tested: leaking parent's `node_outputs` into the subgraph's `node_inputs` flips the assertion with a clear "leak=PARENT_VALUE" diagnostic. Real subprocess only.
- [x] **(19) Resume-mode `# Skip if already completed` guard turned `Command(goto=...)` into silent no-op** — _delivered._ Three guards (`compile/nodes.py::_make_execution_node`, `compile/nodes.py::_make_evaluation_node`, `compile/dynamic.py::_make_subphase_node`) all originated in the pre-LangGraph-checkpointer era (commit `1ec9ed3`, 2026-03-20) when the homegrown JSON-envelope resume rehydrated `completed_phases` into state and re-ran the graph. After the migration to `AsyncSqliteSaver` on 2026-04-17, the guards became dead defensive code — checkpointer resume picks up at super-step boundaries, so already-completed nodes don't re-fire from dep edges. With Stage 5c routes, the guards became actively wrong: `Command(goto=X)` is a deliberate re-fire, but the guard suppressed body execution. Removing the three lines unlocks the wave-driven dynamic-task pattern (a fan-out parent re-entered via a route now re-reads its manifest from current state and dispatches the next wave). The `failed_nodes` checks are kept — those guard against re-attempting a hard-failed node and are semantically distinct.
- [x] **(20) DeepSeek live tests (`tests/unit/runtime/test_openai_backend.py::TestDeepSeekLive`) flake** — _diagnosed 2026-05-06: transient upstream blip, not a deterministic bug._ Re-probing confirmed `deepseek-v4-flash` IS in the live catalog (`client.models.list()` returns it alongside `deepseek-v4-pro`) and a direct send completes in ~1.7s. The 30s timeout observed during the test run was DeepSeek server-side load/queueing, not a bug in our code, model name, or the post-Stage-5c branch's changes. No code change required. If recurrent, gate the live tests behind an opt-in `pytest.mark.live` marker rather than chasing a fix to a non-deterministic symptom.
- [x] **(21) Live alias-drift detection for `_MODEL_ALIASES`** — _delivered 2026-05-06 alongside the DeepSeek diagnosis._ Anthropic's vendor IDs (`claude-sonnet-4-6` etc.) can drift if Anthropic releases a new headline model under the same family name. New live test (`tests/unit/runtime/test_anthropic_backend.py::TestAnthropicLive::test_alias_table_resolves_in_live_catalog`) queries `client.models.list()` and fails with a clear remediation message ("update _MODEL_ALIASES to the current headline IDs") when an alias points at a deprecated vendor ID. Skipped automatically when no Anthropic key is on disk. Gives us a per-CI-run drift signal whenever a key is configured.

## Post-Stage-5d audit findings (2026-05-08, eval/decision-split branch)

Three low-priority findings from the parallel framework / DRY-KISS-YAGNI / test-doctrine sweep that ran alongside the eval/decision split. The high/medium findings (test fakes, joke-test placement, `_run_eval_core`, `run_evaluation_and_outcome` signature, lazy-init mixin, `OverloadedError` cleanup) all landed in this branch.

- [x] **(27) `_route_targets()` recomputed per render** — _delivered._ `_route_targets(graph)` was called once per `render_mermaid` plus once per terminal node in the `_routes_to_end` loop. Now computed once at the top of `render_mermaid` and threaded into `_classify_endpoints(graph, routes)` and `_routes_to_end(routes, node_id)`. Pure local refactor; existing 25 view tests pass unchanged.

- [x] **(28) Subgraph wrapper duplicates dep-join logic** — _delivered._ `make_subgraph_node` in `compile/subgraph.py` had a hand-rolled dep-join (manual `completed`/`failed` set construction + inline error-update shape). Replaced with the shared `check_dep_failed` + `all_deps_completed` helpers from `compile/nodes.py`. **Bonus bug fix**: the wrapper still carried the `if parent_id in completed_nodes: return {}` re-entry guard that was removed from `_make_execution_node` / `_make_evaluation_node` / `_make_subphase_node` in audit fix #19 — same bug, fourth location the audit missed. With the wave pattern, a `Command(goto=parent_subgraph_node)` would have silently no-op'd instead of re-invoking. Now consistent with execution-node behavior; `failed_nodes` guard kept (short-circuits hard-failed nodes, semantically distinct).

- [ ] 🤞 **(29) `_make_combined_eval_decide_node` is residual debt from the eval/decision split** (`compile/nodes.py`) — top-level gated nodes use the clean Eval + Decision pair, but **dynamic gated parents** stayed on the combined factory because their downstream `_make_dynamic_router` needs `completed_nodes`/`failed_nodes` already in state when it issues `Send(...)` for fan-out. Folding fan-out branch eval into the new pattern would let us delete the combined factory entirely, but requires either (a) graph-level loops over Send branches (LangGraph doesn't support) or (b) a parallel inline-Decision loop that duplicates the new node-factory logic. Defer until fan-out branch authoring patterns surface real pain.

## Post-Phase-B audit findings (2026-05-08, framework alignment + test doctrine sweep)

Architectural findings from the second-round agent audit. The cluster of small-win items (live markers, ImportError skip, layer-test enforcement, runner try/finally, OVERLOAD names co-location, await_with_timeout helper, three test-content tightenings) landed as commit `5de1166`. The four below are real but defer for design discussion or LangGraph-constraint reasons.

- [x] **(30) `_make_final_fan_out_node` polling barrier** — _closed as not-a-defect, 2026-05-20._ Original framing (it "fights LangGraph's scheduler", a native fan-in aggregator would replace it) was **wrong**. LangGraph 1.0.7 research: there is no native count-based "wait for N `Send` branches" barrier. Fan-out children are `Send`-dispatched and finish across *different* super-steps (variable-length inline retry loops), and the only native idiom for Send fan-in is state-reducer accumulation — which the final node already does (`child_outputs` + the manifest check). `defer=True` waits for the *whole graph*, not this fan-out, so it's the wrong tool. The hand-rolled barrier is doing the only thing possible. Revisit only if a concrete defect surfaces.

- [ ] 🚨 **(31) `--resume` discards the checkpointer instead of trusting it** (`cli/main.py:269-291`). Reads `channel_values` from prior checkpoint, builds a cleaned state dict, calls `cp.adelete_thread(thread_id)`, then re-streams from initial-state-like dict. Effectively replays the whole graph (the runs-counter in `test_resume_fan_out.py` still pins this: `_read_runs("a") == 2` after resume). The visible symptom of `completed_nodes` accumulating duplicates was masked by #32 (set-union reducer, 2026-05-19), but bodies still re-execute. Re-reading the design landscape post-#32: the LangGraph-native "pass thread_id to astream" pattern assumes the graph paused mid-execution (via `interrupt()`); a graph that returned terminal-with-failures has nothing to resume from natively. Fully resolving the DAG case requires picking one of three API shapes in WISHLIST #26 (skip-completed-via-prior-run channel, `--resume-from <node>`, JSONL-driven skip). Defer until that design call lands.

- [x] **(32) `completed_nodes` / `failed_nodes` use `operator.add` reducer** — _delivered, 2026-05-19._ Switched to `_merge_sets` (set-union); TypedDict annotations changed from `list[str]` to `set[str]`. Migration covered 9 source emission sites (`[node_id]` → `{node_id}`), the inline-retry-loop accumulator (`.append` → `.add`), and ~30 test assertions (`== ["p1"]` → `== {"p1"}`). The wave-pattern test lost its `dispatcher_fires == 2` assertion (now impossible to express in state since set-union dedupes); the `dispatcher::q_gamma` presence assertion is the load-bearing regression check that remains. JSONL event derivation untouched (events fire per super-step, not per state entry). Masks the visible symptom of `--resume` accumulation but doesn't fix the underlying replay logic — see #31.

- [x] **(33) `all_deps_completed` manual polling barrier** — _closed as not-a-defect, 2026-05-20._ Original framing (native LangGraph multi-edge join would replace it) doesn't survive scrutiny. Two findings from the team review:
  1. LangGraph 1.0.7's `NamedBarrierValue` join works only for pure static `add_edge` predecessors — `Command(goto)` and conditional edges both bypass it (verified against source).
  2. **Empirically tested**: `Command(goto=X)` does NOT suppress a node's static out-edges — both fire. So the clean conversion ("Decision emits a plain dict + static edge on pass/fail, `Command(goto)` only on retry") is impossible: the static edge into the join target would fire on every retry cycle, tripping the barrier prematurely.
  The only native-join-compatible design is per-gated-node **marker nodes** (`_settled_<dep>`, reached only on terminal outcomes, carrying the static edge). That adds ~40-60 lines + a synthetic node per gated node — strictly more complexity than the 10-line `all_deps_completed` guard it would remove. `all_deps_completed` is non-idiomatic but correct, small, and doing a job LangGraph genuinely lacks a cleaner primitive for. Revisit only if a concrete defect surfaces.

## Hardening — structural footgun checks (2026-05-20)

Inspired by a "structural backpressure" review: deterministic,
machine-checkable constraints beat behavioral instructions. Several
entries in CLAUDE.md's "Known limitations" are *author footguns* —
gotchas the workflow author must remember, with nothing in `validate`
to catch a violation. The structural fix is to move each check into
the compile/validate boundary. Warn at minimum; hard-error only where
the construct is unambiguously broken.

- [~] **(34) Compile-time footgun checks for documented gotchas** —
  partial. Warning channel + hyphenated-id check delivered 2026-05-20.
  - [x] **Warning channel** — `compile/lint.py::collect_warnings`
    (pure, langgraph-free) + `cli/main.py::_emit_warnings` printing
    yellow `warning:` lines to stderr. Wired into both `validate` and
    `run` (covers `--dry-run`).
  - [x] **Hyphenated node IDs** — `collect_warnings` flags any
    top-level node id or fan-out final-node id containing `-`
    (subtraction footgun), suggesting the underscore rename. Chose the
    structural ID-only check over a template scan: pure, zero-I/O,
    safe on every `run`. Top-level config only — subgraph configs are
    not loaded.
  - [ ] 🤞 **`{{sender_id}}` on a non-goto-reachable node** —
    `_route_sender` is last-write-wins; a node reached by a static
    `depends_on` edge *after* an inline-route hop elsewhere can
    observe a stale `sender_id`. CLAUDE.md tells authors to guard with
    `{% if sender_id %}`. Deferred — needs topology reachability
    analysis and is lower confidence (may be noisy).

## Transport / backend design (2026-05-27)

Design discussion surfaced during PyPI launch. The current backend
axis treats `transport: api` and `transport: acp` as equivalents, but
they have capability-wise different shapes:

- **API backends** (`anthropic`, `openai`, `deepseek`, `custom`) are
  text → text. The model has no filesystem, no tool use, no local
  context. Workflow nodes that assume an agent with worktree access
  (most of them) silently degrade to "transform input text into
  output text." Honest fit is *stateless transforms* (classify,
  score, JSON-shape a piece of text) — not producer nodes.
- **ACP backend** is the agent-with-local-context shape the project
  was designed around.

- [~] **(35) `transport: cli` + ACP value reassessment** — partial.
  _Investigation closed + cli implementation landed in 0.3.0
  (commits 7855731..ad12c89). See findings in
  `docs/investigations/transport-context-parallelism.md`._ Both
  transports coexist; ACP retirement is **deferred** pending real
  workflow soak (current default in the jokes example is cli, acp
  available via `--preset acp`). Original investigation note
  retained below for context.
  **Open investigation gating priority:** is `cli` *additive* (a third
  transport alongside acp) or *replacement* (consolidate on cli, retire
  acp)? Until we know, scope and breakage profile are unclear, which
  lowers priority. Four questions to answer empirically (see
  `docs/investigations/transport-context-parallelism.md` for the
  detailed plan):

  1. **`new_session()` cost:** does ACP's `new_session()` reset
     context *in-process* (server-side state op, ~ms — think Claude
     Code's `/clear`) or *fork a fresh process* (full cold-start)?
     The first lets sqrlly clear context per node with no cold-start
     hit, preserving the process-warmth advantage. The second
     collapses ACP's edge over CLI.
  2. **Real cold-start numbers:** audit estimates (5s CLI / 7s ACP)
     are structural, not measured. Direct `time` runs on `claude -p`
     and ACP `_ensure_initialized` would settle the N-node crossover.
     Working hypothesis: actual numbers are lower than estimates.
  3. **Per-branch ACP for real parallelism:** foreman allocating one
     `ACPBackend` per `(preset, branch_id)` instead of per preset.
     Worktrees are already per-branch — not blocked there. Worth
     measuring vs CLI projection.
  4. **Optional context retention** (`settings.context_mode:
     isolated | shared`): pipelines (research → outline → write)
     might genuinely benefit from accumulated conversation history.
     If retained as an opt-in, ACP gains a user-visible
     differentiator CLI can't trivially replicate.

  **`transport: cli` (agent-CLI subprocess).** Add a third transport
  shape: `subprocess.run([cli_for(provider), …print-mode flags,
  "--model", model], input=prompt, …)`. Same "agent with local
  context" capability as ACP, but a plain subprocess — no stream
  protocol, no process-tree cleanup, no `aclose` warnings, no
  soak-under-load concern. Provider table: `anthropic` → `claude -p`,
  `openai` → `codex exec` (pin syntax at impl time), `google` →
  `gemini -p` (adds `google` to the provider literal). Escape hatch:
  `transport: cli, provider: custom, cli_command: "aider --message
  {{prompt}}"` mirrors the existing `api_base_url` constraint
  pattern on `transport: api, provider: custom`.

  **ACP value-add reassessment.** What ACP currently buys that
  `transport: cli` wouldn't:
  - Session persistence across prompts — *doesn't matter* in
    sqrlly's independent-per-node model; no warm state to reuse.
  - Streaming `session_update` events (tool calls in flight, plan
    updates) — *could matter* if we grew "live progress in the JSONL
    log"; not surfaced today (we only keep the final text).
  - Programmatic per-tool permission control — *could matter* for
    policy enforcement; not used today (we auto-approve everything
    in `_ACPCallbacks.request_permission`).
  - Declarative MCP servers via `new_session(mcp_servers=[...])` —
    CLI equivalent is `--mcp-config`. Same capability, different
    surface. We pass `[]` today.
  - Multi-vendor portability — ACP is a Zed-designed *protocol*, in
    theory implementable by codex / gemini. As of 2026-05 only
    `claude-code-acp` ships publicly. *Hypothetical* until that
    changes.

  None of these are load-bearing today. ACP earns its weight only if
  (a) we surface mid-flight events to the log, or (b) other vendors
  ship ACP servers. Until either, ACP is the protocol whose stream we
  don't consume.

  **Migration option once `cli` lands.** Run `cli` + `acp` in parallel
  for a release or two; if nothing trips on the difference, retire
  `transport: acp` (deletes the backend, the conftest pre-flight, the
  npm dependency note, the soak concerns in WISHLIST 49/53/54). Frees
  roughly the process-tree-cleanup surface and ~400 lines.

  **Text-pipe CLIs are NOT in scope for `transport: cli`.** Tools like
  `ollama run` / `llm` / llama.cpp's `main` are text-in/text-out with
  no agent shape — they belong in `transport: api` + `provider:
  custom` against the tool's OpenAI-compatible HTTP server (Ollama,
  vLLM, llama.cpp all ship one).

  **Order of operations** if any of this becomes real work:
  1. Doc-only: README/SKILLS clarifies the API-vs-agent distinction.
     Zero code.
  2. `transport: cli` backend — focused add (~200 lines + tests).
     Stands on its own.
  3. Contracts arc lands (existing WISHLIST: *Schema enforcement at
     backend boundary*, *Schema sources*, *Schema-first templates*).
     Independent of the transport work, and the gating piece for
     **making API mode useful for producer-shaped nodes via
     structured returns**. The inline-context alternative (bake file
     contents into prompts via `{{dep_id}}` extensions) is a smaller
     patch but doesn't scale to large workdirs and re-eats tokens on
     every retry.
  4. Revisit `transport: acp` retirement.

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

Five gaps surfaced when auditing test/example coverage of recently-
landed features. Tracked separately from the audit-finding numbers
above because they're additive (no production-code change) rather
than fixes to existing code.

- [x] **(22) Wave-driven dynamic-task pattern lacked an example and
  a permanent test.** The pattern was proven via a one-shot spike
  (`.temp/wave_spike.py`) but had no examples-gallery entry and no
  e2e regression test. Delivered `examples/wave_planner/` (workflow
  + 4 deterministic Python scripts + README) and
  `tests/e2e/test_wave_pattern.py` (mutation-tested: temporarily
  re-adding the resume-mode `completed_nodes` guard at
  `compile/nodes.py::_make_execution_node` reproduces the original
  regression as a `GraphRecursionError`). Folded
  `memory_threshold_pct` into the example's settings as documentation
  surface for that feature. Spike file deleted as part of the
  example landing.
- [x] **(23) Live-backend e2e round-trip** — _delivered._
  `tests/e2e/test_live_backend_roundtrip.py` exercises the full CLI
  pipeline (Click runner + `AsyncSqliteSaver` + `DispatchExecutor` +
  real backend) against `examples/jokes/workflow.yaml` parametrized
  over all four backends (`anthropic` / `deepseek` / `openai` /
  `custom`). Marked `pytest.mark.live`; per-backend skip when the
  matching API key is absent — runs cleanly offline (zero
  parametrized cases execute) and gradually fills in coverage as
  keys get configured. Asserts only structural properties (exit 0,
  both nodes complete) so it stays robust to model output drift.
- [x] **(24) DeepSeek model-availability drift** — _delivered._
  `tests/unit/runtime/test_openai_backend.py::test_alias_table_resolves_in_live_catalog`
  (line 69-92) mirrors the Anthropic test: queries `client.models.list()`
  against DeepSeek with the test-pinned `deepseek-v4-flash`; fails
  with a clear "update the test fixture" diagnostic when the model
  retires. Skipped when no DeepSeek key is on disk.
- [x] **(25) Resume-from-checkpoint e2e** — _delivered._
  `tests/e2e/test_resume_fan_out.py` exercises a real
  `AsyncSqliteSaver` round-trip across two phases with mid-chain
  failure injection. The runs-counter side-channel pins exactly how
  many times each node body executes. Surprising finding: ``a``'s
  counter goes from 1 to 2 — see (26) below.
- [ ] 🚨 **(26) `--resume` semantics are underspecified for goto-driven
  workflows** — _surfaced by (25); design question, not a bug._
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

- [x] **Unify gate-eval via outcome-as-routing-signal** — _landed, Stages 3 + 3b._ Top-level phases use the data-driven model from `compile/evaluation.py` (`Criterion`, `Route`, `walk_routes`, `gate_to_routes`). `classify_gate_outcome` walks routes; `state.evaluations: {node_id: [EvaluationRecord]}` is the single source of truth for scores/feedback. Stage 3b (branch `stage-3b-evaluation-node`) completed the picture: gated top-level phases are a graph pair (`phase` execution node → `_eval_{phase}` evaluation node via `_make_evaluation_router`); gated subphase templates evaluate inline within the Send-dispatched subphase node (graph-level self-loops strip `_subphase_item` at the super-step boundary, so inline retry is the only shape that preserves per-branch identity); legacy `gate_scores`/`gate_feedback` state channels removed outright. Subphase gates honor `max_retries`, write `EvaluationRecord`s with real `invocation` counters, and log per-dimension scores.

- [x] **Phase → Node terminology + Recursive subgraphs + Join nodes** — _landed, Stage 4 (branch `stage-4-node-recursive-subgraphs`)._ Hard cutover on YAML and Python: `phases:`→`nodes:`, `Phase`→`Node`, `WorkflowConfig`→`Graph`, `dynamic_subphases:`→`fan_out:`, `final_phases:`→`final_nodes:`, `quality_gate:`→`evaluation:` (alias dropped). State channels and helpers renamed to match (`phase_outputs`→`node_outputs`, `_make_phase_node`→`_make_execution_node`, etc.). Stage 4b: `execution: { type: join }` no-op topology marker. Stage 4c: `Node.config:` references another graph YAML; recursively compiled via `add_node(node.id, compiled_subgraph)`. State projection via explicit `inputs:`/`outputs:` declarations; subgraph runs in isolation. Compile-time cycle detection + `settings.max_subgraph_depth=10` cap. 488 tests passing.

- [x] **Split Evaluation from Decision** — _delivered (Stage 5d, 2026-05-08)._ Top-level gated nodes now compile to `exec → _eval_<id> → _decide_<id>` with plain edges. The Eval node body writes only `state.evaluations[node_id]` (an EvaluationRecord); the new Decision node reads that record, classifies the outcome via `classify_evaluation_outcome`, and returns `Command(update={completed/failed/retries/errors}, goto=target)`. `_make_evaluation_router` deleted; `add_conditional_edges` for the eval pair removed entirely. Mirrors the existing `_make_inline_route_node` Command pattern.

    The four unlock cases (refinement, multi-eval consensus, human-in-the-loop, cross-phase evaluation) are all now *topologically expressible*: an author or downstream tool can insert nodes between Eval and Decision (e.g. consume the EvaluationRecord, emit a revised draft, then route to Decision OR back to executor). First-class authoring API for these patterns (e.g. an `evaluation: { refine: <node_id> }` schema) is iteration 2 — separate plan.

    **Subphase inline retry loops** (`compile/dynamic.py::_make_fan_out_node`) stay untouched — independent subsystem with its own per-Send-branch state model. **Dynamic gated parents** keep the pre-Stage-5d combined factory (`_make_combined_eval_decide_node`) because their downstream is a conditional-edge dynamic_router that needs `completed_nodes`/`failed_nodes` already in state; inserting a Decision node there would fragment manifest dispatch. Both deferrals documented inline.

    Tests: 14 new in `test_decision_node.py` (Command emission per outcome + guards + subphase resolver + history-latest); existing `test_evaluation_node.py` migrated to assert eval-only contract; `test_evaluation_router` class deleted (router gone); `test_graph_shape.py` topology tests updated to expect plain `eval → decide` edges with no conditional edges. 878 tests total green (was 875).

- [ ] **Collapse `runtime/executor/backends/` → `runtime/backends/`** — 4-level nesting (`runtime/executor/backends/acp.py`) for 4 small files. Semantic loss: current nesting signals that only `PromptExecutor` uses backends. If we land the anthropic/openai backends (below), the signal still holds but less strongly — multiple executor types might route through one backends/ module. Low value, low risk; defer until a second executor family justifies the flattening.

- [ ] **Fold `compile/dynamic.py` into `compile/nodes.py`** — 182 LOC would bring `nodes.py` to ~530 LOC. The split is defensive today: `_make_fan_out_node` has legitimately divergent semantics (no dep check, no output contract, no retry routing). **Worth revisiting after** the gate-eval unification above — if the gate block is gone and the final remaining divergence is "Send-triggered vs. normal-invocation," the split stops earning its keep.

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

- [x] **Reconsider dependency/ordering model** — _delivered, post-Stage-5d._ Every sub-bullet has landed: (a) phases as a YAML construct are gone — schema is `nodes:` + `fan_out:` (Stage 4); `cli/migrate.py` carries the one-time legacy YAML migrator. (b) QualityGate-as-special is gone — `evaluation:` is a sub-config on any Node, and Stage 5d split it into separate `_eval_<id>` + `_decide_<id>` nodes in the compiled graph. (c) retry-with-context is the route chosen by the Decision node via `Command(goto=exec_id)` with retry context surfaced through `_route_eval_preamble` and `inject_retry_reason`. (d) escalation tiers do not exist — single `retries:` budget; `rg "escalated|super_escalated|retry_tier" src/` returns zero hits. Naming cleanup pass (2026-05-15) renamed remaining internal `subphase` tokens to `branch` and dropped the dead `_subphase_id_resolver`.

- [x] **ACP test flakiness** — _closed for now, 2026-05-15._ Likely-root-cause fix landed in `5de1166` (Python 3.14 ACP `aclose()` warning handled via `/proc`-walk descendant reaping in `ACPBackend.close()`). Two consecutive `tests/acp/` runs on 2026-05-15: 14/14 each, 150s and 160s — close enough timings to rule out race-amplified hangs in this environment. Reopen if symptoms return; the parent item below (soak-test under load with `max_parallel_jobs > 1`) is the formal validation gate.

- [x] **ACP process-tree leaks / zombie subprocesses under long runs** — _initial fix landed in post-Stage-5b. `ACPBackend.close()` now captures descendants from `/proc/<pid>/task/<pid>/children` BEFORE `__aexit__` (so re-parented orphans stay tracked), runs graceful shutdown under a 5s `wait_for`, then SIGTERM→0.5s→SIGKILLs each captured PID. Teardown assertion `tests/acp/test_acp_cleanup.py::test_close_reaps_descendant_tree` watches a 15-PID descendant tree disappear within 3s of close. **Open: soak-test under load** — needs a multi-hour run with `max_parallel_jobs > 1` against the absurd-paper workflow before this can be fully ticked off._

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

### Orchestrator join semantics

- [x] **Multi-gated-predecessor join bug** — _Stage 1, 2026-04-17._ `_make_phase_node::node_fn` now returns `{}` when any dep is missing from `completed_phases`. LangGraph re-fires the node on each subsequent pred completion; missing-pred returns turn the node into a natural join barrier. `examples/absurd-paper/` runs cleanly with natural topology (commit `593d1c3`). Regression test: `tests/e2e/test_orchestrator.py::TestParallelExecution::test_multi_gated_predecessor_joins_correctly`.

- [x] **Subphase context doesn't inherit parent's upstream deps** — _Stage 2a, 2026-04-17._ `_make_subphase_node` now calls `build_context(parent_phase, state)` before layering in item fields, so subphase templates see the full upstream chain. Regression test: `tests/e2e/test_dynamic.py::TestManifestFieldPropagation::test_subphase_context_inherits_parent_deps`.

- [x] **Final-phase output unreachable from downstream non-fan-out phases** — _Stage 2b, 2026-04-17._ `build_context` now synthesizes `{dep}_subphases` and `{dep}_subphase_worktrees` directly from `state.subphase_outputs` / `state.phase_worktrees`. Any downstream (final or otherwise) depending on a dynamic parent sees the same aggregate. `_make_final_phase_node` collapsed from ~25 LOC to a thin alias. Regression test: `tests/e2e/test_dynamic.py::TestManifestFieldPropagation::test_downstream_sees_subphase_aggregate`.

### Data-flow gaps

- [x] **Command phase `args` are not Jinja-templated** — _Stage 2c, 2026-04-17._ `CommandExecutor.execute` now renders each arg through `render_template(arg, context)` before building `cmd`. Plain strings pass through. `command` itself is not templated (security: keeps binary choice static). Regression tests: `tests/unit/runtime/test_command_executor.py::TestCommandExecutor::test_args_are_jinja_rendered` and `test_args_without_templating_render_literally`.
    - Separately: consider also templating `env` additions or piping dep outputs to stdin for command phases — would unlock simple Python-script "aggregator" phases.

- [x] **Gate validators can't see dep outputs; gate-only phases have no useful signal** — _delivered 2026-05-08._ `runtime/gates.py::run_evaluation_*` now accept `dep_outputs` / `dep_structured_outputs` / `dep_worktrees` kwargs threaded through `compile/nodes.py::run_evaluation_and_outcome`. Script gates project them to `DEPS_JSON` / `DEPS_STRUCTURED_JSON` / `DEPS_WORKTREES_JSON` env vars. LLM gates bind each dep by id directly in the Jinja context (matches `build_context`'s convention) plus `_deps` / `_dep_structured` aggregates. Dep scoping mirrors `build_context`: gates see their node's declared `depends_on` outputs only; gate-only phases (no execute, no deps) see all completed outputs (the bug case). Tests: 8 new in `test_gates.py::TestGateDepOutputs` + `TestGateScopingByDeps`, plus `tests/e2e/test_gates_dep_access.py` proves a gate-only phase can validate upstream content end-to-end without the `$WORKDIR` filesystem-read workaround. Subphase gates in `compile/dynamic.py::_make_fan_out_node` were left untouched — independent subsystem with its own context model; revisit when use cases surface.

### Observability

- [x] **Multi-dim gate `score` logged as 0.0 even when dimensions pass** — _Stage 3, 2026-04-17._ `gate_evaluated` events now source from `state.evaluations` (real evaluation records) and carry a `scores` dict with per-dimension values alongside the top-level `score`. Regression test: `tests/unit/workflow/test_logging.py::TestLogSnapshot::test_detects_multidim_gate`.

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

- [x] **Workflow run visualization tool** — _delivered 2026-05-08
  (MVP)._ `sqrlly view <yaml> [--log <jsonl>] [--out <path>]
  [--direction TB|LR|BT|RL]` emits a self-contained HTML page with
  custom Mermaid emission (not LangGraph's, for layout control and
  author-perspective output skipping synthetic `_eval_<id>` /
  `_route_<id>` nodes). Authoring mode (no log): topology + per-
  node config inspector. Debug mode (with log): adds status overlay
  (passed/failed/retried/untouched), goto-re-fire chip
  (`fired N×`), retry chip, last-error display, full event slice.
  Layout uses a Mermaid subgraph block with invisible spine edges
  `START ~~~ workflow ~~~ END` for predictable direction. Bonus
  fix: runner's `log_update(None)` crashed on Command-only nodes;
  guard added.

  Iteration 2 still on the table (deferred): time-slider replay,
  `--follow` live mode, explicit `goto_fired` / `send_dispatched`
  events for animated arrows, drill-down to per-node worktree
  commits (depends on the integrated checkpoint story).

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

- [ ] **Agent skills draft creation primitive** — surfaced 2026-05-06. Authors today encode "small reusable instruction modules" (skill drafts — short Markdown bundles describing a tool/role/workflow + example invocations) inline as prompt files plus per-node Jinja context. As more workflows reuse the same skill across nodes, three patterns emerge: (a) duplicate the .md across prompts, (b) `{% include %}` it, (c) handcraft a meta-prompt that asks Claude to first synthesize a skill from constraints and then apply it. (c) is the interesting case — it's the "draft creation" half of an agent-skill lifecycle (draft → apply → critique → revise) that doesn't have a first-class shape today. **What an `agent_skill:` block could look like**: a node-level declaration that the prompt produces a structured skill artifact (path, name, description, invocation example), and downstream nodes can reference it as `{{skills.<name>}}` for inclusion. Pairs with output_contract for the artifact-on-disk form, and with the WISHLIST "Schema enforcement at backend boundary" item for typing the draft. **Open questions**: should the skill be persisted across runs (cross-thread `BaseStore`?) or is it per-workflow scratch? Should the schema enforce a draft → apply → critique → revise loop, or stay loose and let authors compose? Probably investigate against a concrete example workflow (e.g., `examples/agent_skill_draft/` writing a "research-summary" skill once and reusing it across multiple summarize nodes) before designing the schema.


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
    - Today's fan-out almost gets there: a `fan_out:` block already gives N parallel branches with shared template, and `final_nodes:` already sees the aggregate `{{parent_branches}}` map. What's missing is **per-candidate execution config** — each branch needs to be able to override its own model, temperature, prompt, agent vs prompt mode, etc., not just receive different per-item context. Today the manifest can rename the persona via `{{style}}` template substitution but can't make candidate A run on opus while candidate B runs on haiku, because `fan_out.template.execute` is a single shape applied to every branch.
    - **Schema gap to close**: let `fan_out.template.execute.params` reference manifest fields, e.g. `params.llm.model: "{{model}}"` so the manifest can supply per-candidate execution config alongside per-candidate context. Pairs with the three-axis llm config above — once `params.llm` is a recognized shape with extra="forbid", per-candidate llm config slots in cleanly.
    - **Synthesis step shape** — two final_nodes:
      1. **judge**: prompt that reads all `{{parent_branches}}`, picks a winner, and emits structured output `{winner: "candidate_b", reasoning: "...", strengths_in_losers: [{candidate_a: "the conclusion is sharper"}, ...]}`.
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

- [x] **Scope-aware settings resolution (prerequisite for everything below)** — _landed post-Stage-5b. `runtime/settings_merge.merge_settings` uses Pydantic v2 `model_fields_set` so child YAML's explicit fields win, parent's flow through. `NodeExecutor.execute` Protocol gained `settings_override: Settings | None`; threaded through `DispatchExecutor` (every read site of `self._settings`), `ForemanExecutor` (per-model semaphore selection), and `PromptExecutor` (`apply_preamble`, `execute_rendered.model_downgrade_chain`). Compile layer: `build_workflow_graph(effective_settings=)`, `_make_execution_node`, `_make_evaluation_node`, `run_evaluation_and_outcome`, fan-out factories all take and propagate. `make_subgraph_node` and `make_fan_out_subgraph_invoker` accept `parent_settings` and call `compile_fn(..., effective_settings=merge_settings(parent, sub))`. Tested by 10 unit tests + 6 e2e (incl. real-subprocess `default_timeout` proof: subgraph override lets `sleep 1.5` pass under parent's 1s; reverse polarity kills `sleep 2`). Critical bug caught en route — the inner `compile_fn` closure was forwarding the OUTER scope's settings into recursive build calls instead of the inner scope's; the artifact-driven sleep test pinned it._

- [x] **Default executor should be real, not stub** — _landed post-Stage-5b. `auto_detect_executor()` in `factory.py` walks `ANTHROPIC_API_KEY` (placeholder) → DeepSeek key (env or `~/.pi/agent/auth.json`) → `npx` on PATH → `stub` with a `UserWarning` naming concrete remediation. `Settings.executor: str | None = None` (was `"stub"`); the CLI does `executor or settings.executor or auto_detect_executor()`. Explicit `-e stub` or `executor: stub` in YAML never triggers the fallback warning — stub stays usable for offline testing, just no longer the default. Manual artifact gate: `sqrlly run examples/jokes/workflow.yaml` (no `-e` flag) now auto-picks DeepSeek (since the key is on disk) and produces real output (not `[prompt-stub]`)._

- [x] **Named presets (was: three orthogonal axes for LLM execution)** — _delivered, 2026-05-19, three commits: ba85287 (schema foundation), 8f1a3d0 (runtime wiring), this rework's final commit (cutover)._ Honest re-scope: the matrix is mostly diagonal (acp implies agent; api implies prompt), so the "three axes" framing was retired in favor of **two axes** (transport + provider/model) bundled into named ``settings.presets``. Nodes reference one via ``params.preset:``; the preset marked ``default: true`` applies otherwise; ``--preset/-p`` CLI flag overrides. Hard cutover — ``Settings.executor``, ``Settings.default_model``, ``Node.model``, ``PromptParams.model``, ``--executor/--model`` CLI flags, and ``sqrlly migrate`` subcommand all removed. The ``migrate.py`` module remains as an internal utility; one-time migration walked all ``examples/`` workflows automatically. 914 tests passing. Subgraph preset inheritance via the existing scope-aware ``model_fields_set`` path (subgraph without ``presets:`` inherits parent's; subgraph with ``presets:`` replaces; key-wise additive merge deferred).

- [x] **Command presets — named interpreters for script nodes (was: D4
  discriminated union)** — _delivered, 2026-05-20, two commits: `eb6aeb5`
  (schema) + dispatch/tests commit._ `settings.presets` is now a
  `kind`-discriminated union of `LlmPreset` + `CommandPreset`. A script
  node names a command preset via `params.preset:`; dispatch runs the
  preset's command string (`shlex.split` + `{{file}}`/`{{args}}` token
  placeholders, default-append when absent) instead of the
  extension-map interpreter. Resolved design (Q1-Q5): `default` is
  LLM-only (no `default` on CommandPreset; script nodes opt in by name);
  command preset and `execute.mode` are mutually exclusive (validated);
  `SubprocessParams.preset` added; callable discriminator defaults
  absent `kind` to `llm` (zero migration). `examples/command_preset/`
  demonstrates `uv run`.

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

### Backends to add (lower priority once axes above land)

- [x] **Direct Anthropic API backend** — _landed alongside StubBackend removal._ `runtime/executor/backends/anthropic.py` (~160 LOC) implements `PromptBackend` via `AsyncAnthropic`. Generic model alias table (`sonnet` / `opus` / `haiku` → vendor IDs) with pass-through for explicit pins. `OverloadError` mapping for transient failures (status 429 / 502 / 503 / 504 / 529 + class-name fallback `RateLimitError` / `APIConnectionError` / `APITimeoutError` / `InternalServerError` / `OverloadedError`). Optional dep — install with `uv sync --extra anthropic`. Auto-detect picks it first.

- [x] **OpenAI-compatible backend** — _landed post-Stage-5b. `runtime/executor/backends/openai.py` (~105 LOC) implements the `PromptBackend` Protocol via the `openai` SDK with overridable `base_url`. Validated end-to-end against DeepSeek (`base_url=https://api.deepseek.com/v1`, model `deepseek-v4-flash`) — auto-detect picks it up via `~/.pi/agent/auth.json`. Maps 429/502/503/504/529 + `RateLimitError` + `APIConnectionError` → `OverloadError` (activates the existing model-downgrade chain). Optional dep — install with `uv sync --extra openai`. Three-axis LLM config sketch above is still TODO; this backend is the first concrete demo that decouples provider/model from the ACP transport. Unlocks OpenAI, Azure OpenAI, Ollama, vLLM, llama.cpp, LM Studio, LiteLLM via the same backend with `base_url` overrides._

- [ ] **Wire `OverloadError` through `ACPBackend`**
    - Translate ACP 429 / 529 / overload codes so the existing downgrade path fires with ACP too

- [ ] **`claude -p` (Claude Code CLI print-mode) backend** — a third option alongside `ACPBackend` (JSON-RPC over stdio) and `AnthropicBackend` (HTTP SDK). `claude -p "prompt"` is Claude Code's non-interactive surface: single-shot completion, but runs through the **locally-configured Claude Code session** (its MCP servers, allowed tools, project-local settings.json). That's the differentiator vs. `AnthropicBackend` — the prompt has the user's existing tool/MCP environment available without our needing to re-plumb any of it. Fits the deferred three-axes model as `protocol: claude-cli + mode: prompt + provider: anthropic`. Implementation sketch: ~80 LOC `ClaudeCliBackend` that shells out to `claude -p`, captures stdout, maps non-zero exit + known stderr patterns to `OverloadError`. Auto-detect chain entry: `claude` on PATH (sibling to today's `npx @zed-industries/claude-code-acp` check). Open design question: how to thread per-call model selection — `claude -p --model sonnet "..."` exists but isn't always honored if the session is pre-bound; needs probing against the installed version.

- [ ] **Streaming on `PromptBackend`**
    - Optional `async stream_prompt(...) -> AsyncIterator[str]`
    - Live progress in CLI and JSONL log

## Langgraph adoption wins

- [ ] **`Command` objects for node-level routing** — paired with the Evaluation/Decision split (top of file). A node returns `Command(update=..., goto=...)` instead of writing state and being routed by a downstream conditional edge. Removes router closures across `compile/graph.py` and makes the topology self-describing — the destination lives in the node return, not in a separate function reading state we just wrote.

- [x] **`stream_mode="updates"` in runner + logging** — _delivered._ `runtime/runner.py` now drives logging from `astream(stream_mode=["updates", "values"])` (tuple form supported in LangGraph 1.0.7); `compile/subgraph.py` switches its two `astream` call sites the same way. `JsonlLogger.log_snapshot(prev, curr)` was replaced with `log_update(node_name, update)`; `_PrefixingProxy` deleted (the prefix is now applied to `node_name` before dispatch). The 6 emitted event types and their schemas are unchanged for downstream consumers.

- [ ] **Interrupts / human-in-the-loop** — `interrupt()` + `Command(resume=...)` from langgraph. Free on the existing checkpointer; enables author/operator approval nodes, manual quality gates, draft review. New execution type (`type: human_review`) or `evaluation.mode: human` schema option.

- [x] **Subgraphs with declared entry/exit** — _landed, Stage 4c (no separate execution type)._ User clarified during planning: graphs and subgraphs are definitionally identical, so a node references another graph YAML via `config:` rather than getting tagged as a `subgraph` type. Recursion falls out naturally. See "Nodes as proper langgraph subgraphs" above.

- [ ] **`add_messages` reducer for in-phase refinement loops** — multi-turn draft → critique → revise within a single phase using LangGraph's native message-list reducer. Phase-local `messages` channel; no ACP round-trip per turn for pure model revision.

- [ ] **Time-travel replay in CLI** — `sqrlly replay <thread-id> --from <checkpoint>`. Checkpointer already persists every super-step; we just don't expose it. Enables A/B of executor changes against the same past state, bisecting regressions, reproducing flakes.

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

Audit of where we shadow LangGraph functionality. Most of our code is genuinely complementary (timeouts, semantic retries, concurrency caps, custom reducers) — these two items are not.

- [x] **Stop diffing state in `runtime/logging.py`** — _delivered._ Snapshot-compare path deleted. Events key directly on the `node_name → update` pairs the stream emits. Removed `_PrefixingProxy` along with `log_snapshot`; the new `log_update(node_name, update)` is shared by `JsonlLogger` and `SubgraphLogger` via a free helper.
- [~] **Stop hand-writing router closures** — partial. `_make_evaluation_router` deleted (Stage 5d Eval/Decision split — Decision nodes emit `Command(goto=...)` directly). `_subphase_id_resolver` deleted (2026-05-15, was dead code). Still open: the dynamic-router closure and conditional-edge scaffolding it feeds in `compile/graph.py` for dynamic gated parents (blocked on item #29).

**Not reimplementation** (clarified during audit, kept for reference):

- Eval-score-driven retries (ours) vs `RetryPolicy` (exception-driven) — complementary, not duplicative.
- `_merge_dicts` / `_merge_evaluations` reducers — LangGraph offers no dict-merge or per-key list-append natively.
- Timeouts (`asyncio.wait_for`), concurrency caps (`asyncio.Semaphore`), worktree pool — outside LangGraph's scope.
- Thread ID derivation from `(workflow_name, workdir)` — policy choice, not a feature we shadow.
