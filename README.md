# abe-froman

Workflow orchestrator using LangGraph for graph topology and Claude / DeepSeek / scripts for execution.

## What it is

abe-froman compiles a YAML workflow into a LangGraph `StateGraph`. Each node executes one of: a prompt against an LLM backend, an interpreted script (`.py` / `.js` / `.ts` / `.sh`), a binary, a `join` topology marker, a `route` case ladder, or a recursive subgraph reference. The shape of every executable node is the unified Stage 5b `execute: { url, params }` block — the URL extension (or `mode:` override) drives the dispatcher.

Quality gates wrap each node. A gate is a script (`.py` / `.js`) reading the node output on stdin, or an `.md` Jinja prompt evaluated by the node's LLM backend. Gate failures retry the node with `{{_retry_reason}}` injected. Subgraphs are recursive — the same YAML schema is runnable standalone via `abe-froman run` or as a node reference from another graph. Fan-out spawns a `Send` per manifest item, optionally backed by a per-Send subgraph.

State persists through LangGraph's `AsyncSqliteSaver` checkpointer at `<workdir>/.abe-froman-checkpoint.db`, keyed by a deterministic SHA1 of `(workflow_name, resolved_workdir)`. `--resume` reads the last checkpoint and restarts from where the previous run stopped. When `--workdir` is a git repo, every node runs in its own git worktree under `<workdir>/.abe-foreman/`, retained across retries so prompt nodes can iterate on prior files.

## Install

```bash
uv sync                                  # core
uv sync --extra openai                   # for OpenAI / DeepSeek backend
npm i -g @zed-industries/claude-code-acp # for ACP backend
```

Python 3.11+ is required. The project develops on 3.14.

For the DeepSeek backend, set `DEEPSEEK_API_KEY` in the environment, or place a JSON file at `~/.pi/agent/auth.json` shaped as `{"deepseek": {"key": "..."}}`. Either source resolves to the same key.

## Quickstart

The minimal example lives in `examples/jokes/`. The workflow YAML:

```yaml
name: "Joke Generator"
version: "0.1.0"

nodes:
  - id: generate
    name: "Generate Jokes"
    evaluation:
      validator: "examples/jokes/gates/validate_jokes.py"
      threshold: 1.0
      blocking: true
      max_retries: 2
    execute:
      url: "examples/jokes/generate.md"
  - id: select
    name: "Select Best Joke"
    depends_on: ["generate"]
    execute:
      url: "examples/jokes/select.md"

settings:
  default_model: "sonnet"
  executor: "acp"
```

Validate, then run with a JSONL log:

```bash
uv run abe-froman validate examples/jokes/workflow.yaml
uv run abe-froman run examples/jokes/workflow.yaml --log /tmp/jokes.jsonl
```

The validate command echoes `Valid: Joke Generator v0.1.0 (2 nodes)`. The run command echoes per-node progress and finishes with a `Completed:` summary. Inspect `/tmp/jokes.jsonl` for `workflow_start`, `node_completed`, `gate_evaluated`, `node_retried`, `workflow_end` events.

## Backend selection

Resolution order at run time: `--executor` flag, then `settings.executor` in YAML, then auto-detect. Auto-detect picks the first available real backend:

1. DeepSeek key (env `DEEPSEEK_API_KEY` or `~/.pi/agent/auth.json`) → `deepseek`
2. `npx` on `PATH` → `acp`
3. Nothing → exit with a clear remediation message naming the env var or install command.

Setting only `ANTHROPIC_API_KEY` does not auto-pick a backend — a native Anthropic backend is on the wishlist but not yet wired, so this case falls through.

| Key         | Install                                    | Auth                                                    | Models                  | Cost                |
|-------------|--------------------------------------------|---------------------------------------------------------|-------------------------|---------------------|
| `acp`       | `npm i -g @zed-industries/claude-code-acp` | Claude CLI session (no API key needed)                  | Claude (opus/sonnet/haiku) | usage-billed via Anthropic |
| `deepseek`  | `uv sync --extra openai`                   | `DEEPSEEK_API_KEY` or `~/.pi/agent/auth.json`           | DeepSeek catalog (e.g. `deepseek-chat`, `deepseek-reasoner`, `deepseek-v4-flash`) | usage-billed via DeepSeek |
| `openai`    | `uv sync --extra openai`                   | `OPENAI_API_KEY` (or pass `api_key=` programmatically)  | OpenAI catalog or compatible base_url | usage-billed |

