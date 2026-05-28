# sqrlly

**A high-level interface to and extension of [LangGraph](https://github.com/langchain-ai/langgraph) for agents in local environments.** Declare a workflow in YAML; sqrlly compiles it to a runnable `StateGraph` and adds the runtime extensions agent workflows need:

- **Nodes** execute one of: an LLM prompt (agent-with-tools or text-only), an interpreted script (`.py` / `.js` / `.ts` / `.sh`), a binary, or a recursive subgraph.
- **Local context** — when run inside a git repo, every node gets its own isolated worktree; agents read dep outputs, edit files on disk, and run tools.
- **Quality gates** — a script or LLM scorer judges output; failing gates re-run the node with feedback injected, up to a configurable retry budget.
- **Branching** — `route:` sends flow conditionally; `fan_out:` spawns parallel branches over a JSON manifest.
- **Resumable** — state checkpoints to SQLite; `--resume` picks up where a stopped run left off.

## Install

For CLI use (recommended — isolated venv, doesn't touch your project's environment):

```bash
pipx install sqrlly             # or: uv tool install sqrlly
```

As a project dependency:

```bash
pip install sqrlly             # core
pip install "sqrlly[acp]"      # + ACP backend
```

LLM dispatch currently goes through the local `claude-code-acp`
adapter (also requires `npm i -g @zed-industries/claude-code-acp` on
your PATH). The direct-API backends (Anthropic / OpenAI / DeepSeek /
custom OpenAI-compatible endpoints) were removed in 0.2.x while the
project consolidates around a single transport — a re-introduced
`transport: cli` (and possibly `transport: api`) is on the roadmap.

**Auth is per-CLI, not sqrlly's job.** Each LLM CLI you point sqrlly
at handles its own credentials independently. For ACP today, the
adapter inherits whatever session `claude` itself has — log in once
with `claude /login` and sqrlly's runs use that session. For future
`transport: cli` providers, each tool authenticates itself
(`codex auth`, `gemini auth login`, etc.). sqrlly never reads or
stores API keys for these tools.

Python 3.11+. From source: `git clone` then `uv sync`.

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

- `validate` — compiles the graph and reports the node count.
- `run` — executes the workflow; with `--log`, writes a JSONL event stream (`workflow_start`, `node_completed`, `gate_evaluated`, `node_retried`, `workflow_end`).

## How it works

- **URL-based dispatch** — `execute.url`'s extension picks the handler: `.md` / `.txt` / `.prompt` → LLM prompt, `.py` / `.js` / `.ts` / `.sh` → script, `.yaml` → subgraph, anything else → binary.
- **Jinja2 templates** — prompt files are full Jinja2; `{{generate}}` interpolates an upstream node's output; `{% if %}` / `{% for %}` / filters all work.
- **Retry feedback loop** — when a gate scores below `threshold`, the next attempt's context gets `{{_retry_reason}}` auto-populated with the previous score, per-dimension thresholds, and feedback.
- **Worktree isolation** — inside a git repo, each node runs in its own `git worktree` under `<workdir>/.sqrlly/`, reused across retries so prompt nodes can iterate on prior files.
- **Checkpointed state** — runs persist to `<workdir>/.sqrlly-checkpoint.db` (LangGraph `AsyncSqliteSaver`); `--resume` continues from the last checkpoint.
- **Recursive subgraphs** — a `.yaml` URL runs another sqrlly workflow; the same file works standalone or as a subgraph reference.

## Backends and presets

LLM and script execution is configured by **presets** under `settings.presets` — named bundles referenced by a node's `params.preset`. Exactly one LLM preset is marked `default: true`:

```yaml
settings:
  presets:
    default:
      transport: acp
      provider: anthropic
      model: sonnet
      default: true
```

**Declare presets explicitly.** sqrlly doesn't probe your environment
or synthesize defaults. Empty `settings.presets` is valid for
script-only workflows; any LLM-dispatching node will fail at its call
site with a clear "no prompt backend wired" error. For ACP, the
adapter inherits the local `claude` CLI session — no API keys needed,
just `npm i -g @zed-industries/claude-code-acp` and `claude /login`
once. If the adapter or `npx` is missing at run time, the backend
surfaces a clear error at the first prompt call (not pre-flight,
which would forbid workflows that install the adapter as an earlier
script node).

Full reference (including `CommandPreset` for custom script interpreters): [docs/schema-reference.md](https://github.com/christopherseaman/sqrlly/blob/main/docs/schema-reference.md).

## CLI

| Command | Purpose |
|---|---|
| `sqrlly validate <config>` | Compile the workflow; report node count and lint warnings. |
| `sqrlly run <config>` | Execute the workflow. |
| `sqrlly graph <config>` | Print the topology as a Mermaid diagram. |
| `sqrlly view <config>` | Render a self-contained interactive HTML viewer. |

`run` flags:

- `--workdir / -w <dir>` — working directory (default `.`).
- `--dry-run` — trace topology without executing.
- `--preset / -p <name>` — force a named preset as the default.
- `--resume` — continue from the last checkpoint.
- `--log <path>` — write a JSONL event log.

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
- **[SKILLS.md](https://github.com/christopherseaman/sqrlly/blob/main/SKILLS.md)** — agent skill doc: instructions for an AI coding agent authoring and running sqrlly workflows.

## Contributing

- **Three-layer split** — `schema/` (Pydantic models) → `compile/` (YAML → LangGraph) → `runtime/` (executors, backends). Layer rules enforced at CI time by an AST import check.
- **No mocks of external systems** — tests run real subprocesses, real `git worktree`, and real backends.
- See **[TECHNICAL.md](https://github.com/christopherseaman/sqrlly/blob/main/TECHNICAL.md)** for architecture and contributor reading order.

```bash
uv run pytest tests/ --ignore=tests/acp     # ~840 tests
uv run pytest tests/acp                     # ACP integration (needs the npm adapter)
```

## License

Apache-2.0 — see [LICENSE](https://github.com/christopherseaman/sqrlly/blob/main/LICENSE).
