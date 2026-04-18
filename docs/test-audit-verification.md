# Test Quality Audit — Verification Pass

## Context

Independent re-audit of `docs/test-audit.md`. Twenty Explore sub-agents ran in parallel, one per test file (+ one for `conftest`/`helpers`/`mock_executor`). Each agent full-read its test file, full-read the implicated `src/` module(s), and verified the M/J/C items the original audit assigned to it. No grep-only passes. No implementation changes made during the audit itself.

**Question answered**: *Did the M/J/C remediation land, and does the suite genuinely satisfy the "meaningful coverage, no fakes/fallbacks/mocks" goal?*

**Headline**: The remediation largely landed (**22 of 27 items RESOLVED**). Five items are PARTIAL and two new findings deserve attention — most notably **an undiscovered fake-backend policy violation in `test_prompt.py`** that was not in the original audit's scope.

## Aggregate M/J/C status

| Status | Count | IDs |
|---|---|---|
| RESOLVED | 22 | M1–M11, J1, J3, J4, J8, J9, J10, J11, J12, C1, C3, C4 |
| PARTIAL | 4 | C2, J2, J5, J7 |
| ROLLED-BACK / AUDIT-WAS-WRONG | 1 | J6 |

## Per-file counts (prior → now)

The prior audit's "~314 tests" line undercounted: current suite reads 335+ test functions. Agent counts (non-GOOD only shown):