## CLI reference

Four commands. `validate`, `run`, `migrate`, `graph`.

### validate

Compile the YAML and report node count.

```bash
uv run abe-froman validate config.yaml
```

### run

Execute the workflow.

| Flag           | Default | Description |
|----------------|---------|-------------|
| `--workdir, -w` | `.`    | Working directory for prompt files, scripts, and outputs. |
| `--dry-run`    | `false` | Validate and trace without executing. |
| `--model, -m`  | (none)  | Override `settings.default_model`. |
| `--executor, -e` | (auto) | `acp` \| `deepseek` \| `openai`. Omit to auto-detect (DeepSeek key → npx/ACP). |
| `--resume`     | `false` | Resume from the last checkpoint for this workflow + workdir. |
| `--log`        | (none)  | JSONL event log path. |

```bash
uv run abe-froman run config.yaml -w ./run --executor deepseek --log run.jsonl
uv run abe-froman run config.yaml --resume
```

### migrate

Rewrite pre-Stage-4 YAML (with `phases:` / `quality_gate:` / `dynamic_subphases:`) to the current schema. Comments, anchors, and `{{}}` strings are preserved. Running on already-migrated YAML is a no-op.

| Flag        | Default | Description |
|-------------|---------|-------------|
| `--dry-run` | `false` | Print rewritten YAML to stdout without touching disk. |
| `--in-place`| `false` | Rewrite the file on disk (default: print to stdout). |

```bash
uv run abe-froman migrate old.yaml --in-place
```

### graph

Render the compiled LangGraph as a Mermaid diagram on stdout.

```bash
uv run abe-froman graph config.yaml
```

## Workflow schema

### Graph

Top-level fields: `name`, `version`, `nodes`, `settings`. Schema validation rejects unknown keys on `Node` (the model declares `extra="forbid"`), so legacy fields surface as a clear error pointing at the unsupported key.

### Settings

All defaults in the table below are pulled from `Settings(BaseModel)` in `src/abe_froman/schema/models.py`.

**Output / retries / timeout / preamble / backoff**

| Field                  | Type            | Default              | Effect |
|------------------------|-----------------|----------------------|--------|
| `output_directory`     | `str`           | `"output"`           | Default base directory for output contracts. |
| `max_retries`          | `int`           | `3`                  | Default retry budget when an evaluation fails. |
| `default_timeout`      | `float \| None` | `None`               | Per-node timeout (seconds). `None` = no timeout. |
| `preamble_file`        | `str \| None`   | `None`               | Prepended to every prompt before Jinja rendering. Missing file is a hard fail. |
| `retry_backoff`        | `list[float]`   | `[]`                 | `asyncio.sleep` seconds before each retry; clamps to last value past list length. |

**Models / backend**

| Field                    | Type           | Default                            | Effect |
|--------------------------|----------------|------------------------------------|--------|
| `default_model`          | `str`          | `"sonnet"`                         | Used when `node.model` and `params.model` are unset. |
| `executor`               | `str \| None`  | `None` (auto-detect)               | `"stub"` \| `"acp"` \| `"deepseek"` \| `"openai"`. |
| `model_downgrade_chain`  | `list[str]`    | `["opus", "sonnet", "haiku"]`      | Tier list for `OverloadError` auto-downgrade. |

**Concurrency**

| Field                | Type              | Default | Effect |
|----------------------|-------------------|---------|--------|
| `max_parallel_jobs`  | `int`             | `4`     | Foreman global semaphore. |
| `per_model_limits`   | `dict[str, int]`  | `{}`    | Per-model caps layered inside the global semaphore. |
| `max_subgraph_depth` | `int`             | `10`    | Cap on recursive subgraph nesting. |

