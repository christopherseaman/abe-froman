# Decisions

## 2026-04-14 — Three-layer refactor of engine/builder.py

- **Three layer directories**: `schema/` (DSL), `compile/` (the only place langgraph is imported), `runtime/` (executors, gates, contracts, state). Workflow-level orchestration (runner, persistence, resume, logging) lives in a fourth directory `workflow/`, above the node level.
- **Package naming**: picked `compile/` (matches WISHLIST's "compilation layer" vocabulary) over `builder/`. Picked `runtime/` over `engine/` to avoid overloading the old `engine/` name. Picked `workflow/` for runner-level orchestration above a single phase.
- **Defer state-shape cleanup** (`phases: dict[str, PhaseRunData]`). Out of scope — would modify every node function and every test assertion against state keys, breaking the "no test file modifications except import paths" constraint.
- **Unified `ExecutionResult`** replaces `PhaseResult` + `PromptBackendResult`. Backends return `ExecutionResult(success=True, ...)` or raise `OverloadError`. Executors own retry/downgrade policy; backends own transport.
- **`build_phase_subgraph` returns a `PhaseSubgraph` dataclass** `(node_fn, router, needs_conditional)`, NOT a compiled subgraph. True compiled subgraphs are blocked on open questions from WISHLIST line 9 (state projection, reducer composition, cross-boundary template resolution). This refactor gets us the structural split today, leaves a single call site to change for the future migration.
- **Architecture tests as AST-walkers**, not `import-linter` or `pydeps`. Zero extra dependencies. ~60 lines of test code, runs in under 100 ms.
- **ACP tests REQUIRE `@zed-industries/claude-code-acp`**, do not skip when absent. Pre-flight check in `tests/conftest.py` uses `pytest_collection_modifyitems` to exit with install instructions. No `@pytest.mark.skipif` fallback — aligns with the "no workarounds to make tests pass" rule.
- **Private helpers keep underscore names** after moving to `compile/*`. Matches WISHLIST "Stability contract is the YAML schema, not the Python API underneath." Tests update their import paths but the private-API disclaimer is preserved.
- **Move, don't rewrite, test files.** Existing 296 tests keep passing with only import-path updates (migration step 10). New tests (architecture, node helpers) are additive.
- **Shim-based migration.** Each step leaves a re-export shim in the old location until step 10 deletes them all. Each step is independently revertable via `git reset --hard`.
- **Do NOT split `runtime/executor/` into a top-level `executor/` package.** Keeping it under `runtime/` avoids a fifth top-level directory — executors are the prototypical runtime peer and have no business being peers to `runtime/gates.py` at the top level.
- **Python 3.14 pydantic v1 warning**: pre-existing, not refactor-induced. Ignore for this PR.
- **Baseline at branch creation**: 296 tests passing on `refactor` branch (verified 2026-04-14 before any source changes).
