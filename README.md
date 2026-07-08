# sqrlly

**A high-level interface to and extension of [LangGraph](https://github.com/langchain-ai/langgraph) for agents in local environments.** Declare a workflow in YAML; sqrlly compiles it to a runnable `StateGraph` and adds the runtime extensions agent workflows need:

- **Nodes** execute one of: an LLM prompt (agent-with-tools or text-only), an interpreted script (`.py` / `.js` / `.ts` / `.sh`), a binary, or a recursive subgraph.
- **Local context** — when run inside a git repo, every node gets its own isolated worktree; agents read dep outputs, edit files on disk, and run tools.
- **Quality gates** — a script or LLM scorer judges output; failing gates re-run the node with feedback injected, up to a configurable retry budget.
- **Branching** — `route:` sends flow conditionally; `fan_out:` spawns parallel branches over a JSON manifest.
- **Fault-recovery resume** — state checkpoints to SQLite; `--resume` re-runs seeded with the prior run's state and clears failures to retry.

## Install

For CLI use (recommended — isolated venv, doesn't touch your project's environment):

```bash
pipx install sqrlly             # or: uv tool install sqrlly
uvx sqrlly --help               # run once, no install
```

As a project dependency:

```bash
uv add sqrlly                   # CLI backend    (or: pip install sqrlly)
uv add "sqrlly[acp]"            # + ACP backend  (or: pip install "sqrlly[acp]")
```

LLM dispatch goes through Claude Code via one of two transports:

- `transport: acp` — the `claude-code-acp` adapter (warm process,
  streaming chunks). Requires `npm i -g @zed-industries/claude-code-acp`.
- `transport: cli` — `claude -p` subprocess-per-call (no warm
  process, real `asyncio` parallelism). Requires `claude` on PATH.

The direct-API backends (Anthropic / OpenAI / DeepSeek / custom
OpenAI-compatible endpoints) were removed in 0.2.x while the project
consolidates around Claude Code; restoring `transport: api` remains
on the roadmap, and additional `transport: cli` providers (codex /
gemini) are tracked in TODO 36.

**Auth is per-CLI, not sqrlly's job.** Each CLI you point sqrlly at
handles its own credentials independently — log in once with
`claude /login` and sqrlly's runs (acp OR cli) use that session.
Future cli providers will follow the same pattern (`codex auth`,
`gemini auth login`, etc.). sqrlly never reads or stores API keys
for these tools.

Python 3.11+. From source: `git clone` then `uv sync`.

**Using sqrlly from a coding agent?** `sqrlly init --skill` installs the
agent skill into your repo at `.agents/skills/sqrlly/SKILL.md`
(repo-aware) so the agent auto-discovers how to author and run
workflows.

## Quickstart

Bootstrap a runnable workflow in two commands — no git clone needed:

```bash
sqrlly init my-workflow
cd my-workflow && sqrlly run workflow.yaml
```

`sqrlly init` scaffolds `workflow.yaml` (one prompt node, CLI preset) and `prompts/hello.md` you can edit in place.

For something more substantive, the shipped example is two nodes — generate jokes, gate them, pick the best (`examples/jokes/`):

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
      transport: cli
      provider: anthropic
      model: sonnet
      default: true
```

```bash
sqrlly validate examples/jokes/workflow.yaml
sqrlly run examples/jokes/workflow.yaml --log run.jsonl
```

- `validate` — compiles the graph and reports the node count.
- `run` — executes the workflow; with `--log`, writes a JSONL event stream (`workflow_start`, `node_model`, `node_completed`, `node_failed`, `gate_evaluated`, `node_retried`, `workflow_end`).

## How it works

- **URL-based dispatch** — `execute.url`'s extension picks the handler: `.md` / `.txt` / `.prompt` → LLM prompt, `.py` / `.js` / `.ts` / `.sh` → script, `.yaml` → subgraph, anything else → binary.
- **Jinja2 templates** — prompt files are full Jinja2; `{{generate}}` interpolates an upstream node's output; `{% if %}` / `{% for %}` / filters all work.
- **Retry feedback loop** — when a gate scores below `threshold`, the next attempt's context gets `{{_retry_reason}}` auto-populated with the previous score, per-dimension thresholds, and feedback.
- **Worktree isolation** — inside a git repo, each node runs in its own `git worktree` under `<workdir>/.sqrlly/`, reused across retries so prompt nodes can iterate on prior files.
- **Checkpointed state** — runs persist to `<workdir>/.sqrlly-checkpoint.db` (LangGraph `AsyncSqliteSaver`); `--resume` re-runs seeded with that state, clearing failed nodes to retry. Completed nodes skip by default (fault-recovery re-run, not full replay) unless `--rerun-all` is set. For fan-out branches: failed children re-run, but completed siblings stay frozen (no re-billing).
- **Recursive subgraphs** — a `.yaml` URL runs another sqrlly workflow; the same file works standalone or as a subgraph reference.

## Backends and presets

LLM and script execution is configured by **presets** under `settings.presets` — named bundles referenced by a node's `params.preset`. Exactly one LLM preset is marked `default: true`:

```yaml
settings:
  presets:
    default:
      transport: cli       # or acp — see below
      provider: anthropic
      model: sonnet
      default: true
```

Pick a transport by what shape suits the workflow:

| Transport | Invocation | Best for |
|---|---|---|
| `acp` | `claude-code-acp` adapter (warm process, streaming) | Workflows that want streamed chunks or MCP-via-session. |
| `cli` | `claude -p` subprocess per call (no warm state) | Fan-out workflows — real `asyncio` parallelism per call, simpler lifecycle. |

Both pair with `provider: anthropic` today and both authenticate via the local `claude` CLI session — `claude /login` once and either transport runs against it. Multiple presets can coexist in a single workflow; only one is `default: true`, others get named via `params.preset` per node.

**Declare presets explicitly.** sqrlly doesn't probe your environment
or synthesize defaults. Empty `settings.presets` is valid for
script-only workflows; any LLM-dispatching node will fail at its call
site with a clear "no prompt backend wired" error. If a backend's
toolchain is missing at run time (`npx` for acp, `claude` for cli),
the backend surfaces a clear error at the first prompt call — never
as a pre-flight check (a pre-flight would forbid workflows that
install the toolchain in an earlier script node).

Full reference (including `CommandPreset` for custom script interpreters): [SCHEMA.md](https://github.com/christopherseaman/sqrlly/blob/main/SCHEMA.md).

## CLI

| Command | Purpose |
|---|---|
| `sqrlly init [<dir>]` | Scaffold a minimal runnable workflow into `<dir>` (default `.`). |
| `sqrlly init --example <name>` | Scaffold a bundled example into `./<name>` (`--list-examples` to see them). |
| `sqrlly validate <config>` | Compile the workflow; report node count and lint warnings. |
| `sqrlly run <config>` | Execute the workflow. |
| `sqrlly graph <config>` | Print the topology as a Mermaid diagram. |
| `sqrlly view <config>` | Render a self-contained interactive HTML viewer. |

`run` flags:

- `--workdir / -w <dir>` — working directory (default `.`).
- `--dry-run` — trace topology without executing.
- `--preset / -p <name>` — force a named preset as the default.
- `--resume` — re-run seeded with the prior run's state, clearing failures to retry. Completed nodes skip by default; use `--rerun-all` to re-execute every node.
- `--entry <node>` — cold-start at `<node>`: run it and everything downstream WITHOUT a checkpoint (the upstream artifacts must already be on disk). Mutually exclusive with `--resume` / `--resume-from` / `--rerun-all`.
- `--log <path>` — write a JSONL event log.
- `--quiet / -q` — suppress the live terminal renderer (use in CI / piped runs).
- `--safe-mode / --no-safe-mode` — run Claude with operator customizations (output styles, CLAUDE.md, skills, MCP, hooks) disabled, for clean reproducible output (cli transport). Overrides `settings.safe_mode`; absent, the setting applies.

`run` shows a live per-node status grid + a clock-driven aliveness spinner when stdout is a TTY. Non-TTY runs (piped, redirected, CI) fall through to the plain summary output automatically. Workflow events only — no LLM-token streaming. On completion `run` prints a `where to find things` summary — the produced output files, the log path (or how to capture one with `--log`), and run artifacts (checkpoint DB + worktree pool); `--quiet` suppresses it.

`init --example <name>` scaffolds a runnable bundled example (`jokes`, `route_classify`, `explicit_join`, `pipeline_style`, `absurd-paper`) — handy after `uv tool install sqrlly` when you have no repo checkout. `init --list-examples` lists them.

## Examples

Render any example's interactive view with `sqrlly view <workflow.yaml>`; `examples/regenerate_views.sh` rebuilds views (and, for the deterministic script-only examples, debug views from a captured log) locally.

| Workflow | What it shows |
|---|---|
| [`examples/jokes/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/jokes/workflow.yaml) | Minimal: prompt + script gate + select. Start here. |
| [`examples/route_classify/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/route_classify/workflow.yaml) | Inline `route:` case ladder over structured state. |
| [`examples/pipeline_style/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/pipeline_style/workflow.yaml) | `route: { goto: <next> }` forward-edge authoring; a linear pipeline. |
| [`examples/absurd-paper/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/absurd-paper/workflow.yaml) | Full pipeline: prompts, multi-dim quality gates, a subgraph, and a rendered PDF. Try `sqrlly init --example absurd-paper`. |
| [`examples/wave_planner/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/wave_planner/workflow.yaml) | Wave-driven dynamic-task pattern — `goto:` loop-back to a fan-out parent. |

## Documentation

- **[SCHEMA.md](https://github.com/christopherseaman/sqrlly/blob/main/SCHEMA.md)** — every Settings / Node / Execute / preset / route / evaluation field, the URL dispatch table, and the route-predicate namespace.
- **[SKILLS.md](https://github.com/christopherseaman/sqrlly/blob/main/SKILLS.md)** — agent skill doc: instructions for an AI coding agent authoring and running sqrlly workflows.

## Contributing

- **Three-layer split** — `schema/` (Pydantic models) → `compile/` (YAML → LangGraph) → `runtime/` (executors, backends). Layer rules enforced at CI time by an AST import check.
- **No mocks of external systems** — tests run real subprocesses, real `git worktree`, and real backends.
- **Layer rules** — `tests/architecture/test_layers.py` enforces the import boundaries; `CLAUDE.md` has the architecture sketch + contributor reading order.

```bash
uv run pytest tests/ --ignore=tests/acp     # ~1k tests
uv run pytest tests/acp                     # ACP integration (needs the npm adapter)
```

## License

Apache-2.0 — see [LICENSE](https://github.com/christopherseaman/sqrlly/blob/main/LICENSE).
