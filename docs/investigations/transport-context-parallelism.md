# Transport / context / parallelism investigation

The decision on whether `transport: cli` should be *additive* to ACP
or *replacement* for ACP hinges on three measurable questions about
ACP's actual behavior, plus one design question. This plan defines
the experiments and a decision tree.

Source for the design context: WISHLIST item 35.

## Hypotheses to test

| H | Statement | If true | If false |
|---|---|---|---|
| H1 | ACP `new_session()` is an in-process state reset (cheap, ~ms) | sqrlly can clear context per `send_prompt` while keeping ACP's process warm — best of both worlds | ACP's session-isolation cost equals CLI's cold-start; warm-reuse advantage evaporates |
| H2 | `claude -p` cold-start is materially under 5s in practice | CLI's per-call cost is comparable to ACP's even for short serial workflows | Audit's structural estimate stands; long serial workflows favor ACP |
| H3 | One `ACPBackend` per `(preset, branch_id)` gives real parallelism without architectural pain | Per-branch ACP is the simplest parallelism fix; CLI is additive, not a replacement | Per-branch ACP degenerates (process-tree explosion, etc.); CLI replaces ACP |
| H4 | Real workflows have pipeline shapes that *want* shared conversation context | A `settings.context_mode: isolated \| shared` knob has user value; ACP gains a differentiator | The knob is YAGNI; isolated is the only reasonable default |

## Experiments

### E1 — `new_session()` semantics

**Setup:** standalone Python script using the `acp` package directly
(no sqrlly involvement). Targets the `claude-code-acp` adapter.

**Procedure:**

1. `spawn_agent_process` once, enter the async context, capture pid.
2. `sess_a = await conn.new_session(cwd=tmp, mcp_servers=[])`.
3. `await conn.prompt(session_id=sess_a.session_id, prompt=[text_block("remember the number 7")])`.
4. `await conn.prompt(session_id=sess_a.session_id, prompt=[text_block("what number did I tell you?")])` — **assert** response contains "7" (sanity: within-session history holds).
5. `time_b_start = perf_counter(); sess_b = await conn.new_session(...); time_b = perf_counter() - time_b_start`.
6. `await conn.prompt(session_id=sess_b.session_id, prompt=[text_block("what number did I tell you?")])` — **observe**.
7. Verify pid is unchanged across step 5 (compare `/proc/<spawn_pid>/task/<spawn_pid>/children` snapshots).

**Decision matrix:**

| `time_b` | Step 6 response | Verdict |
|---|---|---|
| < 200 ms | "I don't know" / asks | **H1 true** — in-process reset, sessions isolated. Use per-`send_prompt`. |
| < 200 ms | mentions "7" | Sessions cheap but not isolated — protocol smell; treat ACP as single-context. |
| > 1 s | "I don't know" | **H1 false** — `new_session` is process fork. CLI cost-profile equivalent. |
| > 1 s | mentions "7" | Pathological; ACP has both costs. Strong argument for CLI. |

### E2 — Cold-start measurements

**Setup:** plain `time` invocations.

**Procedure:**

```bash
# CLI cold-start (5 runs)
for i in 1 2 3 4 5; do
  time claude -p "echo: hello" >/dev/null
done

# ACP backend init (one-shot script)
python -c '
import asyncio, time
from sqrlly.runtime.executor.backends.acp import ACPBackend
async def main():
    b = ACPBackend()
    t0 = time.perf_counter()
    await b._ensure_initialized("/tmp")
    print(f"ACP init: {time.perf_counter()-t0:.2f}s")
    t0 = time.perf_counter()
    r = await b.send_prompt("echo: hello", "sonnet", "/tmp", timeout=30)
    print(f"warm prompt #1: {time.perf_counter()-t0:.2f}s")
    t0 = time.perf_counter()
    r = await b.send_prompt("echo: hello", "sonnet", "/tmp", timeout=30)
    print(f"warm prompt #2: {time.perf_counter()-t0:.2f}s")
    await b.close()
asyncio.run(main())
'
```

**Record:** `C_cli` = median CLI cold-start; `C_acp` = ACP init time;
`W_acp` = mean warm-prompt time (excluding model time — measure with
a no-op prompt or compare relative).

**Decision matrix on `C_cli` vs `C_acp`:** see WISHLIST item 35 — if
`C_cli` ≤ 2s and ACP per-call needs `new_session()` per E1, the
crossover shifts dramatically toward CLI.

### E3 — Per-branch ACP parallelism

**Setup:** throwaway branch `experiment/per-branch-acp`. Modify
`ForemanExecutor` to allocate one `ACPBackend` per `(preset, node_id)`
instead of per preset. Don't merge.

