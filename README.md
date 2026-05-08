# abe-froman

Workflow orchestrator using LangGraph for graph topology and Claude / DeepSeek / scripts for execution.

## What it is

abe-froman compiles a YAML workflow into a LangGraph `StateGraph`. Each node executes one of: a prompt against an LLM backend, an interpreted script (`.py` / `.js` / `.ts` / `.sh`), a binary, a `join` topology marker, or a recursive subgraph reference. The shape of every executable node is the unified Stage 5b `execute: { url, params }` block — the URL extension (or `mode:` override) drives the dispatcher. Forward-edge dispatch lives on the orthogonal Stage 5c `route:` block, which can stand alone (a pure dispatcher) or pair with `execute:` on the same node.

Quality gates wrap each node. A gate is a script (`.py` / `.js`) reading the node output on stdin, or an `.md` Jinja prompt evaluated by the node's LLM backend. Gate failures retry the node with `{{_retry_reason}}` injected. Subgraphs are recursive — the same YAML schema is runnable standalone via `abe-froman run` or as a node reference from another graph. Fan-out spawns a `Send` per manifest item, optionally backed by a per-Send subgraph.

State persists through LangGraph's `AsyncSqliteSaver` checkpointer at `<workdir>/.abe-froman-checkpoint.db`, keyed by a deterministic SHA1 of `(workflow_name, resolved_workdir)`. `--resume` reads the last checkpoint and restarts from where the previous run stopped. When `--workdir` is a git repo, every node runs in its own git worktree under `<workdir>/.abe-foreman/`, retained across retries so prompt nodes can iterate on prior files.

## Install

```bash
uv sync                                  # core
uv sync --extra openai                   # for OpenAI / DeepSeek backend
npm i -g @zed-industries/claude-code-acp # for ACP backend
```

Python 3.11+ is required. The project develops on 3.14.

### API keys

Copy `.env.example` to `.env` (gitignored) and uncomment any backend you plan to use:

```bash
ANTHROPIC_API_KEY=sk-ant-...        # --executor anthropic; auto-detect picks this first
DEEPSEEK_API_KEY=sk-...             # --executor deepseek
OPENAI_API_KEY=sk-...               # --executor openai (reserved for real openai.com)
CUSTOM_API_KEY=sk-or-v1-...         # --executor custom (any OpenAI-compatible third party)
CUSTOM_API_BASE_URL=https://openrouter.ai/api/v1
```

Load via `uv run --env-file .env abe-froman run <config.yaml>`, or `set -a; source .env; set +a` in your shell.

Resolution order: workflow YAML setting (when a binding exists) → process env (`os.environ`) → project-local `.env` file (auto-discovered by walking up from CWD). abe-froman never reads from machine-global keystores; keys live in the project's environment.

