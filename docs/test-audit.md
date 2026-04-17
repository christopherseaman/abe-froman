# Test Quality Audit

## Scope

18 test files / ~335 tests + `runtime/executor/backends/acp.py`. Deep read of every test function against the src it exercises. No grep-only passes.

## Classification legend

- **GOOD** — concrete assertion that would fail on regression; covers the implicated src branch
- **WEAK** — assertion too loose (`is not None`, `len > 0`, stopword substring); passes on malformed-but-truthy output
- **SUSPECT** — mocks external system, `pytest.skip` hiding deps, tautological, asserts behavior src doesn't provide, couples to private state
- **DEAD** — no meaningful assertion, or tests stdlib rather than our code
- **MISPLACED** — test belongs in a different file (e.g. schema test in E2E file)
- **MISSING** — src branch/function has no test

## Aggregate counts

| Batch | Files | Tests | GOOD | WEAK | SUSPECT | DEAD | MISPLACED |
|---|---|---|---|---|---|---|---|
| A. runtime/ | 7 | ~106 | 99 | 0 | 4 | 3 | — |
| B. compile/+schema/+cli/+workflow/ | 6 | ~111 | 104 | 6 | 1 | — | — |
| C. e2e/ | 3 | ~67 | 52 | 6 | 2 | — | 7 |
| D. acp/+builder/+architecture/ | 3+conftest | ~30 | 21 | 5 | 3 | 1 | — |
| **Total** | **19** | **~314** | **276** | **17** | **10** | **4** | **7** |

## Findings

### A. Mechanical fixes (no judgment needed)

Severity legend: 🔴 block shipping, 🟠 fix soon, 🟡 nice-to-have.

| ID | Severity | File:line | Category | Finding | Src reference | Fix |
|---|---|---|---|---|---|---|
| M1 | 🟠 | `tests/unit/runtime/test_gates.py:295` | SUSPECT | `pytest.skip("node not available")` masks missing dep; node is a first-class validator type | `runtime/gates.py:86-87` | Remove skip; add node pre-req check to `tests/conftest.py` mirroring ACP pattern. See also **C2** (ACP check missing from conftest) |
| M2 | 🟠 | `tests/unit/runtime/test_gates.py:496,551,604` | SUSPECT (3 tests) | `monkeypatch.setattr(asyncio, "sleep", ...)` mocks stdlib | `compile/nodes.py:27-36,116-123` | `_get_retry_delay` is already pure and tested in `test_node_helpers.py:37-50`. Replace the 3 mock-based tests with (a) real-sleep integration test using a small backoff (e.g. `[0.05, 0.1]`) and elapsed-time bound, OR (b) delete if `test_node_helpers.py` coverage is sufficient |
| M3 | 🟡 | `tests/unit/runtime/test_gates.py:150-160` | DEAD (3 tests — TestGateThresholdComparison) | Tests stdlib float comparison (`0.9 >= 0.8`); threshold comparison lives in `compile/nodes.py:163`, not gates.py | n/a | Delete the class |
| M4 | 🟡 | `tests/unit/runtime/test_gates.py:133,141` | DEAD (2 tests) | `test_py_validator_zero_score` / `test_py_validator_perfect_score` — pure float equality against literals | `runtime/gates.py:30-31` | Consolidate into the existing `test_py_validator_returns_float_score` parametrize or delete |
| M5 | 🔴 | `tests/builder/test_graph_shape.py:104` | DEAD | `assert graph is not None` is the only assertion | `compile/graph.py:245-249` (terminal gate wiring) | Replace with explicit edge check: `assert (phase_id, END) in edges` and conditional gate routing |
| M6 | 🟠 | `tests/builder/test_graph_shape.py:50-88,106-148` | WEAK (7 tests — linear/diamond/gate tests) | Assert nodes but never call `graph.get_graph().edges` to verify edge topology | `compile/graph.py:180-271` | For each topology, add explicit edge-set assertion. Diamond: `{(a,b),(a,c),(b,d),(c,d)}`. Non-terminal gate: conditional edges `{pass,retry,fail}`. Multi-dependent gate: passthrough wiring `(a→_after_a)` + `(_after_a→b)` + `(_after_a→c)` |
| M7 | 🟡 | `tests/unit/workflow/test_logging.py:125` | SUSPECT | `assert buf.getvalue() == ""` is whitespace-brittle | `runtime/logging.py:32-67` | Replace with `events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]; assert events == []` |
| M8 | 🟠 | `tests/acp/test_acp_backend.py:100-101` | WEAK | `assert len(r1.output) > 0` / `len(r2.output) > 0` — any single character passes | `runtime/executor/backends/acp.py:116-137` | Use deterministic prompts ("respond with only the number 42") and assert regex pattern match; OR keep semantic prompts but tighten to reject obvious refusal markers |
| M9 | 🟠 | `tests/acp/test_acp_backend.py:76` | WEAK | `"pong" in result.output.lower()` passes on "pong is not a valid keyword" | same | Reject-pattern regex (`\bpong\b` + no `"sorry"`/`"can't"`/`"unable"` substrings) OR deterministic prompt |
| M10 | 🟠 | `tests/acp/test_acp_backend.py:141` | WEAK | `"abe froman" in result.output.lower()` — same class as M9 | same | Same |
| M11 | 🔴 | `tests/conftest.py` | MISSING | CLAUDE.md L290 claims an ACP pre-req check exists in conftest.py. **It does not.** Tests fail cryptically at import time instead of exiting cleanly at collection | n/a | Add collection-time check: if `@zed-industries/claude-code-acp` (npx) not resolvable AND any `tests/acp/*` collected, `pytest.exit()` with install instructions. Same pattern for **node** (covers M1) |