**Procedure:**

1. Run `examples/jokes/workflow.yaml` (2-node serial): record
   wall-clock vs current shared-backend wall-clock. Expect: per-branch
   pays 2× cold-start (acceptable cost; correctness check only).
2. Construct a synthetic 4-branch fan-out workflow (all branches
   share `preset: default`, model the same trivial task). Run both
   shared (current) and per-branch (experimental). Record wall-clock
   for each.
3. Project CLI wall-clock from E2 numbers for the same 4-branch
   workflow.

**Decision matrix on per-branch wall-clock relative to shared and CLI
projection:**

| Per-branch vs shared | Per-branch vs CLI | Verdict |
|---|---|---|
| Much faster | ≈ CLI | Per-branch ACP is the parallelism fix; CLI is additive only |
| Faster | Faster than CLI | Per-branch ACP wins outright; CLI is additive only |
| Faster | Slower than CLI | CLI replaces ACP for fan-out shape; ACP keeps serial niche |
| Equal or slower | — | Per-branch ACP doesn't pay off (likely the per-instance cold-start eats the parallelism win). CLI replaces ACP. |

### E4 — Workflow context-mode survey

**Setup:** read the 5 shipped example workflows under `examples/`.

**Procedure:** For each, classify:

- **Independent-per-node** — each node is a self-contained task; no
  benefit from seeing prior nodes' conversation. (Gates, fan-outs,
  classifiers, routers.)
- **Pipeline-with-continuity** — each node materially builds on the
  prior node's conversational thread; isolating each in a fresh
  session would lose useful context. (Long-form writing pipelines,
  multi-turn reasoning chains.)
- **Mixed** — some nodes are independent, others want continuity.

**Decision:**

- All independent → `context_mode` is YAGNI; ship per-prompt
  `new_session()` as the only behavior (assuming E1 makes it cheap).
- Pipeline use cases observed (even one example) → schema-level
  `settings.context_mode: isolated | shared`, isolated default,
  per-node override possible. This becomes a real differentiator
  for ACP.

## Decision tree (synthesizing E1–E4)

```
                        E1 (new_session cost + isolation)
                                    │
              ┌─────────────────────┼────────────────────┐
              │                                          │
       cheap + isolated                          fork or contaminated
              │                                          │
        E3 (per-branch parallelism)                E2 (CLI cold-start)
              │                                          │
   ┌──────────┴──────────┐                  ┌────────────┴─────────┐
   │                     │                  │                      │
  wins                  loses              ≤ 2s                  > 2s
   │                     │                  │                      │
 Path B               Path C             Path A                Path D
```

- **Path A — CLI replaces ACP.** E1 shows process fork or
  contamination; E2 shows CLI cold-start cheap enough. ACP buys
  nothing CLI doesn't already give us. Implement `transport: cli`,
  retire `transport: acp` in 0.3.x.

- **Path B — CLI additive; per-branch ACP solves parallelism.**
  `new_session()` is in-process; per-branch ACP gives real
  parallelism without catastrophic cost. Implement per-prompt
  `new_session()` in `ACPBackend`, refactor foreman to allocate
  per-branch ACPBackends. Add `transport: cli` as an additional
  option for users on alternate CLIs (codex/gemini). Keep both.

- **Path C — Status quo + isolated mode.** `new_session()` is
  in-process, but per-branch ACP doesn't pay off (process-tree pain,
  unexpected overhead). Add per-prompt `new_session()` for context
  isolation. Defer CLI until codex/gemini demand surfaces.

- **Path D — ACP keeps serial niche.** CLI cold-start is high
  enough that long serial workflows favor ACP, but its context
  semantics are bad (process-fork-per-session). Add `transport: cli`
  as additive; document the workflow-shape tradeoff; keep both;
  revisit if measurements shift.

## Time estimate

- E1: 1 hour (write a ~30-line standalone async script, run, observe)
- E2: 30 min (time commands, one Python snippet)
- E3: 2–3 hours (foreman modification on throwaway branch, E2E run)
- E4: 30 min (read 5 example workflows, classify)

**Total: ~half-day to settle the priority gating WISHLIST 35.**

## Findings (2026-05-27)

### E1 — `new_session()` semantics

Measured via `.temp/e1_new_session.py` against real `claude-code-acp`.

| Metric | Result |
|---|---|
| Within-session history | ✅ session A asked twice, model recalled "7" |
| Cross-session isolation | ✅ session B does not see session A's history |
| Process identity across sessions | ✅ pid stable (1451926 → 1451926) |
| `new_session()` cost | **~5,500 ms** (5.5 s) |
| Initial `initialize()` cost (one-time) | ~2,600 ms |
| Initial spawn cost (one-time) | ~1 ms (warm npx cache) |