**`openai` vs `custom`**: `--executor openai` is reserved for real openai.com. For OpenAI-compatible third parties — OpenRouter, Ollama, LM Studio, LiteLLM, Azure OpenAI, vLLM, etc. — use `--executor custom` with `CUSTOM_API_KEY` and `CUSTOM_API_BASE_URL`. Both vars are required for `custom` (no silent fallback to OpenAI's default endpoint with a non-OpenAI key). Example endpoints: `https://openrouter.ai/api/v1` (OpenRouter), `http://localhost:11434/v1` (Ollama), `http://localhost:1234/v1` (LM Studio), `http://localhost:4000` (LiteLLM proxy).

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

1. `ANTHROPIC_API_KEY` (env or `.env`) → `anthropic`
2. `DEEPSEEK_API_KEY` (env or `.env`) → `deepseek`
3. `npx` on `PATH` → `acp`
4. Nothing → raise `RuntimeError` with a clear remediation message naming each env var and install command. There is no longer a silent stub fallback.

| Key         | Install                                    | Auth                                                    | Models                  | Cost                |
|-------------|--------------------------------------------|---------------------------------------------------------|-------------------------|---------------------|
| `acp`       | `npm i -g @zed-industries/claude-code-acp` | Claude CLI session (no API key needed)                  | Claude (opus/sonnet/haiku) | usage-billed via Anthropic |
| `anthropic` | `uv sync --extra anthropic`                | `ANTHROPIC_API_KEY` (env or `.env`)                     | Claude (opus/sonnet/haiku via the Messages API; full vendor IDs accepted) | usage-billed via Anthropic |
| `deepseek`  | `uv sync --extra openai`                   | `DEEPSEEK_API_KEY` (env or `.env`)                      | DeepSeek catalog (e.g. `deepseek-chat`, `deepseek-reasoner`, `deepseek-v4-flash`) | usage-billed via DeepSeek |
| `openai`    | `uv sync --extra openai`                   | `OPENAI_API_KEY` (env or `.env`); reserved for real openai.com | OpenAI catalog | usage-billed via OpenAI |
| `custom`    | `uv sync --extra openai`                   | `CUSTOM_API_KEY` + `CUSTOM_API_BASE_URL` (env or `.env`) | Whatever the configured endpoint serves — OpenRouter, Ollama, LM Studio, LiteLLM, Azure OpenAI, vLLM, etc. | usage-billed by the provider |

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
| `--executor, -e` | (auto) | `acp` \| `anthropic` \| `deepseek` \| `openai`. Omit to auto-detect (Anthropic key → DeepSeek key → npx/ACP; raises if none). |
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

### view

Render a workflow as a self-contained interactive HTML page. Open
the output in a browser — no server, no auth.

Two modes:

```bash
# Authoring: topology + per-node config inspector. No log needed.
uv run abe-froman view config.yaml

# Debug: same, plus per-node status overlay + log slices on click.
uv run abe-froman view config.yaml --log out.jsonl
```

Flags:
- `--out <path.html>` — output path (default `<workdir>/abe-froman-view.html`).
- `--direction TB|LR|BT|RL` — Mermaid layout direction (default `TB`).
- `--workdir <path>` — for resolving the default `--out` path.

The Mermaid output is generated directly from the schema, not from
LangGraph's compile-time topology, so synthetic `_eval_<id>` /
`_route_<id>` nodes are hidden — authors see what they wrote. Node
shapes encode type: rectangle for plain execute, hexagon for fan-out
parents, diamond for route-only nodes, subroutine for subgraph
references. Nodes with `evaluation:` blocks get a colored stroke.

Mermaid loads via CDN; if blocked, the page falls back to displaying
the raw Mermaid source. Opens cleanly without internet on subsequent
loads (browser cache).

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
| `executor`               | `str \| None`  | `None` (auto-detect)               | `"acp"` \| `"anthropic"` \| `"deepseek"` \| `"openai"`. |
| `model_downgrade_chain`  | `list[str]`    | `["opus", "sonnet", "haiku"]`      | Tier list for `OverloadError` auto-downgrade. |

**Concurrency**

| Field                | Type              | Default | Effect |
|----------------------|-------------------|---------|--------|
| `max_parallel_jobs`           | `int`               | `4`     | Foreman global semaphore. |
| `per_model_limits`            | `dict[str, int]`    | `{}`    | Per-model caps layered inside the global semaphore. |
| `memory_threshold_pct`        | `float \| None`     | `None`  | Foreman blocks new dispatches while host memory percent (`psutil.virtual_memory().percent`) is above this threshold. `None` disables. |
| `memory_min_available_bytes`  | `int \| str \| None`| `None`  | Foreman blocks new dispatches while available memory is below this threshold. Accepts raw bytes (`4_294_967_296`) or a string with a binary-multiplier suffix (`"4GB"`, `"500MiB"`, `"2T"`). Case-insensitive; `KB = 1024` (binary semantics, matches `free -h`). `None` disables. |
| `max_subgraph_depth`          | `int`               | `10`    | Cap on recursive subgraph nesting. |

Both memory gates compose (AND) with each other and with the semaphores — every gate must allow dispatch. In-flight jobs are never aborted by the gates; only new acquisitions wait.

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
| `depends_on`      | `list[str]`    | `[]`    | Static DAG edges. Cannot reference an inline-route node (validated at compile time). |
| `timeout`         | `float \| None`| `None`  | Per-node timeout (seconds). Falls back to `settings.default_timeout`. |
| `execute`         | `Execute \| None` | `None` | Execution descriptor (see below). Omit + add `evaluation:` for gate-only-by-elision; omit + add `route:` for a standalone router. |
| `evaluation`      | `Evaluation \| None` | `None` | Quality gate. |
| `output_contract` | `OutputContract \| None` | `None` | Required-files check after execution. |
| `fan_out`         | `FanOut \| None` | `None` | Manifest-driven `Send` fan-out. |
| `route`           | `Route \| None` | `None` | Stage 5c inline routing block. `goto:` shorthand or `cases:`/`else:` ladder; coexists with `execute:` (synthetic post-execute dispatcher) or stands alone (pure router). See "Inline routing" below. |

### Execute

Exactly one of `{url, type=join}` must be set. Validation runs in `Execute.validate_shape` and surfaces typos as a Pydantic `ValidationError`. Forward-edge dispatch (`cases:` / `goto:` / `else:`) lives on the orthogonal `Node.route` block — see "Inline routing" below.

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

### Inline routing

Stage 5c. `Node.route` is a first-class forward-edge dispatcher: every node can declare where flow goes next, in addition to (or instead of) what it executes. Two shapes:

**Goto shorthand** — unconditional dispatch:

```yaml
- id: research
  execute:
    url: prompts/research.md
  route:
    goto: write             # str → Command(goto="write")
```

```yaml
- id: research
  execute:
    url: prompts/research.md
  route:
    goto: [draft_a, draft_b]   # list → Command(goto=[...])
                                # static fan-out via LangGraph 1.x native multi-edge
```

**Conditional ladder** — first-match-wins predicate:

```yaml
- id: decide
  depends_on: [judge]
  route:
    cases:
      - when: "passed('judge') and score('judge') >= 0.8"
        goto: ship
      - when: "len(history['judge']) >= 3"
        goto: __end__
    else: produce
```

`else:` accepts a bare string/list (auto-promoted) or a structured `{goto, include_eval}` for `include_eval` control.

**`include_eval`** opts the goto target into receiving the same neutral eval-result preamble that retry attempts get. Default `false` — success paths typically perform a different task than the previous node, so the previous eval's feedback is noise unless explicitly wanted. Set per-case (or on the goto-shorthand) to opt in:

```yaml
route:
  cases:
    - when: "score('judge') >= 0.8"
      goto: ship
      # success path — no preamble
    - when: "len(history['judge']) >= 3"
      goto: escalate
      include_eval: true
      # escalation path — carry forward the eval preamble
  else:
    goto: produce
    include_eval: true
```

When `include_eval: true` fires, the goto target's prompt gets the eval preamble auto-prepended — no template syntax needed. This sits ahead of the rendered template body as a system-style block.

**Standalone vs synthetic dispatch.** A node with `route:` and no `execute:` is a standalone router (the form previously written as `execute: { type: route, cases, else }`). A node with both `execute:` and `route:` runs the execute body (and eval, if present), then a synthetic `_route_<id>` dispatcher fires post-eval and resolves the route. Old YAML using `execute: { type: route, ... }` is auto-migrated to inline `route:` by `abe-froman migrate` (idempotent).

**LangGraph forms produced.** A scalar `goto:` compiles to `Command(goto="target")`; a list `goto: [a, b]` compiles to `Command(goto=["a", "b"])` — LangGraph 1.x dispatches each target as its own concurrent edge in the next super-step.

**`__end__`** halts the workflow (maps to LangGraph `END`). All other goto values must resolve to a real node id; the schema validator rejects unknown targets at compile time. Inline-route nodes are leaves in the depends_on DAG — a node cannot `depends_on:` a node with `route:` (would double-trigger via Command + plain edge).

**Route namespace** for case predicates (built by `compile/route.py::build_route_namespace` + `build_safe_funcs`):

| Name | Type | Effect |
|------|------|--------|
| `<dep_id>` | structured-or-raw | Each dep's `node_structured_outputs[dep]` if present, else raw output. |
| `history` | `dict[str, list[dict]]` | Full `state.evaluations` map. |
| `state` | `dict` | Full state dict. |
| `evals` | `dict[str, dict]` | `evals[node_id]` → latest eval result dict (`{score, scores, reasons, feedback, ...}`) or `{}` if no eval has run. |
| `passed(id)` | `bool` | "Settled cleanly": in `completed_nodes`, not in `failed_nodes`. |
| `score(id)` | `float` | Latest top-level score for that node (0.0 if absent). |
| `scores(id)` | `dict[str, float]` | Latest per-dimension scores. |
| `len`, `any`, `all`, `min`, `max`, `sum` | builtins | Safe functions. |

Workflow YAML is treated as author-checked-in code, so the simpleeval sandbox is footgun prevention (no dunders, no statements, no imports), not adversarial isolation.

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
| Standalone inline route (`Node.route` set, no `execute:`) | compile-time `Command(goto=...)`   | —                  |

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

Prompt files (`.md` / `.txt` / `.prompt`) are read, optionally prepended with `settings.preamble_file`, then rendered with full Jinja2 (`{{var}}`, `{% if %}`, `{% for %}`, filters — anything Jinja2 supports). Variables bound to the template context:

- `{{dep_id}}` — raw output of each dep.
- `{{dep_id_structured}}` — parsed structured output of each dep, when present.
- `{{dep_id_worktree}}` — absolute path to the dep's git worktree (when foreman is active).
- `{{_retry_reason}}` — auto-injected on retry (previous score, threshold, attempt number, gate feedback). Author-referenced — put `{{_retry_reason}}` in the template body where the preamble should appear.
- `{{evals}}` — always-on global. `evals[node_id]` returns the latest eval result dict for that node (`{score, scores, reasons, feedback, ...}`) or `{}` if no eval has run. Use as a backstop for cross-cutting eval reads; e.g. `{{evals.classify.score}}` works without declaring `classify` as a dep.
- **Inline-route goto target** (`route:` dispatched into this node): `{{sender_id}}` (str — source node id), `{{sender}}` (raw output of the source), `{{sender_structured}}` (parsed output if available), `{{sender_worktree}}` (path if foreman is active). Eval feedback for `include_eval: true` flows via the auto-prepended preamble, NOT via these Jinja vars — see "Evaluation" below.
- Inside a fan-out child: every key from the manifest item, plus `{{<parent_id>}}` for the parent's output (see Fan-out below).
- Inside a subgraph: whatever `params.inputs` projects in.

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

Stage 5c. Inline forward-edge dispatch lives on `Node.route`; full schema reference is in the "Inline routing" section under "Workflow schema" above. Two compile-time shapes:

- **Standalone** — `route:` with no `execute:`. The node body itself is the dispatcher; emits `Command(goto=...)` directly. Replaces the legacy `execute: { type: route, ... }` form (auto-migrated by `abe-froman migrate`).
- **Synthetic post-execute** — `execute:` and `route:` on the same node. After the execute body (and eval, if any) settles, a synthetic `_route_<id>` node fires, resolves the route, and dispatches via `Command(goto=...)`.

Both forms support `goto: <str>`, `goto: [list]` (static fan-out), and `cases:` + `else:` ladders. Goto targets skip the START fallback edge so they only fire via `Command`. Inline-route nodes are leaves in the `depends_on` DAG (validated at compile time).

The route's `Command` payload also threads `_route_sender` (source node id), `_route_include_eval` (bool), and `_route_eval_preamble` (pre-built string when `include_eval: true`) into state — see "Sender bindings" and "Evaluation" below for how the goto target reads them.

### Fan-out

A `fan_out:` block on a node activates manifest-driven `Send` fan-out — its presence IS the activation; there is no separate enable flag. The manifest is read from the parent node's JSON output, falling back to `manifest_path` on disk. Each Send runs the `template.execute` (and optionally `template.evaluation`); the gate retry loop runs inline. After all Sends complete, `final_nodes` consume aggregated `child_outputs[parent::item_id]`. To disable fan-out, remove the `fan_out:` block.

Each Send branch renders its prompt / script args against a per-item Jinja2 context:

- Dep outputs by node id (same as a top-level node).
- `{{<parent_id>}}` — the parent fan-out node's raw output (so a child can reference the manifest-producing prompt's output verbatim).
- Every key on the manifest item — `{"id": "alpha", "topic": "cats"}` exposes `{{id}}` and `{{topic}}` to that branch's template only.