| File | Prior GOOD/WEAK/SUSPECT/DEAD/Mp | Now |
|---|---|---|
| tests/unit/schema/test_schema.py | 37/0/0/0/0 | 37/0/0/0/0 |
| tests/unit/compile/test_manifest.py | 10/0/0/0/0 | 10/0/0/0/0 |
| tests/unit/compile/test_node_helpers.py | 50/0/0/0/0 | 50/**2**/0/**2**/0 (new findings below) |
| tests/unit/compile/test_phase_node.py | (n/a — file named for but doesn't test `_make_phase_node`) | 6/0/0/0/0 + **label error** |
| tests/unit/runtime/test_state.py | 5/0/0/0/0 | 4/**1**/0/0/0 (new finding) |
| tests/unit/runtime/test_contracts.py | 5/0/0/0/0 | 5/0/0/0/0 |
| tests/unit/runtime/test_command_executor.py | 5/0/0/0/0 | 5/0/0/0/0 |
| tests/unit/runtime/test_prompt.py | 21/0/0/0/0 | 21/0/**7+**/0/0 **policy violation** |
| tests/unit/runtime/test_gates.py | 51/0/0/0/0 (post M1–M4) | 49/0/**1**/**1**/0 |
| tests/unit/runtime/test_foreman.py | 15/0/0/0/0 | 15/0/0/0/0 |
| tests/unit/runtime/test_scaffolding.py | 7/0/0/0/0 | 7/0/0/0/0 |
| tests/unit/cli/test_cli.py | 20/1/0/0/0 | 20/**1**/0/0/0 (residual WEAK by design — J8 is satisfied by positive-path sibling) |
| tests/unit/workflow/test_logging.py | 15/0/0/0/0 | 15/0/0/0/0 |
| tests/builder/test_graph_shape.py | 14/0/0/0/0 | 14/0/0/0/0 |
| tests/architecture/test_layers.py | 7/0/0/0/0 | 7/0/0/0/0 |
| tests/acp/test_acp_backend.py | 7/0/0/1/0 | 7/0/0/1/0 |
| tests/e2e/test_dynamic.py | 19/0/0/0/0 | 16/**2**/**1**/0/0 (new findings) |
| tests/e2e/test_orchestrator.py | 48/1/0/0/0 | 48/**2**/0/0/0 (new finding) |
| tests/e2e/test_timeout.py | 9/0/0/0/0 | 7/**2**/0/0/0 (1 residual, 1 new) |
| Infra (conftest/helpers/mock_executor) | — | No mocks detected; MockExecutor is custom Protocol double |

## M/J/C verification matrix

| ID | Severity | Status | Evidence |
|---|---|---|---|
| M1 | 🟠 | RESOLVED | `conftest.py:47–57` — node pre-req check at collection time; skip in `test_gates.py` removed |
| M2 | 🟠 | RESOLVED | `test_gates.py` — `monkeypatch.setattr(asyncio, "sleep", …)` removed; real-sleep test at `L389–440` |
| M3 | 🟡 | RESOLVED | `TestGateThresholdComparison` class (stdlib float-compare) removed |
| M4 | 🟡 | RESOLVED | `test_py_validator_zero_score` / `_perfect_score` removed/consolidated |
| M5 | 🔴 | RESOLVED | `test_graph_shape.py:60–61` — explicit edge assertions including `(START, "g1")` + conditional routing |
| M6 | 🟠 | RESOLVED | All 7 topology tests call `_edges(graph)` and assert explicit edge-set tuples (`test_graph_shape.py:68–194`) |
| M7 | 🟡 | RESOLVED | `test_logging.py:131` — `events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]; assert events == []` |
| M8 | 🟠 | RESOLVED | `test_acp_backend.py:142` — `_assert_non_refusal_contains(r1.output, r"\bone\b")` |
| M9 | 🟠 | RESOLVED | `test_acp_backend.py:120` — `_assert_non_refusal_contains(result.output, r"\bpong\b")` |
| M10 | 🟠 | RESOLVED | `test_acp_backend.py:182` — `_assert_non_refusal_contains(result.output, r"\babe\s+froman\b")` |
| M11 | 🔴 | RESOLVED | `conftest.py:27–45` — `pytest_collection_modifyitems` exits with install instructions if ACP tests collected without `@zed-industries/claude-code-acp` on path |
| J1 | 🟠 | RESOLVED | `test_orchestrator.py:33–43` — `assert mock.execution_order == []` proves dry-run did not invoke executor |
| J2 | 🟡 | **PARTIAL** | `test_three_phase_chain` asserts output values but does not assert downstream context; context-flow covered separately in `test_dependency_output_in_executor_context` (`L487`). Audit's specific wording "assert context dict seen by C" was not implemented — though J4 satisfies the concern elsewhere. |
| J3 | 🟡 | RESOLVED | `test_diamond_all_complete` + `test_independent_phases_both_complete` now assert `phase_outputs["a"] == "root"` etc. |
| J4 | 🟡 | RESOLVED | `test_orchestrator.py:487–524` — `mock.received_contexts["b"]["a"] == "a-output"` + structured-output flow test |
| J5 | 🟠 | **PARTIAL** | `test_custom_fields_reach_subphase_context` (test_dynamic.py:364) uses `MockExecutor` and asserts against the mock's spy dict; never verifies the template placeholder was actually interpolated in a DispatchExecutor-driven output. |
| J6 | 🟠 | AUDIT-WAS-WRONG | `test_timeout.py:19–226` — all 9 tests are integration tests; no pure schema tests at `L21–50` as audit claimed. `Phase.effective_timeout` IS tested at `test_schema.py:400–413`. Either the MISPLACED tests were moved before this audit, or the audit's L21–50 reference was based on an earlier file state. |
| J7 | 🟠 | **PARTIAL** | `test_timeout_on_slow_executor` (test_timeout.py:133) has elapsed-time bound at `L152`. But `test_subphase_inherits_parent_timeout` (test_timeout.py:195–225) still asserts only message substring — no bound. |
| J8 | 🟡 | RESOLVED | `test_cli.py:209` — `TestTokenSummaryPositive.test_token_summary_displayed_when_tokens_present` asserts "Tokens:", "100", "50" in output via tokens-returning backend. Legacy L124 test remains as negative-path regression guard. |
| J9 | 🟡 | RESOLVED | `test_cli.py:250–284` — `TestCliHelpers` unit-tests all three: `_is_git_repo`, `_thread_id_for` (determinism + workdir sensitivity), `_db_path`. |
| J10 | 🟡 | RESOLVED | `test_schema.py:400–413` — three tests: override wins, fallback to settings, both None. |
| J11 | 🟡 | RESOLVED | `test_layers.py:89` — `test_no_langgraph_identifiers_via_ast` uses `ast.walk()` + `ast.Name`/`ast.Attribute` inspection; catches aliased imports. |
| J12 | 🟡 | RESOLVED | `test_acp_backend.py:148–158` — `test_close_is_idempotent` replaces private-state reads with behavioral check. |
| C1 | 🔴 | RESOLVED | `acp.py:96–98` — `async with self._init_lock:` wraps entire init block; `_init_lock = asyncio.Lock()` on `L91`. |
| C2 | 🟠 | **PARTIAL** | `acp.py:113` calls `self._callbacks.reset()` at the start of every `send_prompt`, which covers the sequential-caller leak scenario. But the explicit `try: … finally: acc = None` the audit recommended is not present. Risk is mitigated, not eliminated — concurrent callers on a shared backend still race on accumulator state. |
| C3 | 🟠 | RESOLVED | `acp.py:140` — `logger.warning("ACP process cleanup failed", exc_info=True)` plus fallback `proc.terminate()` at `L142–146`. |
| C4 | 🟠 | RESOLVED | `acp.py:108–123` — `timeout: float \| None` parameter threaded; `asyncio.wait_for(coro, timeout=timeout)` at `L121`. |

## New findings (not in prior audit)

### 🔴 NF-1 — Fake PromptBackend doubles violate memory policy — `test_prompt.py:91–130,431–448`

`test_prompt.py` defines three custom `PromptBackend` implementations (`MemoryBackend`, `ErrorBackend`, `_OverloadBackend`) used across ~11 test methods. The memory file `feedback_no_fake_backends.md` explicitly states: *"Do not introduce FakeBackend / StubBackend subclasses, unittest.mock, or monkeypatch spies in abe-froman tests … A PromptBackend represents Claude — it is an external system."* These three classes are exactly what the policy forbids.

**Complication**: the model-downgrade branch (`prompt.py:74–91`) is the hardest thing in the codebase to test without a backend double — the real `StubBackend` never raises `OverloadError`. The policy suggests: "For LLM gate behaviors, test pure parser functions directly with string inputs." Downgrade logic could similarly be extracted and tested without a backend, but that's a src refactor, not a test fix.

**Severity**: 🔴 (policy violation; 18+ tests affected)
**Disposition**: Judgment-call — **defer to follow-up plan** for user decision.

### 🟠 NF-2 — `test_token_usage_merges_across_phases` doesn't verify merge semantics — `test_state.py:33–40`

Asserts keys are present but doesn't verify shallow-copy independence: modifying `merged["p1"]` after merge would silently corrupt `left["p1"]` under the current shallow merge, and the test wouldn't catch it. Severity: 🟡. **Trivial-fix candidate**.

### 🟠 NF-3 — `test_phase_node.py` tests the wrong thing — `test_phase_node.py:1–3` + full file

Filename and docstring say "phase_node"; actual tests exercise `_make_gate_router` (from `compile/graph.py`), not `_make_phase_node`. The 15 pure helpers extracted from the node body are tested in `test_node_helpers.py`; the closure body (early-exit sequencing, timeout wrap, contract validation, worktree hand-off) has **zero unit coverage** — only E2E. Severity: 🟡 docstring, 🟠 coverage gap.
**Trivial-fix candidate** for docstring. **Judgment call** for the coverage gap.

### 🟠 NF-4 — Two DEAD tests in `test_node_helpers.py:275–283`

`test_none_tokens_excluded` and `test_none_structured_excluded` in `TestAssembleSuccessUpdate` assert absence of a dict key — stdlib behavior, not src logic. Severity: 🟡. **Judgment call** (deletion requires confirming no latent value).

### 🟠 NF-5 — WEAK parametrize rows in `test_node_helpers.py:295,408`

`TestClassifyGateOutcome` parametrize row 8 tests `0.79 < 0.8` (stdlib float compare); `TestDimensionGateClassification.test_dimension_gate_ignore_threshold` asserts dimensions-override-threshold implicitly via test name. Severity: 🟡 (signal clarity, not correctness).

### 🟠 NF-6 — `test_two_phases_execute_in_order` doesn't assert outputs — `test_orchestrator.py:53`

Sibling `test_three_phase_chain` does assert outputs; this one doesn't. Trivial divergence. Severity: 🟡. **Trivial-fix candidate**.

### 🟠 NF-7 — `test_basic_fan_out` doesn't verify template interpolation — `test_dynamic.py:53–66`

Creates template `{{id}}` but only asserts subphase completion, not that the template rendered with `id` substituted. Would pass if templating broke but phases still executed. Severity: 🟡. **Judgment call** (stronger assertion needs DispatchExecutor swap).

### 🟠 NF-8 — C2 accumulator guard — `acp.py:115–127`

Independent of the PARTIAL C2 classification above, the agent flagged this explicitly: the mitigation (`reset()` at L113) is not the guard the audit recommended. Under concurrent callers on a shared `ACPBackend` instance, accumulator state can still race. Severity: 🟠. **Judgment call / src change — follow-up plan**.

### 🟠 NF-9 — Persistent MISSING unit coverage

All five D-series items from the original audit plus a few additions remain UNCOVERED at unit level (integration only):

| Target | Has unit test? |
|---|---|
| `compile/nodes.py:apply_backoff` | ✅ (L27–50 of test_node_helpers.py) |
| `compile/nodes.py:evaluate_gate_and_outcome` | ❌ |
| `compile/nodes.py:_make_phase_node` (body) | ❌ |
| `runtime/foreman.py:86–97` (git worktree add failure) | ❌ |
| `runtime/executor/backends/acp.py:_is_overload_error` | ❌ |
| `runtime/executor/backends/acp.py:add_usage` | ❌ (integration only) |
| `runtime/gates.py:evaluate_gate_llm` (positive path) | ❌ (E2E only via TestJokeWorkflowIntegration) |

Severity: 🟡. **Judgment call** — the audit already flagged these as "not blocking, surfaced during audit." Status unchanged.

## Trivial-fix queue (applied inline this session)

| # | File:line | Change | Size |
|---|---|---|---|
| TF-1 | `tests/unit/compile/test_phase_node.py:1–3` | Correct docstring to "gate_router" | 1 LOC |
| TF-2 | `tests/unit/runtime/test_state.py:33–40` | Add merge-isolation assertion: mutate `merged["p1"]`, assert `left["p1"]` unchanged | ≤5 LOC |
| TF-3 | `tests/e2e/test_orchestrator.py:53–62` | Add `assert result["phase_outputs"]["a"] == "a-out"` and `["b"] == "b-out"` | ≤2 LOC |
| TF-4 | `tests/e2e/test_timeout.py:195–225` | Add `t0 = time.monotonic()` + `assert elapsed < 1.5` for subphase timeout | ≤5 LOC |

## Judgment-call queue (deferred to follow-up plan)

| # | Finding | Why deferred |
|---|---|---|
| FU-1 | **NF-1**: `test_prompt.py` PromptBackend doubles — 18+ tests rely on `MemoryBackend`/`ErrorBackend`/`_OverloadBackend` | Architectural decision: refactor to pure-helper tests, move to real-ACP integration, or relax the policy. Not a 5-LOC change. |
| FU-2 | **NF-8 / C2 src-side**: explicit try/finally around accumulator in `acp.py:send_prompt` | Source change, not test change. Plan owner should verify impact on concurrent-call semantics. |
| FU-3 | **NF-3 coverage**: no unit test for `_make_phase_node` closure body | Requires designing what "unit-level" looks like for the closure (direct call with fake state dict vs. assembled graph with minimal config). |
| FU-4 | **NF-4**: Delete 2 DEAD tests (`test_none_tokens_excluded`, `test_none_structured_excluded`) | Low-risk but requires sign-off that no latent value exists. |
| FU-5 | **NF-7**: Strengthen `test_basic_fan_out` to verify template interpolation | Changing MockExecutor → DispatchExecutor is a test-architecture change (per plan's trivial-fix definition). |
| FU-6 | **NF-9 MISSING coverage**: `_is_overload_error`, `add_usage`, `evaluate_gate_llm` positive path, `evaluate_gate_and_outcome`, `git worktree add` failure | Each is a new test requiring design decisions (what to assert, how to stage). |
| FU-7 | **J2 residual**: add context-flow assertion to `test_three_phase_chain` | The concern is already covered by `test_dependency_output_in_executor_context`; decide whether to leave or consolidate. |

## Follow-up decisions (post-walkthrough)

Each FU item was reviewed by the owner. Annotated here for the historical record:

- **FU-4 (Delete 2 DEAD tests)** — **Leave both in place.** Re-examination showed `test_none_tokens_excluded` / `test_none_structured_excluded` pin a real invariant: `assemble_success_update` must omit (not write `None` to) keys so LangGraph's `_merge_dicts` reducer doesn't overwrite prior entries with `None`. Deleting these tests would expose that invariant to silent regression. Keeping them is deliberate.
- **FU-7 (J2 residual)** — **Do nothing.** Context-flow coverage already exists at `test_orchestrator.py:487` (`test_dependency_output_in_executor_context`). Adding the assertion to `test_three_phase_chain` would duplicate coverage without improving signal. Accepted residual.

## Verdict on the original question

> *"Did we actually tackle the last goal of validating that test coverage is meaningful, complete, and doesn't rely on fallbacks, mocks, or fake responses/functionality?"*

**Partially.** The M/J/C remediation landed cleanly for the items the prior audit identified, and the suite demonstrably uses real subprocess / real ACP / real git worktrees / real filesystem. **But** the independent re-audit surfaced a substantial finding the original pass missed: `test_prompt.py` contains three custom `PromptBackend` implementations that are exactly what the `feedback_no_fake_backends.md` memory policy forbids. This is not "fallbacks to make tests pass" in the cosmetic sense — the tests do exercise real logic — but it is a fake of the external-system boundary, which the project rule names as the exact thing to avoid.

The suite is **meaningful and largely complete** (no grep-only-pass regressions, no pytest.skip hiding deps, no `unittest.mock`). It is **not yet free** of fake external-system backends. Deciding what to do about that is FU-1.
