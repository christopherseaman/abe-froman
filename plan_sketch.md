# Workflow Orchestrator: Claude Agent SDK + LangGraph

__It's a little childish and stupid, but then, so is high school.__

## Architecture

```
YAML Config ─▶ Pydantic Schema ─▶ LangGraph StateGraph
                                         │
                                         ▼
                                   Phase Node
                                         │
                                         ▼
                              Claude Agent SDK (ClaudeSDKClient)
                                         │
                              ┌──────────┴──────────┐
                              ▼                     ▼
                         MCP Tools              Built-in Tools
                    (validators, gates)      (Read, Write, Bash)
                              │                     │
                              └──────────┬──────────┘
                                         ▼
                                  Quality Gate
                                         │
                              ┌──────────┼──────────┐
                              ▼          ▼          ▼
                            pass       retry       fail
                              │          │          │
                              ▼          ▼          ▼
                         Next Phase   Same Node    END
```

## Stack

| Layer | Component |
|-------|-----------|
| Definition | YAML configs + Pydantic validation |
| Orchestration | LangGraph (StateGraph, conditional edges, Send API) |
| Execution | Claude Agent SDK (ClaudeSDKClient) |
| Tools | In-process MCP servers |
| Persistence | LangGraph checkpointing |

---

## YAML Schema

```yaml
name: "Workflow Name"
version: "1.0.0"

phases:
  - id: phase-1
    name: "Phase Name"
    prompt_file: "phases/phase-1.md"
    depends_on: []
    parallel: false
    quality_gate:
      validator: "gates/validator.py"
      threshold: 0.85
      blocking: false
    output_contract:
      base_directory: "output/"
      required_files: ["result.json"]
    dynamic_subphases:
      enabled: false
      manifest_path: "manifest.json"
      template: "template.md"

settings:
  output_directory: "output"
  max_retries: 3
```

### Pydantic Models

```python
from pydantic import BaseModel

class QualityGate(BaseModel):
    validator: str
    threshold: float
    blocking: bool = False

class OutputContract(BaseModel):
    base_directory: str
    required_files: list[str]
    validation_only: bool = False

class DynamicPhaseConfig(BaseModel):
    enabled: bool = False
    manifest_path: str | None = None
    template: str | None = None

class Phase(BaseModel):
    id: str
    name: str
    description: str | None = None
    prompt_file: str | None = None
    depends_on: list[str] = []
    parallel: bool = False
    quality_gate: QualityGate | None = None
    output_contract: OutputContract | None = None
    dynamic_subphases: DynamicPhaseConfig | None = None

class WorkflowConfig(BaseModel):
    name: str
    version: str
    phases: list[Phase]
    settings: dict = {}
```

### YAML → LangGraph Mapping

| YAML | LangGraph |
|------|-----------|
| `phases` | `StateGraph.add_node()` |
| `depends_on` | `add_edge()` |
| `quality_gate` | `add_conditional_edges()` |
| `parallel: true` | Multiple edges from START |
| `dynamic_subphases` | `Send()` API |
| `blocking: true` + fail | Edge to END |
| retry on gate fail | Edge back to same node |

---

## LangGraph State

```python
from typing import TypedDict, Annotated
import operator

class WorkflowState(TypedDict):
    workflow_name: str
    current_phase_id: str | None
    completed_phases: Annotated[list[str], operator.add]
    phase_outputs: dict[str, dict]
    gate_scores: dict[str, float]
    retries: dict[str, int]
    errors: Annotated[list[dict], operator.add]
    workdir: str
```

---

## Claude Agent SDK Integration

### ClaudeSDKClient vs query()

| Feature | query() | ClaudeSDKClient |
|---------|---------|-----------------|
| Session | New each call | Persistent |
| Custom Tools | ✗ | ✓ (via MCP) |
| Hooks | ✗ | ✓ |
| Interrupts | ✗ | ✓ |
| Use for | Independent phases | Context-dependent phases |

### Custom MCP Tools

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("validate_output", "Validate phase output", {
    "phase_id": str,
    "base_directory": str,
    "required_files": list
})
async def validate_output(args: dict) -> dict:
    missing = []
    base = Path(args["base_directory"])
    for f in args["required_files"]:
        if not (base / f).exists():
            missing.append(f)
    
    return {
        "content": [{
            "type": "text",
            "text": f"passed={len(missing)==0}, missing={missing}"
        }]
    }

@tool("run_quality_gate", "Execute validator", {
    "validator_path": str,
    "phase_id": str
})
async def run_quality_gate(args: dict) -> dict:
    # Execute validator script, return score
    score = await execute_validator(args["validator_path"])
    return {
        "content": [{
            "type": "text", 
            "text": f"score={score}"
        }]
    }

workflow_tools = create_sdk_mcp_server(
    name="workflow",
    tools=[validate_output, run_quality_gate]
)
```

### Hooks

```python
from claude_agent_sdk import HookMatcher, HookContext

async def post_write_hook(
    input_data: dict,
    tool_use_id: str | None,
    context: HookContext
) -> dict:
    """Trigger validation after file writes"""
    if input_data.get("tool_name") == "Write":
        return {"systemMessage": "Validating output..."}
    return {}

