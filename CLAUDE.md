# Abe Froman

Workflow orchestrator using LangGraph for orchestration and Claude Agent SDK for execution.

## Architecture

```
YAML Config → Pydantic Schema → LangGraph StateGraph
                                       │
                                       ▼
                                 Phase Node → PhaseExecutor (Protocol)
                                       │            │
                                       │      ┌─────┼──────┐
                                       │      ▼     ▼      ▼
                                       │    ACP   Cmd   Mock
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
uv run pytest tests/ -v                    # run all tests
uv run pytest tests/test_schema.py -v      # run specific test file
uv run abe-froman validate config.yaml     # validate a workflow config
uv run abe-froman run config.yaml --dry-run # dry run
uv run abe-froman graph config.yaml        # print dependency graph
```

## Project Layout

- `src/abe_froman/schema/models.py` — Pydantic models (WorkflowConfig, Phase, execution types)
- `src/abe_froman/engine/state.py` — WorkflowState TypedDict with LangGraph reducers
- `src/abe_froman/engine/builder.py` — YAML → LangGraph StateGraph construction
- `src/abe_froman/engine/gates.py` — Quality gate evaluation + routing
- `src/abe_froman/executor/base.py` — PhaseExecutor Protocol + PhaseResult
- `src/abe_froman/executor/acp.py` — ACP executor via claude-code-acp (Phase 5, not yet implemented)
- `src/abe_froman/executor/dispatch.py` — Routes execution by phase type (command/gate_only/prompt stub)
- `src/abe_froman/executor/command.py` — Subprocess executor
- `src/abe_froman/executor/mock.py` — Mock executor for testing
- `src/abe_froman/cli/main.py` — Click CLI

## Key Design Decisions

- **PhaseExecutor Protocol** (not ABC) — duck-typed, agent-agnostic
- **Discriminated union** for execution types: PromptExecution | CommandExecution | GateOnlyExecution
- **Quality gates owned by orchestrator**, not executor
- **LangGraph state with Annotated reducers** for safe parallel state merging
- **Model per phase** with `settings.default_model` fallback
- **`{{variable}}` templating** in prompt files for parameterized execution

## Testing Patterns

- Layer 1: Unit tests with fixtures (schema, gates)
- Layer 2: Single-node workflows (builder, each execution type)
- Layer 3: Multi-node integration (orchestrator, mock executor)
- Layer 4: CLI smoke tests (CliRunner)
- Layer 5: Claude executor integration (mocked SDK client)