### B. Judgment-call fixes (ask before applying)

| ID | Severity | File:line | Category | Finding | Src reference | Decision needed |
|---|---|---|---|---|---|---|
| J1 | 🟠 | `tests/e2e/test_orchestrator.py:33` | SUSPECT | `test_dry_run_skips_execution` asserts `"dry-run" in result["phase_outputs"]["p1"]`; would pass if executor DID run and echoed "dry-run" | `compile/nodes.py:55-64,267-274` | Refactor to use MockExecutor and assert `mock.execution_order == []` (proof executor was not invoked), OR also check a counter-file was NOT incremented |
| J2 | 🟡 | `tests/e2e/test_orchestrator.py:59` | WEAK | `test_three_phase_chain` only checks set membership of keys; no assertion that phase C received B's output | `compile/nodes.py:67-86` | Strengthen to assert `phase_outputs["a"] == "a-out"`, `"b"]==` etc. and that context dict seen by C contains `{"a": "a-out", "b": "b-out"}` |
| J3 | 🟡 | `tests/e2e/test_orchestrator.py:79,93` | WEAK | `test_diamond_all_complete` / `test_independent_phases_both_complete` — set-membership only | `compile/graph.py:186-195` | Add output-value assertions for each phase |
| J4 | 🟡 | `tests/e2e/test_orchestrator.py:471,490,518` | SUSPECT (3 tests) | Context-propagation tests use MockExecutor; would pass with a no-op executor since assertions are against the mock's own spy dict | `compile/nodes.py:67-86` | Either keep (purpose is context wiring, not executor) and rename to make intent explicit, OR add a separate E2E test that uses DispatchExecutor + command phases to prove real context flow |
| J5 | 🟠 | `tests/e2e/test_dynamic.py` | MISSING | No test verifies that manifest item fields **beyond `id`** are passed to template context. `{{id}}` flow via subphase_id is implicit but `{{custom_field}}` never tested | `compile/dynamic.py:67-71` | Add test: manifest item `{"id": "x", "custom_field": "value123"}` → subphase template uses `{{custom_field}}` → assert it expanded correctly |
| J6 | 🟠 | `tests/e2e/test_timeout.py:21-50` | MISPLACED (7 tests) | Pure Pydantic schema tests (`test_phase_timeout_field`, `test_effective_timeout_*`, etc.) in E2E file | `schema/models.py:108-112` | Move to `tests/unit/schema/test_schema.py`. Also add the currently-MISSING test for `Phase.effective_timeout` |
| J7 | 🟠 | `tests/e2e/test_timeout.py:182,222` | WEAK | Timeout tests assert `"timed out"` in error message but have no elapsed-time bound. Would pass if test failed for unrelated reason | `compile/nodes.py:126-138` | Add `t0 = time.monotonic()`, `...`, `assert time.monotonic() - t0 < timeout * 3` (bound above; below is implicitly covered by the message match) |
| J8 | 🟡 | `tests/unit/cli/test_cli.py:116` | WEAK | `TestTokenSummary.test_token_summary_displayed` only tests the negative path (stub backend → no "Tokens:") | `cli/main.py:214-221` | Add positive-path test with a test double backend that returns `tokens_used={"input":100,"output":50}`, assert output contains the summary |
| J9 | 🟡 | `tests/unit/cli/test_cli.py` | MISSING | `_is_git_repo` (main.py:20-30), `_thread_id_for` (main.py:41-44), `_db_path` (main.py:47-48) have no unit tests. `_thread_id_for` is load-bearing for checkpoint resume | `cli/main.py:20-48` | Decide: unit-test the three helpers, or accept integration-only coverage via the resume tests |
| J10 | 🟡 | `tests/unit/schema/test_schema.py` | MISSING | `Phase.effective_timeout` (models.py:108-112) has no test | `schema/models.py:108-112` | Add pair: phase override wins / falls back to settings / both None |
| J11 | 🟡 | `tests/architecture/test_layers.py:88` | WEAK | `test_no_langgraph_terminology` is text-search not AST-aware; `from langgraph.types import Send as S` would bypass | `tests/architecture/test_layers.py:80-93` | Convert to AST-based import inspection matching the other layer tests |
| J12 | 🟡 | `tests/acp/test_acp_backend.py:92,106-117` | SUSPECT | Tests read private state (`_session_id`, `_initialized`). Refactors that preserve behavior but rename state break tests | `runtime/executor/backends/acp.py:102-148` | Decide: observe behavior (make two calls, assert both succeed and point at same ACP process) vs. keep internal-state assertions for now |