So a fan-out template at `templates/per_item.md` that says `Write about {{topic}} in a {{tone}} voice, building on {{generate}}` will render once per manifest item with that item's `topic` / `tone` and the parent `generate` node's output. Built by `compile/dynamic.py::node_fn` (lines 133–139).

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

### Per-dimension `<dim>_reason` capture

Multi-dim gates may include a `<dim>_reason` string field per dimension in the JSON response. The parser captures these into `EvaluationResult.reasons: dict[str, str]` (keyed by the dimension name with the `_reason` suffix stripped). Non-string values for `*_reason` keys are dropped silently — the field is reserved for rationale text. Example gate output:

```json
{
  "rigor": 0.8,
  "rigor_reason": "citations cover the claim space",
  "novelty": 0.4,
  "novelty_reason": "argument restates prior work"
}
```

`reasons` flow through `state.evaluations[node_id][-1].result.reasons` and are surfaced in the eval preamble (see below).

### Eval preamble (neutral structural format)

`runtime/gates.py::build_eval_preamble` formats an `EvaluationResult` as a **neutral** preamble block — no "failed" / "fail" / "failure" framing. A `blocking: false` settled score below threshold is not a failure, and a goto target opting in via `include_eval: true` may receive contextual non-failure information (e.g. a passing score with feedback worth carrying forward). The preamble carries the previous score(s), per-dimension thresholds + reasons, top-level `feedback`, and met/unmet criteria. For retry attempts it appends `Attempt N of M.`; for goto targets the footer is omitted.

