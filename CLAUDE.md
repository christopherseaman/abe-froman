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
uv run pytest tests/ -v                    # run all tests (~140 tests, ~60s)
uv run abe-froman validate config.yaml     # validate a workflow config
uv run abe-froman run config.yaml --dry-run # dry run
uv run abe-froman run config.yaml -e acp   # run with Claude via ACP
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

    # Dynamic subphases — optional (Phase 6, not yet implemented)
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

### Quality gate validators

**Script validators** (`.py`, `.js`) receive the phase output on stdin and must print a score to stdout:

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

## Testing

- All tests use real execution — no mocks of external systems
- Command phases use real subprocesses (`echo`, `cat`, `false`)
- Gate validators are real Python scripts that inspect stdin
- ACP integration tests spawn real claude-code-acp processes
- E2E joke workflow: generate → deterministic gate → select, all through ACP