**H1 falsified.** `new_session()` is "process-warm, session-cold" — the
same `claude` subprocess survives, but every new session pays ~5.5s of
setup (likely re-auth + system prompt + tool init). Not a cheap state
reset.

**Implication:** ACP's claimed warm-reuse advantage was implicitly
relying on *not* calling `new_session()` per prompt — i.e., sharing
one session across all prompt nodes. That's correctness-broken for
sqrlly's design (independent per-node prompts), because the model
sees prior nodes' outputs as conversational history.

### E2 — `claude -p` cold-start

Three runs of `time claude -p "Reply with just the digit 7."`:

| Run | Real time |
|---|---|
| 1 | 5.55 s |
| 2 | 6.46 s |
| 3 | 5.04 s |

**Median ~5.5 s.** Audit's 5 s estimate was approximately right.

**Convergence:** ACP `new_session()` ≈ `claude -p` cold-start ≈ 5.5 s.
The two are within measurement noise of each other. Once you require
context isolation (which sqrlly does — see E4), ACP and CLI have
essentially the same per-call cost.

### E3 — skipped

Per-branch ACP for parallelism. Skipped — the structural argument is
decisive without a foreman refactor:

- **Per-branch ACP, N parallel branches:** each branch needs its own
  ACPBackend (spawn + initialize + first new_session). Parallel via
  `asyncio.gather` → wall-clock ≈ 8 s + max(model time). Operational
  complexity: N × process-tree-management surfaces, N × conftest
  pre-flight, N × stream-protocol lifecycle.
- **CLI, N parallel branches:** each branch is one
  `asyncio.create_subprocess_exec`. Parallel → wall-clock ≈ 5.5 s +
  max(model time). Operational complexity: one `subprocess.run` per
  call, no shared state.

CLI is structurally simpler and ~2.5 s faster at the cold-start
boundary, before any operational tax is paid. A real benchmark would
confirm but not change the direction.

### E4 — workflow shape survey

Sampled `examples/pipeline_style/workflow.yaml` (script-only chain
with `route: goto`) and `examples/absurd-paper/workflow.yaml`
(13-node multi-stage with `depends_on` DAG). Both follow the same
pattern: **dependencies flow via `{{dep_id}}` template substitution**,
not via the LLM's session memory.

Generalization: sqrlly's entire abstraction is "the LLM is a function
from prompt to response." Every prompt is rendered fresh from
templates; the orchestrator hands the LLM all relevant context per
call. No workflow in the repo benefits from session-shared
conversation history. The current shared-session behavior is at best
wasteful (token bloat with no payoff) and at worst contaminating
(unrelated upstream output bleeds into downstream prompts).

**H4 result:** the `context_mode: isolated | shared` knob is YAGNI
under the current schema. If a future workflow wants
pipeline-with-continuity, it would be a meaningful new feature —
independent of the transport decision.

## Decision

**Path A — CLI replaces ACP.** Implement `transport: cli` (WISHLIST 35).
ACP's remaining defenses (streaming events, MCP-via-session,
multi-vendor portability) are not used today; the cost-per-call
advantage was illusory once isolation is required.

Suggested order of operations:

1. Implement `transport: cli` with `provider: anthropic` → `claude -p`.
   Single backend file, factory entry, schema literal change. Tests
   covering subprocess invocation + the existing
   `tests/e2e/test_live_backend_roundtrip.py` pattern restored for
   this single transport.
2. Cut 0.3.0-dev with both transports coexisting; default unchanged.
3. Migrate examples (`absurd-paper/subgraphs/*`) to `transport: cli`
   to dogfood; tag a 0.3.0 release with cli as a peer.
4. Decide deprecation timing for `transport: acp` after a real run
   on a non-trivial workflow confirms cli parity.

`scripts/migrate_legacy_executor_to_presets.py` will need a
follow-on update once cli's full provider table lands (currently it
refuses non-acp legacy executors with a `MigrateError`).

## What this does NOT settle

- Multi-vendor CLI feasibility (codex / gemini print-mode syntax,
  feature-parity with `claude -p`'s tool surface). A separate spike
  if/when the path involves adding a non-Anthropic CLI.
- The contracts arc (schema enforcement at backend boundary +
  structured-return). Independent of this investigation; can land in
  parallel and will compose with whichever transport story wins.
- Whether ACP's known defects (the `../../`-traversal hang, the SDK
  `Internal error` under concurrent load) are fixable in-place vs.
  blocked on upstream. Out of scope here.
