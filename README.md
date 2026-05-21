# sqrlly

**A workflow orchestrator that compiles a YAML file into a runnable LangGraph.** Each step runs an LLM prompt, a script, a binary, or a nested workflow; quality gates retry failing steps with feedback; routing and fan-out handle branching and parallelism.

## What it is

sqrlly turns a declarative YAML workflow into a compiled LangGraph `StateGraph` and runs it. A workflow is a list of **nodes** — each node executes one thing: a prompt against an LLM, an interpreted script (`.py` / `.js` / `.ts` / `.sh`), a binary, or a recursive subgraph (another sqrlly workflow). **Quality gates** wrap any node — a gate scores the node's output and retries it with feedback until it passes. **Routing** sends flow to the next node conditionally; **fan-out** spawns a parallel branch per item in a manifest. State checkpoints to SQLite, so `--resume` picks up where a stopped run left off.

## Install

```bash
pip install sqrlly                      # core
pip install "sqrlly[anthropic,openai]"  # + API backends
```

Backend extras:

- **`anthropic`** — Claude via the Anthropic API.
- **`openai`** — OpenAI, DeepSeek, and any OpenAI-compatible endpoint.
- **`acp`** — Claude via the local `claude-code-acp` adapter (also needs `npm i -g @zed-industries/claude-code-acp`).

Requires Python 3.11+. From source: `git clone` then `uv sync` (add `--extra openai --extra anthropic` for backends).

## Quickstart

A two-node workflow — generate jokes, gate them, pick the best (`examples/jokes/`):

```yaml
name: "Joke Generator"
version: "0.1.0"

nodes:
  - id: generate
    name: "Generate Jokes"
    execute:
      url: "examples/jokes/generate.md"
    evaluation:
      validator: "examples/jokes/gates/validate_jokes.py"
      threshold: 1.0
      blocking: true
      max_retries: 2

  - id: select
    name: "Select Best Joke"
    depends_on: ["generate"]
    execute:
      url: "examples/jokes/select.md"

settings:
  presets:
    default:
      transport: acp
      provider: anthropic
      model: sonnet
      default: true
```

```bash
sqrlly validate examples/jokes/workflow.yaml
sqrlly run examples/jokes/workflow.yaml --log run.jsonl
```

`validate` compiles the graph and reports the node count. `run` executes it and writes a JSONL event log you can inspect for `workflow_start`, `node_completed`, `gate_evaluated`, `node_retried`, and `workflow_end` events.

## How it works

A node's `execute.url` is dispatched by extension: `.md` / `.txt` / `.prompt` → LLM prompt, `.py` / `.js` / `.ts` / `.sh` → script, `.yaml` → subgraph, anything else → binary. Prompt files are Jinja2 templates — `{{generate}}` interpolates the output of an upstream node named `generate`.

- **Gates & retries** — an `evaluation:` block runs a script or LLM scorer over a node's output. Below `threshold`, the node re-runs with the gate's feedback injected (`{{_retry_reason}}`), up to `max_retries`.
- **Routing** — a `route:` block declares where flow goes next: `goto:` for unconditional dispatch, or a `cases:` / `else:` predicate ladder for conditional branching.
- **Fan-out** — a `fan_out:` block spawns one parallel branch per item in a JSON manifest, optionally backed by a per-item subgraph.
- **Subgraphs** — a node whose `execute.url` is a `.yaml` runs another workflow, recursively. The same file is runnable standalone or as a subgraph reference.
- **Foreman** — when run inside a git repository, each node executes in its own git worktree, reused across retries so prompt nodes can iterate on prior files.
- **Resume** — state checkpoints to `<workdir>/.sqrlly-checkpoint.db`; `--resume` restarts from the last checkpoint.

## Backends and presets

LLM and script execution is configured by **presets** under `settings.presets` — named bundles referenced by a node's `params.preset`. Exactly one LLM preset is marked `default: true`:

```yaml
settings:
  presets:
    default:
      transport: api        # api | acp
      provider: anthropic   # anthropic | openai | deepseek | custom
      model: sonnet
      default: true
```

If `settings.presets` is omitted entirely, sqrlly auto-detects a backend from environment keys, in order: `ANTHROPIC_API_KEY` → `DEEPSEEK_API_KEY` → `npx` on `PATH` (ACP). API keys load from the environment or a project-local `.env` file (copy `.env.example`):

```bash
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...                          # reserved for real openai.com
CUSTOM_API_KEY=...                             # any OpenAI-compatible endpoint
CUSTOM_API_BASE_URL=https://openrouter.ai/api/v1
```

The full preset and settings reference — including `CommandPreset` for custom script interpreters — is in [docs/schema-reference.md](https://github.com/christopherseaman/sqrlly/blob/main/docs/schema-reference.md).

## CLI

| Command | Purpose |
|---|---|
| `sqrlly validate <config>` | Compile the workflow; report node count and lint warnings. |
| `sqrlly run <config>` | Execute the workflow. |
| `sqrlly graph <config>` | Print the topology as a Mermaid diagram. |
| `sqrlly view <config>` | Render a self-contained interactive HTML viewer. |

`run` flags: `--workdir/-w <dir>`, `--dry-run`, `--preset/-p <name>` (force a named preset as default), `--resume`, `--log <path>`.

## Examples

Each example directory ships a checked-in `view.html` (authoring view) and, where the run is deterministic, a `view-debug.html` from a captured log.

| Workflow | What it shows |
|---|---|
| [`examples/jokes/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/jokes/workflow.yaml) | Minimal: prompt + script gate + select. Start here. |
| [`examples/route_classify/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/route_classify/workflow.yaml) | Inline `route:` case ladder over structured state. |
| [`examples/pipeline_style/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/pipeline_style/workflow.yaml) | `route: { goto: <next> }` forward-edge authoring; a linear pipeline. |
| [`examples/absurd-paper/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/absurd-paper/workflow.yaml) | 13-node multi-stage pipeline with subgraphs and per-item subgraph fan-out. |
| [`examples/wave_planner/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/wave_planner/workflow.yaml) | Wave-driven dynamic-task pattern — `goto:` loop-back to a fan-out parent. |

## Documentation

- **[docs/schema-reference.md](https://github.com/christopherseaman/sqrlly/blob/main/docs/schema-reference.md)** — every Settings / Node / Execute / preset / route / evaluation field, the URL dispatch table, and the route-predicate namespace.
- **[TECHNICAL.md](https://github.com/christopherseaman/sqrlly/blob/main/TECHNICAL.md)** — three-layer architecture, pipeline flow, state model, key invariants.
- **[.agents/skills/sqrlly/SKILL.md](https://github.com/christopherseaman/sqrlly/blob/main/.agents/skills/sqrlly/SKILL.md)** — an agent skill: instructions for an AI coding agent authoring and running sqrlly workflows.

## Contributing

The codebase is a three-layer split — `schema/` (Pydantic models) → `compile/` (YAML → LangGraph) → `runtime/` (executors, backends) — enforced at CI time by an AST import check. Tests use real subprocesses, real `git worktree`, and real backends (no mocks of external systems). See [TECHNICAL.md](https://github.com/christopherseaman/sqrlly/blob/main/TECHNICAL.md) for architecture and contributor reading order.

```bash
uv run pytest tests/ --ignore=tests/acp     # ~840 tests
uv run pytest tests/acp                     # ACP integration (needs the npm adapter)
```

## License

Apache-2.0 — see [LICENSE](https://github.com/christopherseaman/sqrlly/blob/main/LICENSE).