**URL fetch / remote**

| Field                    | Type                      | Default     | Effect |
|--------------------------|---------------------------|-------------|--------|
| `base_url`               | `str \| None`             | `None`      | Default base for relative `execute.url`. |
| `allow_remote_urls`      | `bool`                    | `false`     | Master switch for non-`file://` fetches. |
| `allow_remote_scripts`   | `bool`                    | `false`     | Extra opt-in for remote `.py` / `.js` / `.sh`. |
| `allowed_url_hosts`      | `list[str]`               | `[]`        | fnmatch host allow-list. `[]` = no filter. |
| `url_headers`            | `dict[str, dict[str, str]]` | `{}`      | Prefix → headers; `${VAR}` expands from env at fetch time. |
| `max_remote_fetch_bytes` | `int`                     | `5_000_000` | Body size cap (5 MB). |

### Node

| Field             | Type           | Default | Effect |
|-------------------|----------------|---------|--------|
| `id`              | `str`          | —       | Unique identifier; referenced by `depends_on` and template `{{id}}`. |
| `name`            | `str`          | —       | Human-readable label. |
| `description`     | `str \| None`  | `None`  | Free text. |
| `model`           | `str \| None`  | `None`  | Per-node model override. |
| `depends_on`      | `list[str]`    | `[]`    | Static DAG edges. Cannot reference a `route` node. |
| `timeout`         | `float \| None`| `None`  | Per-node timeout (seconds). Falls back to `settings.default_timeout`. |
| `execute`         | `Execute \| None` | `None` | Execution descriptor (see below). Omit + add `evaluation:` for gate-only-by-elision. |
| `evaluation`      | `Evaluation \| None` | `None` | Quality gate. |
| `output_contract` | `OutputContract \| None` | `None` | Required-files check after execution. |
| `fan_out`         | `FanOut \| None` | `None` | Manifest-driven `Send` fan-out. |

### Execute

Exactly one of `{url, type=join, type=route}` must be set. Validation runs in `Execute.validate_shape` and surfaces typos as a Pydantic `ValidationError`.

URL mode:

```yaml
execute:
  url: "prompts/draft.md"
  params:
    model: "opus"
```

Join sentinel:

```yaml
execute:
  type: join
```

Route ladder:

```yaml
execute:
  type: route
  cases:
    - when: "judge['score'] >= 0.8"
      goto: ship
    - when: "len(history['judge']) >= 3"
      goto: __end__
  else: produce
```

### Per-mode params

Each mode's `params:` is validated by a Pydantic model (`extra="forbid"`); typos like `arg:` instead of `args:` fail at compile time. Source: `src/abe_froman/schema/params.py`.

| Mode                                             | Model              | Fields |
|--------------------------------------------------|--------------------|--------|
| Prompt (`*.md`, `*.txt`, `*.prompt`)             | `PromptParams`     | `model: str?`, `agent: str?`, `timeout: float?` |
| Subgraph (`*.yaml`, `*.yml`)                     | `SubgraphParams`   | `inputs: dict[str,str]`, `outputs: dict[str,str]` |
| Script / binary / unrecognized                   | `SubprocessParams` | `args: list[str]`, `env: dict[str,str]` |

Authors can override the extension-driven choice with `mode:` (one of `prompt`, `subgraph`, `exec`, `python`, `node`, `tsx`, `bash`).

### URL dispatch table

Source: `src/abe_froman/runtime/executor/dispatch.py`.