options = ClaudeAgentOptions(
    hooks={
        "PostToolUse": [HookMatcher(matcher="Write", hooks=[post_write_hook])]
    }
)
```

---

## Phase Executor

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock
from pathlib import Path

async def execute_phase(phase: Phase, state: WorkflowState, tools_server) -> dict:
    prompt = Path(phase.prompt_file).read_text() if phase.prompt_file else ""
    
    # Inject context from dependencies
    context = "\n".join(
        f"## {dep_id}:\n{state['phase_outputs'].get(dep_id, '')}"
        for dep_id in phase.depends_on
    )
    
    full_prompt = f"""
# {phase.name}
{phase.description or ''}

## Context from previous phases:
{context}

## Instructions:
{prompt}
"""

    options = ClaudeAgentOptions(
        mcp_servers={"workflow": tools_server},
        allowed_tools=[
            "Read", "Write", "Bash", "Glob", "Grep",
            "mcp__workflow__validate_output",
            "mcp__workflow__run_quality_gate"
        ],
        permission_mode="acceptEdits",
        cwd=state["workdir"]
    )
    
    result = {"output": "", "error": None}
    
    async with ClaudeSDKClient(options=options) as client:
        await client.query(full_prompt)
        
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result["output"] += block.text
            elif isinstance(msg, ResultMessage) and msg.is_error:
                result["error"] = msg.result
    
    return result
```

---

## LangGraph Builder

```python
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

def build_workflow_graph(config: WorkflowConfig, tools_server) -> StateGraph:
    builder = StateGraph(WorkflowState)
    
    # Create nodes
    for phase in config.phases:
        async def node(state, p=phase):
            result = await execute_phase(p, state, tools_server)
            return {
                "phase_outputs": {**state["phase_outputs"], p.id: result["output"]},
                "completed_phases": [p.id] if not result["error"] else [],
                "errors": [{"phase": p.id, "error": result["error"]}] if result["error"] else []
            }
        builder.add_node(phase.id, node)
    
    # Create edges
    for phase in config.phases:
        if not phase.depends_on:
            builder.add_edge(START, phase.id)
        else:
            for dep in phase.depends_on:
                if phase.quality_gate:
                    builder.add_conditional_edges(
                        dep,
                        lambda s, p=phase: gate_router(s, p),
                        {"pass": phase.id, "retry": dep, "fail": END}
                    )
                else:
                    builder.add_edge(dep, phase.id)
    
    # Terminal phases
    for phase in find_terminal_phases(config):
        builder.add_edge(phase.id, END)
    
    return builder.compile()

def gate_router(state: WorkflowState, phase: Phase) -> str:
    score = state["gate_scores"].get(phase.id, 0)
    threshold = phase.quality_gate.threshold
    retries = state["retries"].get(phase.id, 0)
    
    if score >= threshold:
        return "pass"
    elif retries < 3:
        return "retry"
    return "fail"
```

---

## Dynamic Phase Generation

```python
from langgraph.types import Send
import json

async def handle_dynamic_phases(state: WorkflowState, config: DynamicPhaseConfig):
    manifest = json.loads(Path(config.manifest_path).read_text())
    template = Path(config.template).read_text()
    
    return [
        Send("execute_subphase", {
            "component": component,
            "template": template,
            "parent_state": state
        })
        for component in manifest.get("components", [])
    ]
```

---

## CLI

```python
import click
import yaml
import asyncio
from langgraph.checkpoint.sqlite import SqliteSaver

@click.group()
def cli():
    pass

@cli.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.option("--phase", "-p", help="Run specific phase")
@click.option("--workdir", "-w", default=".", help="Working directory")
@click.option("--resume", "-r", help="Resume from thread ID")
def run(config_file: str, phase: str | None, workdir: str, resume: str | None):
    config = WorkflowConfig(**yaml.safe_load(Path(config_file).read_text()))
    graph = build_workflow_graph(config, workflow_tools)
    
    initial_state = {
        "workflow_name": config.name,
        "current_phase_id": phase,
        "completed_phases": [],
        "phase_outputs": {},
        "gate_scores": {},
        "retries": {},
        "errors": [],
        "workdir": workdir
    }
    
    with SqliteSaver.from_conn_string(":memory:") as checkpointer:
        app = graph.compile(checkpointer=checkpointer)
        result = asyncio.run(app.ainvoke(
            initial_state,
            config={"thread_id": resume or "main"}
        ))
    
    click.echo(f"Completed: {result['completed_phases']}")
    if result["errors"]:
        click.echo(f"Errors: {result['errors']}")

if __name__ == "__main__":
    cli()
```

---

## Module Structure

```
workflow_orchestrator/
├── schema/
│   ├── workflow.py      # Pydantic models
│   └── validators.py
├── executor/
│   ├── phase.py         # execute_phase()
│   ├── tools.py         # MCP tools
│   └── hooks.py
├── orchestrator/
│   ├── builder.py       # build_workflow_graph()
│   ├── state.py         # WorkflowState
│   └── router.py        # gate_router()
├── cli/
│   └── main.py
└── tests/
```

## Dependencies

```
claude-agent-sdk>=0.1.0
langgraph>=0.2.0
pydantic>=2.0
pyyaml>=6.0
click>=8.0
```
