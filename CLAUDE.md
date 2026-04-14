# Abe Froman

Workflow orchestrator using LangGraph for orchestration and Claude via ACP for execution.

## Architecture

```
YAML Config → Pydantic Schema → LangGraph StateGraph
                                       │
                                       ▼
                                 Phase Node → DispatchExecutor
                                       │           │
                                       │     ┌─────┼──────┐
                                       │     ▼     ▼      ▼
                                       │   ACP   Cmd   GateOnly
                                       ▼
                                 Quality Gate (conditional edge)
                                       │
                                 ┌─────┼─────┐
                                 ▼     ▼     ▼
                               pass  retry  fail
```

## Build & Test

```bash
uv sync                                    # install deps
uv run pytest tests/ -v                    # run all tests (~260 tests, ~60s)
uv run abe-froman validate config.yaml     # validate a workflow config
uv run abe-froman run config.yaml --dry-run # dry run
uv run abe-froman run config.yaml -e acp   # run with Claude via ACP
uv run abe-froman run config.yaml --resume        # resume from last failure
uv run abe-froman run config.yaml --start=phase-3 # restart from specific phase
uv run abe-froman run config.yaml --log out.jsonl  # run with JSONL event log
uv run abe-froman graph config.yaml        # print dependency graph
```

## Workflow Schema

```yaml
name: "Workflow Name"         # required
version: "1.0.0"             # required

phases:                       # required, list of phases
  - id: my-phase              # required, unique identifier
    name: "Human Name"        # required
    description: "..."        # optional
    model: "opus"             # optional, overrides settings.default_model
    depends_on: ["other-id"]  # optional, list of phase IDs this depends on
    timeout: 30.0             # optional, seconds, overrides settings.default_timeout

    # Execution — pick ONE approach:
    prompt_file: "path/to/prompt.md"   # shorthand for type: prompt
    # OR
    execution:
      type: prompt                     # Claude prompt execution
      prompt_file: "path/to/prompt.md"
    # OR
    execution:
      type: command                    # subprocess execution
      command: "echo"
      args: ["hello"]                  # optional, default []
    # OR
    execution:
      type: gate_only                  # no execution, just gate evaluation

    # Quality gate — optional
    quality_gate:
      validator: "gates/check.py"     # .py/.js = subprocess, .md = prompt (stub)
      threshold: 0.8                  # 0.0–1.0, gate passes when score >= threshold
      blocking: false                 # true = failure stops workflow, false = warn and continue
      max_retries: 3                  # optional, overrides settings.max_retries

    # Output — optional
    output_contract:
      base_directory: "output/dir"
      required_files: ["file1.md"]
    output_schema:                    # JSON schema hint for structured output
      type: object
      properties: { ... }

    # Dynamic subphases — optional
    dynamic_subphases:
      enabled: true
      manifest_path: "manifest.json"
      template:
        prompt_file: "template.md"
        quality_gate: { ... }
      final_phases: [{ id, name, prompt_file, ... }]

settings:                     # optional, all fields have defaults
  output_directory: "output"  # default: "output"
  max_retries: 3              # default: 3
  default_model: "sonnet"     # default: "sonnet"
  executor: "stub"            # default: "stub", options: "stub", "acp"
  default_timeout: 300        # optional, seconds, None = no timeout
  preamble_file: "preamble.md" # optional, prepended to all prompt phases
  retry_backoff: [10, 30, 60]  # optional, delay seconds per retry attempt
```

### Execution types

| Type | What it does | Use for |
|------|-------------|---------|
| `prompt` | Sends rendered prompt to Claude via PromptBackend | AI-generated content |
| `command` | Runs subprocess, captures stdout | Scripts, validators, data processing |
| `gate_only` | No execution, just quality gate | Validation checkpoints |

### Prompt templating

Prompt files support `{{variable}}` placeholders. Variables are populated from dependency outputs:

```markdown
Here is the research from the previous phase:

{{research-phase}}

Based on this, generate a summary.
```

The variable name matches the `id` of the dependency phase. Its value is the raw output from that phase.

On retry, the orchestrator auto-injects `{{_retry_reason}}` with the previous gate score, threshold, and attempt number. Templates can use this to give the model feedback on why the previous attempt failed.

Both `prompt_file` and `quality_gate.validator` paths resolve relative to the working directory (`--workdir`), not the config file location.

### Quality gate validators