| URL pattern (scheme + extension)                       | Handler                              | Params shape       |
|--------------------------------------------------------|--------------------------------------|--------------------|
| `*.md` / `*.txt` / `*.prompt` (file://)                | Prompt → PromptBackend                | `PromptParams`     |
| `*.md` (https://, allowed)                             | Prompt → PromptBackend (after fetch)  | `PromptParams`     |
| `*.yaml` / `*.yml` (file://)                           | Subgraph (compile-time)               | `SubgraphParams`   |
| `*.yaml` (https://, allowed)                           | Subgraph after fetch                  | `SubgraphParams`   |
| `*.py` / `*.js` / `*.mjs` / `*.ts` / `*.sh` (file://)  | Script (interpreter dispatch)         | `SubprocessParams` |
| `*.py` etc (https://, with `allow_remote_scripts`)     | deferred — not yet wired              | `SubprocessParams` |
| `/abs/path/to/binary` or extensionless (file://)       | Direct exec                           | `SubprocessParams` |
| `*.yaml` reaching the runtime dispatcher               | error (subgraphs are compile-time)    | —                  |
| `type: join` / `execute: None`                         | no-op                                 | —                  |

### URL resolution

`runtime/url.py::resolve_url(url, base_url, workdir)` applies three rules in order:

1. **Explicit protocol passthrough.** A URL containing `://` is returned canonicalized (lowercase host).
2. **Absolute path → file://.** A URL starting with `/` becomes `file:///abs/path`.
3. **Relative resolves against base.** If `settings.base_url` is set, resolve against it; otherwise against `--workdir`.

| Input `url`           | `base_url`              | `workdir`        | Resolved                            |
|-----------------------|-------------------------|------------------|-------------------------------------|
| `prompts/x.md`        | unset                   | `/home/me/proj`  | `file:///home/me/proj/prompts/x.md` |
| `prompts/x.md`        | `https://prompts/v1/`   | `/home/me/proj`  | `https://prompts/v1/prompts/x.md`   |
| `/etc/scripts/run.sh` | unset                   | `/anywhere`      | `file:///etc/scripts/run.sh`        |
| `/etc/scripts/run.sh` | `https://x.com/v1/`     | `/anywhere`      | `file:///etc/scripts/run.sh`        |
| `https://x.com/y.yaml`| `https://other/v1/`     | `/anywhere`      | `https://x.com/y.yaml`              |

### Remote URL gates

Non-`file://` URLs pass only when **all** gates clear. The defaults reproduce a "local files only" policy.

| Gate                       | Setting                  | Effect |
|----------------------------|--------------------------|--------|
| Master switch              | `allow_remote_urls`      | Must be `true` for any non-`file://` fetch. |
| Host allow-list (fnmatch)  | `allowed_url_hosts`      | Empty list = no filter. |
| Script opt-in              | `allow_remote_scripts`   | Required for `.py` / `.js` / `.sh` / `.ts` / `.mjs` URLs. |
| Body size cap              | `max_remote_fetch_bytes` | Default 5 MB. |
| Headers                    | `url_headers`            | Per-prefix; `${VAR}` expands from env (raises on missing var). |

A per-compile cache keyed by canonical URL ensures the same URL is fetched at most once per `build_workflow_graph` call. There is no on-disk cache.

## Node types in depth

### Prompt nodes

Prompt files (`.md` / `.txt` / `.prompt`) are read, optionally prepended with `settings.preamble_file`, then rendered with Jinja2. Variables visible to the template:

- `{{dep_id}}` — raw output of each dep.
- `{{dep_id_worktree}}` — absolute path to the dep's git worktree (when foreman is active).
- `{{_retry_reason}}` — auto-injected on retry (previous score, threshold, attempt number, gate feedback).

Model resolution order: `params.model` → `node.model` → `settings.default_model`. Hyphenated node IDs in templates (`{{my-id}}`) are parsed by Jinja2 as subtraction and will error — use underscores.

### Script and binary nodes

Script extension dispatch: `.py` → `python3`, `.js` / `.mjs` → `node`, `.ts` → `tsx`, `.sh` → `bash`. Binaries (extensionless or unrecognized) are executed directly. Both paths Jinja-render `params.args` and `params.env` against the dep context, then run via `asyncio.create_subprocess_exec`. Exit code zero is success; non-zero surfaces `stderr` as the error.

```yaml
- id: render_pdf
  depends_on: [paper]
  execute:
    url: /home/me/.local/bin/uv
    params:
      args: ["run", "--script", "scripts/render_pdf.py", "{{paper}}"]
```

### Subgraph nodes (recursive)

A node with `execute: { url: path/to/sub.yaml }` references another graph. The referenced YAML is loaded with the same `Graph` schema and recursively compiled. Identical schemas — the same file is runnable standalone via `abe-froman run` or as a subgraph reference.

```yaml
- id: paper
  depends_on: [discussion, abstract]
  execute:
    url: subgraphs/compose_and_validate.yaml
    params:
      inputs:
        abstract: "{{abstract}}"
        discussion: "{{discussion}}"
      outputs:
        check_result: "{{submission_check}}"
```

`params.inputs` projects parent context into the subgraph's `node_inputs` channel — subgraph nodes see them as plain template variables. `params.outputs` exposes named subgraph node outputs as `node_outputs[parent_id.key]` in the parent. Default (empty `outputs:`) projects the subgraph's terminal-node output as `node_outputs[parent_id]`. Compile-time guards: cycle detection over the URL-reference DAG and `settings.max_subgraph_depth`.

### Join

A no-op topology marker for fan-in. Carries no params, no cases.

```yaml
- id: gather
  depends_on: [a, b, c]
  execute:
    type: join
```

### Route

A pure case ladder over structured state. Each `when:` is evaluated in order against a sandboxed namespace (via `simpleeval`); the first match dispatches via `Command(goto=…)`. The `else:` is required.

Namespace bound to predicates:

- Each dep's structured output (or raw output) by id.
- `history` — full `state.evaluations` map.
- `state` — full state dict.
- Safe functions: `len`, `any`, `all`, `min`, `max`, `sum`.

`__end__` halts the workflow (maps to LangGraph `END`). All other `goto` values must resolve to a real node id; the schema validator rejects unknown targets at compile time. Routes are **leaves in the depends_on DAG** — a node cannot `depends_on:` a route. Goto targets skip the START fallback edge.

Workflow YAML is treated as author-checked-in code, so the sandbox is footgun prevention, not adversarial isolation.

### Fan-out

`fan_out.enabled: true` plus a manifest produces one `Send` per item. The manifest is read from the parent node's JSON output, falling back to `manifest_path` on disk. Each Send runs the `template.execute` (and optionally `template.evaluation`); the gate retry loop runs inline. After all Sends complete, `final_nodes` consume aggregated `child_outputs[parent::item_id]`.

When `template.execute.url` ends in `.yaml`, each Send runs a per-child subgraph instead of a single executor call. See `examples/absurd-paper/workflow.yaml` for `reviewer_pool` — a draft → critique 2-node subgraph per reviewer.

### Gate-only by elision

A node with `evaluation:` and no `execute:` block runs the gate against an empty output. Useful for synthetic checkpoints between phases.

## Evaluation

```yaml
evaluation:
  validator: "gates/check.py"   # .py | .js | .md
  threshold: 0.8                 # default 0.0
  blocking: false                # default false
  max_retries: 3                 # falls back to settings.max_retries
  model: "opus"                  # only used by .md LLM gates
  dimensions:                    # multi-dim DimensionCheck
    - {field: rigor, min: 0.7}
```

**Script validators (`.py`, `.js`)** read the node output on stdin and print to stdout. Environment: `NODE_ID`, `WORKFLOW_NAME`, `ATTEMPT_NUMBER`, `WORKDIR`. Output is accepted in three shapes:

| Shape                    | Example                                                 | Effect |
|--------------------------|---------------------------------------------------------|--------|
| Bare float               | `0.85`                                                  | `score=0.85`, no feedback. |
| JSON `{score}`           | `{"score": 0.6}`                                        | `score=0.6`, no feedback. |
| Full feedback JSON       | `{"score": 0.6, "feedback": "...", "pass_criteria_met": [...], "pass_criteria_unmet": [...]}` | All fields flow into `{{_retry_reason}}`. |

**LLM validators (`.md`)** are rendered as Jinja2 templates with `{{output}}`, `{{phase_id}}`, `{{attempt}}` available, then dispatched through the node's `PromptBackend`. The model response must be JSON with at least a `score` field; the full feedback schema above is supported. Malformed output fails loudly (`score=0.0` plus a diagnostic feedback string) — it does not silently pass.

**Routing** (`runtime/gates.py::classify_gate_outcome`):

- `score >= threshold` → pass; continue to dependents.
- `score < threshold`, retries left → retry; re-execute node.
- `score < threshold`, retries exhausted, `blocking: true` → fail; dependents skipped.
- `score < threshold`, retries exhausted, `blocking: false` → pass with warning; dependents continue.

## Foreman and worktrees

Foreman is enabled when `--workdir` is inside a git working tree. It allocates a worktree per node id at `<workdir>/.abe-foreman/wt-<id>-<uuid>/`, reused across retries so prompt nodes can iterate on prior files. Subphases get worktrees keyed `{parent_id}::{item_id}`. Worktrees survive resume — `state.node_worktrees` rehydrates into a fresh `ForemanExecutor`.

Foreman never cleans worktrees. Authors write explicit reconciliation nodes (typically a `cp` / `git merge-file` script) that decide what flows from a worktree into the base workdir. Stray trees can be removed with `git worktree remove <path>`.

When `--workdir` is not a git repo, the CLI prints a notice and falls back to `DispatchExecutor` directly (no foreman, no worktree isolation).

Concurrency caps: `settings.max_parallel_jobs` (global semaphore) and `settings.per_model_limits` (per-model caps inside the global one).

## Observability

`--log <path>` writes a JSONL event stream. Event types: `workflow_start`, `workflow_end`, `node_completed`, `node_failed`, `gate_evaluated`, `node_retried`. Subgraph events are prefixed `parent::child` so nested compositions are traceable.

`<workdir>/.abe-froman-checkpoint.db` is the LangGraph `AsyncSqliteSaver` store. The thread_id is a deterministic 16-char SHA1 hash of `(workflow_name, resolved_workdir)`. `--resume` reads the most recent checkpoint for that thread, strips failure bookkeeping (`failed_nodes`, `errors`, `retries`), and re-runs from where the previous attempt stopped.

`abe-froman graph config.yaml` emits a Mermaid diagram via LangGraph's `draw_mermaid()`.

## Examples gallery

| Path                                       | What it shows |
|--------------------------------------------|---------------|
| `examples/jokes/workflow.yaml`             | Minimal: prompt + script gate + select. Best to start here. |
| `examples/smoke_test.yaml`                 | Bare-minimum config — single prompt node. |
| `examples/explicit_join.yaml`              | `type: join` topology marker. |
| `examples/route_classify/workflow.yaml`    | `type: route` case ladder over structured state. |
| `examples/absurd-paper/workflow.yaml`      | 13-node multi-stage pipeline with subgraphs and per-Send subgraph fan-out (`reviewer_pool`). |
| `examples/run_all_examples.yaml`           | Wrapper that exercises the full set in CI. |

## Contributing

Architecture details, layer rules, and key invariants live in `TECHNICAL.md`. Operator notes for Claude Code (project layout, testing principles, environment quirks) live in `CLAUDE.md`. Design history and superseded decisions live in `DECISIONS.md` and `docs/plans/`. The three-layer split (`schema/` → `compile/` → `runtime/`) is enforced at CI time by `tests/architecture/test_layers.py` walking imports via AST.

Test invariants:

- No mocks of external systems. Real subprocess for command nodes, real `git worktree add` for foreman tests, real `AsyncSqliteSaver` for resume tests, real `claude-code-acp` for ACP tests.
- No `PromptBackend` mocks. `MockExecutor` is a custom test double implementing the `PhaseExecutor` Protocol — not `unittest.mock`.
- Tests assert concrete output values, not just absence of exceptions.
- ACP tests require `@zed-industries/claude-code-acp` installed globally.

Run the suite (754 tests, ~35s without ACP):

```bash
uv run pytest tests/ --ignore=tests/acp
uv run pytest tests/ -v   # full suite incl. ACP integration
```

## License

License: TBD (no LICENSE file present).