Two call sites use the same builder:

| Call site | Mechanism | Author surface |
|---|---|---|
| Same-node retry | `compile/nodes.py::inject_retry_reason` populates `context["_retry_reason"]` on retry | Author-referenced: put `{{_retry_reason}}` in the template body |
| Inline route goto + `include_eval: true` | Synthetic `_route_<id>` builds the string into `state._route_eval_preamble`; `_dispatch_prompt` auto-prepends it to the rendered body | No template syntax — preamble appears as a system-style block above the authored content |

Asymmetric by design: retry is "iterate on this task" (the author writes `{{_retry_reason}}` into the same prompt); goto is "perform a new task with carried-over context" (no template integration — system-style preamble before authored content).

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
| `examples/route_classify/workflow.yaml`    | Inline `route:` case ladder over structured state (Stage 5c). |
| `examples/pipeline_style/workflow.yaml`    | Inline `route: { goto: <next> }` forward-edge authoring; 3-node linear chain reading top-down like a pipeline. |
| `examples/absurd-paper/workflow.yaml`      | 13-node multi-stage pipeline with subgraphs and per-Send subgraph fan-out (`reviewer_pool`). |
| `examples/wave_planner/workflow.yaml`      | Wave-driven dynamic-task pattern — `goto:` loop-back to a fan_out parent that re-reads its manifest each wave. Demonstrates `memory_threshold_pct`. Requires non-git workdir; see the example's README. |
| `examples/run_all_examples.yaml`           | Wrapper that exercises the full set in CI (excludes wave_planner — that example needs a non-git workdir, incompatible with the parent's worktree-isolated subgraph mode). |

## Contributing

Architecture details, layer rules, and key invariants live in `TECHNICAL.md`. Operator notes for Claude Code (project layout, testing principles, environment quirks) live in `CLAUDE.md`. Design history and superseded decisions live in `DECISIONS.md` and `docs/plans/`. The three-layer split (`schema/` → `compile/` → `runtime/`) is enforced at CI time by `tests/architecture/test_layers.py` walking imports via AST.

Test invariants:

- No mocks of external systems. Real subprocess for command nodes, real `git worktree add` for foreman tests, real `AsyncSqliteSaver` for resume tests, real `claude-code-acp` for ACP tests.
- No `PromptBackend` mocks. `MockExecutor` is a custom test double implementing the `NodeExecutor` Protocol — not `unittest.mock`.
- Tests assert concrete output values, not just absence of exceptions.
- ACP tests require `@zed-industries/claude-code-acp` installed globally.

Run the suite (834 tests, ~55s without ACP):

```bash
uv run pytest tests/ --ignore=tests/acp
uv run pytest tests/ -v   # full suite incl. ACP integration
uv run pytest tests/ -m live   # only the live-backend round-trip tests
uv run pytest tests/ -m "not live"   # skip everything that needs an API key
```

Live-backend tests (`tests/e2e/test_live_backend_roundtrip.py`,
`tests/unit/runtime/test_anthropic_backend.py::TestAnthropicLive`,
`tests/unit/runtime/test_openai_backend.py::TestDeepSeekLive`)
self-skip per-key when the matching API key is absent on disk.
Configure keys in `.env` (see `.env.example`) to opt into live
coverage for any subset of {Anthropic, DeepSeek, real OpenAI,
OpenAI-compatible custom endpoints}.

## License

License: TBD (no LICENSE file present).