**Script validators** (`.py`, `.js`) receive the phase output on stdin and must print a score to stdout.

Environment variables available to validators:
- `PHASE_ID` — the phase's unique identifier
- `WORKFLOW_NAME` — the workflow's `name` field from config
- `ATTEMPT_NUMBER` — current attempt number (starts at 1, increments on retry)
- `WORKDIR` — the working directory path

```python
# gates/validate.py — reads stdin, prints 0.0–1.0
import json, sys
data = json.loads(sys.stdin.read())
print("1.0" if valid(data) else "0.0")
```

Score can be a plain float or JSON `{"score": 0.75}`.

**Prompt validators** (`.md`) are stubbed to 1.0 for now.

### Gate routing

- `score >= threshold` → **pass** → continue to dependents
- `score < threshold` and retries left → **retry** → re-execute phase
- `score < threshold`, no retries, `blocking: true` → **fail** → dependents skipped
- `score < threshold`, no retries, `blocking: false` → **pass with warning** → continue

## Project Layout

- `src/abe_froman/schema/models.py` — Pydantic models (WorkflowConfig, Phase, execution types)
- `src/abe_froman/engine/state.py` — WorkflowState TypedDict with LangGraph reducers
- `src/abe_froman/engine/builder.py` — YAML → LangGraph StateGraph construction
- `src/abe_froman/engine/gates.py` — Quality gate evaluation + routing
- `src/abe_froman/engine/persistence.py` — State save/load/clear to `.abe-froman-state.json`
- `src/abe_froman/engine/resume.py` — Resume/start-from state preparation
- `src/abe_froman/engine/logging.py` — Structured JSONL event logger
- `src/abe_froman/engine/runner.py` — Streaming execution with state persistence and optional JSONL logging
- `src/abe_froman/executor/base.py` — PhaseExecutor Protocol + PhaseResult
- `src/abe_froman/executor/dispatch.py` — Routes execution by phase type
- `src/abe_froman/executor/command.py` — Subprocess executor
- `src/abe_froman/executor/prompt.py` — PromptExecutor (template rendering, model resolution)
- `src/abe_froman/executor/prompt_backend.py` — PromptBackend Protocol
- `src/abe_froman/executor/backends/stub.py` — Stub backend (default)
- `src/abe_froman/executor/backends/acp.py` — ACP backend (claude-code-acp)
- `src/abe_froman/executor/backends/factory.py` — Backend factory
- `src/abe_froman/cli/main.py` — Click CLI

## Key Design Decisions

- **PhaseExecutor Protocol** (not ABC) — duck-typed, agent-agnostic
- **Discriminated union** for execution types: PromptExecution | CommandExecution | GateOnlyExecution
- **Quality gates owned by orchestrator**, not executor — gates read phase output via stdin
- **LangGraph state with Annotated reducers** for safe parallel state merging
- **Model per phase** with `settings.default_model` fallback
- **`{{variable}}` templating** in prompt files for parameterized execution
- **PromptBackend Protocol** — swappable backends (stub, acp, future: API key, OpenAI, etc.)

## Known Limitations

- **Hyphenated phase IDs in templates:** `{{research-phase}}` won't be substituted — the `\w+` regex in `render_template` doesn't match hyphens. Use underscores in phase IDs that need template substitution.
- **Prompt validators stubbed:** `.md` gate validators always return 1.0.
- **Subphase quality gates:** Record scores but do not trigger retries. Retry routing only works for top-level phase gates.

## Backlog

Prioritized features for future development. See `docs/backlog-adapter-inspiration.md` for full details.

### P1 — Next up

1. ~~**Resume from failed phase**~~ — **DONE**. `--resume` and `--start=<phase-id>` flags. State persisted to `.abe-froman-state.json` after each phase via `astream`. Cleared on success, preserved on failure.
2. ~~**Enhanced retry with failure context**~~ — **DONE**. On retry, `{{_retry_reason}}` is auto-injected into context with previous gate score, threshold, and attempt number.
3. ~~**Output contract enforcement**~~ — **DONE**. After execution, before gate evaluation, `validate_output_contract()` checks required files exist. Contract failure is always blocking (hard fail, no retry).
4. ~~**Model downgrade on API overload**~~ — **DONE**. PromptExecutor catches OverloadError and auto-downgrades model tier (opus → sonnet → haiku). Backends detect 529/overload and raise OverloadError.

### P2 — Planned

