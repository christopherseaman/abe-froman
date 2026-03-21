# Feature Backlog: Adapter Inspiration

Features observed in `../adapter` that abe_froman could adopt. Excludes anything tied to claude-flow internals — only portable concepts.

---

## Tier 1 — High value, natural fit

### 1. Output contract validation (pre-gate)
**What:** Before running a quality gate, verify that required output files actually exist and have stabilized (no writes in N seconds).
**Why:** Gates that evaluate missing/partial files give meaningless scores. Separating "did it produce files?" from "are the files good?" makes failures more debuggable.
**Adapter impl:** Polls filesystem with configurable timeout and stability threshold.
**Abe Froman fit:** `output_contract` already exists in schema but isn't enforced at runtime. Wire it into `_make_phase_node` as a pre-gate check.

### 2. JS gate validators
**What:** Support `.js` validators alongside `.py` validators, running via `node`.
**Why:** Some validation logic (JSON schema checks, structural validation) is more natural in JS, and teams may prefer it.
**Abe Froman status:** `evaluate_gate` already handles `.js` — tested in `test_gates.py::TestGateJSValidator`. This is **already implemented**.

### 3. Model routing / forced downgrade on overload
**What:** Detect API overload (529 errors) and automatically downgrade model tier (opus → sonnet → haiku) for the retry.
**Why:** API overload during a 30-phase workflow shouldn't require manual intervention.
**Abe Froman fit:** Add overload detection in `PromptBackend.send_prompt()`. Model downgrade could be a configurable retry strategy in settings.

---

## Tier 2 — Valuable, moderate effort

### 4. Output directory scaffolding
**What:** Before execution, create the expected directory structure with placeholder files showing where outputs should go.
**Why:** Phases that write files need the directories to exist. Scaffolding also serves as documentation of expected outputs.
**Abe Froman fit:** Pre-execution hook that reads `output_contract.base_directory` and `required_files`, creates directories and `.EXAMPLE` placeholders. Clean up placeholders after successful completion.

### 5. Phase execution timeout
**What:** Configurable per-phase timeout. Kill phase if it exceeds the limit.
**Why:** Runaway prompts or hung subprocesses shouldn't block the entire workflow indefinitely.
**Abe Froman fit:** Add `timeout` field to Phase schema. Apply via `asyncio.wait_for()` in `_make_phase_node`.

### 6. Structured phase status logging (JSONL)
**What:** Emit structured events (phase start, phase end, gate result, retry, error) as JSONL to a log file.
**Why:** Machine-parseable execution history enables dashboards, cost analysis, and post-mortem debugging.
**Abe Froman fit:** Add an optional `--log` flag to CLI. Emit events from `_make_phase_node` and gate evaluation.

---

## Tier 3 — Nice to have, lower priority

### 7. Stepped retry backoff
**What:** Configurable delay between retries: e.g., 10s, 20s, 60s, 180s.
**Why:** Immediate retries during API rate limits just burn tokens. Exponential backoff gives the API time to recover.
**Abe Froman fit:** Add `retry_backoff` to settings (list of delay values or exponential config). Apply in the retry loop within `_make_phase_node`.

### 8. Token usage tracking
**What:** Track input/output/cache tokens per phase and per model. Aggregate totals at workflow completion.
**Why:** Cost visibility. Long workflows can burn $50+ in tokens — teams need to know where the spend goes.
**Abe Froman fit:** Lighter approach — add token counts to `PhaseResult`, accumulate in `WorkflowState`, print summary at end. Backend-specific: ACP backend can extract token counts from response metadata.

### 9. Execution mode fallback chain
**What:** If the primary execution mode fails (e.g., ACP backend timeout), fall back to a secondary mode (e.g., direct API call).
**Adapter impl:** Falls back from hive-mind → direct Claude CLI after 3 retries.
**Abe Froman fit:** `PromptBackend` could accept a fallback backend. Or configure per-phase: `execution.fallback: {type: command, ...}`.

### 10. Post-workflow cleanup

**What:** After workflow completion, remove intermediate artifacts (scaffolding files, temp files, validation byproducts) while preserving final deliverables.
**Abe Froman fit:** Add optional `cleanup` section to workflow config listing glob patterns to remove on success.

### 11. Environment variable injection into validators

**What:** Pass workflow context as env vars to gate validator scripts (phase ID, workflow name, attempt number).
**Abe Froman status:** `PHASE_ID` is already injected — tested in `test_gates.py::TestGateEnvironment`. Could extend with `WORKFLOW_NAME`, `ATTEMPT_NUMBER`, `WORKDIR`.

### 12. Preamble / shared context injection

**What:** A shared markdown preamble prepended to all phase prompts, containing project-wide context.
**Adapter impl:** `backpack/preamble.md` automatically injected.
**Abe Froman fit:** Add `settings.preamble_file` — `PromptExecutor` prepends its contents before template rendering.

### 13. Git integration for outputs

**What:** Auto-commit and push workflow outputs to a branch on completion.
**Why:** Useful for CI/CD pipelines where the workflow runs in automation and results need to land in a repo.
**Abe Froman fit:** Post-workflow hook or `settings.git_push` config. Low priority — easy to script externally.

### 14. Health check / liveness endpoint

**What:** HTTP endpoint reporting workflow status, current phase, duration.
**Why:** Required for container orchestration (Kubernetes, Railway) to know the process is alive.
**Abe Froman fit:** Optional `--serve-health` flag that starts a minimal HTTP server alongside execution.

---

## Already implemented in abe_froman

These adapter features are already present — no action needed:

| Feature | Status |
|---------|--------|
| Resume from failed phase | Implemented (`--resume`, `--start=<phase-id>`) |
| Enhanced retry with failure context | Implemented (`{{_retry_reason}}` injection) |
| JS gate validators | Implemented, tested |
| PHASE_ID env var in gates | Implemented, tested |
| Dynamic subphase fan-out from manifest | Implemented (Phase 6) |
| Blocking vs non-blocking gates | Implemented, tested |
| Gate threshold scoring (0.0–1.0) | Implemented, tested |
| Output contract schema definition | Schema exists, not enforced at runtime |
| Per-phase model override | Implemented, tested |
| Prompt template variable substitution | Implemented, tested |

---

## Recommended implementation order

1. **Output contract enforcement** — schema already exists, just needs runtime wiring
2. **Model downgrade on overload** — resilience for long-running workflows
3. **Phase execution timeout** — safety net for production workflows
4. **Structured JSONL logging** — observability foundation for everything else