### C. ACP backend fixes (Phase 5, src changes)

All in `src/abe_froman/runtime/executor/backends/acp.py`:

| ID | Severity | File:line | Finding | Fix |
|---|---|---|---|---|
| C1 | 🔴 | `acp.py:102-114` (`_ensure_initialized`) | Two concurrent `send_prompt` calls race past `if self._initialized:` check, spawn two processes, one becomes orphaned | Wrap the whole init block in `async with self._init_lock:` (asyncio.Lock instance on `__init__`) |
| C2 | 🟠 | `acp.py:121-135` (accumulator lifetime) | Accumulator assignment + reset is not exception-safe; exception between L122 and L135 leaks accumulator into next call | `try: self._client.accumulator = acc; result = await ...; finally: self._client.accumulator = None` |
| C3 | 🟠 | `acp.py:139-148` (`close()`) | `except Exception: pass` silently swallows failures; zombie process possible | Log the exception via stdlib `logging.warning` with traceback; keep state reset; consider `proc.terminate()` fallback |
| C4 | 🟠 | `acp.py:116-137` (`send_prompt`) | No explicit timeout. One stuck ACP process can hang the whole suite up to pytest's 300s default | `await asyncio.wait_for(self._conn.prompt(...), timeout=phase_timeout_or_default)`. Signature needs a timeout parameter threaded from PromptExecutor |

### D. Notable MISSING coverage (candidates for J-series)

(Not blocking, but surfaced during the audit.)

- `compile/nodes.py:116-123` (`apply_backoff`) — integration-only; no unit test
- `compile/nodes.py:218-250` (`evaluate_gate_and_outcome`) — no unit test
- `compile/nodes.py:253-330` (`_make_phase_node` body) — only partially integration-tested
- `runtime/foreman.py:86-97` — `git worktree add` failure path untested
- `runtime/executor/backends/acp.py:12-20` (`_is_overload_error`) — untested
- `runtime/executor/backends/acp.py:34-36` (`add_usage` token callback) — untested
- `runtime/gates.py:167-210` (`evaluate_gate_llm`) — only E2E-covered

## What the audit did NOT flag (confirming the suite's strengths)

- `test_gates.py:738,762,820,872` — prior grep pass thought these `assert result.feedback is not None` were weak. Full read shows each is **immediately followed** by `assert "<specific-term>" in result.feedback`. **These are GOOD**, not WEAK. Classic example of why grep misses context.
- `tests/unit/runtime/{test_foreman,test_scaffolding,test_contracts,test_command_executor,test_state}.py` — 100% GOOD.
- `tests/architecture/test_layers.py:33-77` — AST-walking layer tests are solid.
- `tests/unit/schema/test_schema.py` — 37 tests, all GOOD save the MISSING `effective_timeout` test.

## Verification approach (Phase 6)

1. `uv run pytest tests/ -v` green
2. `for i in {1..10}; do uv run pytest tests/acp -v; done` — record pass rate. Pre-fix baseline first, then post-fix. Target ≥95%
3. Concurrency stress for C1: run a workflow with `max_parallel_jobs: 4` and 4 simultaneous ACP phases; assert no orphaned processes (`pgrep -f claude-code-acp | wc -l`)
4. `tests/architecture/test_layers.py` remains green after any new imports