5. ~~**Phase execution timeout**~~ — **DONE**. Per-phase `timeout` field and `settings.default_timeout`, enforced via `asyncio.wait_for()` on both executor and gate calls. Timeout failures are hard failures (no retry). Subphases inherit parent phase timeout.
6. ~~**Structured JSONL logging**~~ — **DONE**. `--log` flag writes JSONL events (`workflow_start`, `phase_completed`, `phase_failed`, `gate_evaluated`, `phase_retried`, `workflow_end`) via state-diff detection in the runner's `astream` loop.
7. ~~**Output directory scaffolding**~~ — **DONE**. `scaffold_output_directory()` in `contracts.py` pre-creates `base_directory` (with `parents=True`) before execution. Called in `_make_phase_node()` after context setup, before `executor.execute()`.
8. ~~**Preamble injection**~~ — **DONE**. `settings.preamble_file` prepended to all prompt phases for shared project context. PromptExecutor prepends preamble contents before template rendering. Missing preamble file is a hard failure.

### P3 — Nice to have

9. ~~**Stepped retry backoff**~~ — **DONE**. `settings.retry_backoff` list of delay values in seconds. Applied via `asyncio.sleep()` before retry execution in `_make_phase_node`. Clamps to last value for attempts beyond list length. Empty list (default) = no delay.
10. ~~**Token usage tracking**~~ — **DONE**. Per-phase token counts (`input`/`output`) flow from `PromptBackendResult` → `PhaseResult` → `WorkflowState.token_usage`. CLI prints totals after run. JSONL `phase_completed` events include `tokens` field. ACP backend captures usage if exposed; stub/command phases return `None` gracefully.
11. Execution mode fallback chain (ACP → direct API on failure)
12. Post-workflow cleanup (remove intermediate artifacts on success)
13. ~~**Extended env var injection into validators**~~ — **DONE**. Gate validator scripts receive `PHASE_ID`, `WORKFLOW_NAME`, `ATTEMPT_NUMBER`, and `WORKDIR` as environment variables.
14. Git integration for outputs (auto-push to branch on completion)
15. Health check endpoint (for container orchestration)

## Testing

### Facts about the current suite
- All tests use real execution — no mocks of external systems
- Command phases use real subprocesses (`echo`, `cat`, `false`)
- Gate validators are real Python scripts that inspect stdin
- ACP integration tests spawn real claude-code-acp processes
- E2E joke workflow: generate → deterministic gate → select, all through ACP

### Testing Guidelines (apply to any new tests)

1. **No mocks of external systems.** Tests use real subprocess / real ACP / real validators. `MockExecutor` is a custom test double implementing the `PhaseExecutor` Protocol, NOT `unittest.mock`.
2. **Tests validate output, not just "runs without errors."** Every test asserts specific values — output strings, state keys, file contents, graph shape. A test that only checks "no exception raised" is not a meaningful test.
3. **Known-good AND known-bad fixtures for function-level tests.** Every helper gets a pair of tests: success path with expected output, failure/edge case with expected outcome. Use `@pytest.mark.parametrize` for routing tables (score/threshold/retry combinations).
4. **Multi-function end-to-end tests** use simple workflows scoped to each scenario (linear, diamond, dynamic subphase, resume, ACP). E2E tests assert concrete output values and state, not just "test completed."
5. **No separate test codepaths in functions.** Functions must not have `if testing:` branches. If a function can't be tested without special-casing, redesign it.
6. **No fallbacks or workarounds to make tests pass.** No `try/except: pytest.skip(...)` to mask missing dependencies. No `@pytest.mark.skipif(not_installed)` for ACP — it's a hard pre-req enforced at collection time.
7. **If testing is not possible** (missing auth, unreliable environment): STOP and raise the question. Do not paper over.
8. **Quality over count.** Test count is not a substitute for good tests. A small number of tests that validate meaningful output beats many tests that only check for absence of exceptions.
9. **Layer boundary tests** (`tests/architecture/test_layers.py`) enforce the three-layer split at CI time via AST walking. New source files must respect the import rules: `schema/` imports no langgraph; `compile/` only imports langgraph/schema/runtime; `runtime/` imports no compile/langgraph.
10. **ACP tests require `@zed-industries/claude-code-acp`** installed globally (`npm i -g @zed-industries/claude-code-acp`). The pre-flight check in `tests/conftest.py` exits with install instructions if it's missing. Developers can explicitly opt out with `pytest --ignore=tests/acp`.
