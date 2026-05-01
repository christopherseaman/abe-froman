# Abe Froman

Workflow orchestrator using LangGraph for orchestration and Claude via ACP for execution.

## Architecture

```
YAML Config → Pydantic Schema → LangGraph StateGraph (+ SqliteSaver checkpointer)
                                       │
                                       ▼
                             Phase Node → ForemanExecutor → DispatchExecutor
                                   │         (queue +            │
                                   │          worktree           │
                                   │          pool)        ┌─────┼──────┐
                                   │                       ▼     ▼      ▼
                                   │                     Prompt  Cmd  GateOnly
                                   ▼
                             Quality Gate (script or LLM)
                                   │
                                 ┌─┼─┐
                                 ▼ ▼ ▼
                              pass retry fail   (router reads state; no reclassify)
```

## Build & Test

```bash
uv sync                                    # install deps
uv run pytest tests/ -v                    # run all tests (~688 tests)
uv run abe-froman validate config.yaml     # validate a workflow config
uv run abe-froman run config.yaml --dry-run # dry run
uv run abe-froman run config.yaml -e acp   # run with Claude via ACP
uv run abe-froman run config.yaml --resume        # resume from last checkpoint
uv run abe-froman run config.yaml --log out.jsonl  # run with JSONL event log
uv run abe-froman graph config.yaml        # emit Mermaid graph (LangGraph draw_mermaid)
uv run abe-froman migrate old.yaml --in-place  # rewrite pre-Stage-4 YAML to current schema
```

## Workflow Schema

```yaml
name: "Workflow Name"         # required
version: "1.0.0"             # required

nodes:                        # required, list of nodes (Stage 4: phases→nodes)
  - id: my-node               # required, unique identifier
    name: "Human Name"        # required
    description: "..."        # optional
    model: "opus"             # optional, overrides settings.default_model
    depends_on: ["other-id"]  # optional, list of node IDs this depends on
    timeout: 30.0             # optional, seconds, overrides settings.default_timeout

    # Execution (Stage 5b unified shape) — set EITHER `execute:` OR omit
    # entirely (gate-only-by-elision: a node with `evaluation:` and no
    # `execute:` block runs the gate against an empty output).
    execute:
      url: "path/to/prompt.md"         # see "Execute URLs" below for shapes
      params: { ... }                  # per-mode params (see "Execute URLs")
    # OR a join sentinel:
    execute:
      type: join                       # no-op topology marker for fan-in
    # OR a route ladder:
    execute:
      type: route
      cases:
        - { when: "judge['score'] >= 0.8", goto: ship }
      else: produce

    # Evaluation — optional (renamed from quality_gate in Stage 4a)
    evaluation:
      validator: "gates/check.py"     # .py, .js, or .md (LLM gate)
      threshold: 0.8                  # 0.0–1.0, evaluation passes when score >= threshold
      blocking: false                 # true = failure stops workflow, false = warn and continue
      max_retries: 3                  # optional, overrides settings.max_retries
      model: "opus"                   # optional, only used by .md LLM gates

    # Output — optional
    output_contract:
      base_directory: "output/dir"
      required_files: ["file1.md"]

    # Fan-out — optional (renamed from dynamic_subphases in Stage 4a)
    fan_out:
      enabled: true
      manifest_path: "manifest.json"
      template:
        execute: { url: "template.md" }   # OR url: subgraphs/single.yaml
        evaluation: { ... }
      final_nodes:
        - { id, name, execute: { url: "final.md" }, ... }

settings:                      # optional, all fields have defaults
  output_directory: "output"   # default: "output"
  max_retries: 3               # default: 3
  default_model: "sonnet"      # default: "sonnet"
  executor: "stub"             # default: "stub", options: "stub", "acp"
  default_timeout: 300         # optional, seconds, None = no timeout
  preamble_file: "preamble.md" # optional, prepended to all prompt nodes
  retry_backoff: [10, 30, 60]  # optional, delay seconds per retry attempt
  max_parallel_jobs: 4         # foreman global concurrency cap (default: 4)
  max_subgraph_depth: 10       # cap on recursive subgraph nesting (default: 10)
  per_model_limits:            # foreman per-model concurrency caps (default: {})
    opus: 2
    sonnet: 4
  # Stage 5b — execute.url remote URL gates
  base_url: "https://prompts.example.com/v1/"  # default base for relative urls
  allow_remote_urls: false      # master switch for non-file:// fetches (default false)
  allow_remote_scripts: false   # extra opt-in for remote .py/.js/.sh (default false)
  allowed_url_hosts: []         # fnmatch host patterns; [] = no filter
  url_headers:                  # prefix → headers; ${VAR} expands from env
    "https://prompts.example.com/":
      Authorization: "Bearer ${PROMPTS_TOKEN}"
  max_remote_fetch_bytes: 5000000   # 5 MB cap (default)
```

### Execute URLs (Stage 5b)

The `execute:` block has three modes; **exactly one** must be set:

1. **URL mode** — `url:` set, no `type:`. The URL extension and scheme
   pick the dispatch handler.
2. **Join sentinel** — `type: join`, no `url:`, no `params:`, no `cases:`.
   No-op topology marker for fan-in.
3. **Route ladder** — `type: route`, `cases:`, `else:`. Pure
   `Command(goto=…)` dispatch over structured state. See "Route nodes".

URL mode dispatch table:

| URL pattern | Handler | Params shape (`PromptParams` / `SubgraphParams` / `ScriptParams` / `ExecParams`) |
|---|---|---|
| `*.md` / `*.txt` / `*.prompt` (file://) | Prompt → PromptBackend | `model?`, `agent?`, `timeout?` |
| `*.md` (https://, allowed) | Prompt → PromptBackend (after fetch) | same |
| `*.yaml` / `*.yml` (file://) | Subgraph (recursive compile) | `inputs?: dict`, `outputs?: dict` |
| `*.yaml` (https://, allowed) | Subgraph after fetch | same |
| `*.py` / `*.js` / `*.mjs` / `*.ts` / `*.sh` (file://) | Script (interpreter dispatched) | `args?: list[str]`, `env?: dict[str, str]` |
| `*.py` etc (https://, with `allow_remote_scripts`) | (deferred — not yet wired) | — |
| `/abs/path/to/binary` (file://) | Direct exec | `args?: list[str]`, `env?: dict[str, str]` |
| extensionless / unrecognized (file://) | Direct exec | same |
| anything ending in `.yaml` / `.yml` reaching the runtime dispatcher | error (subgraph wiring is compile-time) | — |
| `*.yaml` reached without a compile-fn (e.g. nested fan-out without plumbing) | error | — |

Per-mode params are validated by Pydantic (`extra="forbid"`) — typos
like `args:` on a prompt URL surface as a clear ValidationError at
schema parse time, not runtime.

### URL resolution

`runtime/url.py::resolve_url(url, base_url, workdir)` applies three
rules in order:

1. **Explicit protocol passthrough.** If `url` already starts with
   `<scheme>://` (e.g. `file://`, `https://`), it's returned as-is
   (with canonicalization: lowercase host, no double-slash variance).
2. **Absolute path → file://.** A `url` starting with `/` is wrapped as
   `file:///path/to/x`.
3. **Relative resolves against base.** If `settings.base_url` is set,
   the URL resolves against it; otherwise against `--workdir`.

| Input `url` | `base_url` | `workdir` | Resolved |
|---|---|---|---|
| `prompt.md` | unset | `/work` | `file:///work/prompt.md` |
| `prompt.md` | `https://x/v1/` | `/work` | `https://x/v1/prompt.md` |
| `/abs/x.md` | unset | `/work` | `file:///abs/x.md` |
| `https://x/p.md` | unset | `/work` | `https://x/p.md` (canonicalized) |
| `file:///abs/y.md` | `https://x/` | `/work` | `file:///abs/y.md` (passthrough) |

### Remote URL support (Stage 5b)

Non-`file://` URLs are blocked unless **all** of the following gates
pass. The defaults reproduce a "local files only" policy.

| Gate | Setting | Effect |
|---|---|---|
| 1 | `allow_remote_urls: true` | Master switch — must be true for any non-`file://` fetch. |
| 2 | `allowed_url_hosts: ["*.internal.example.com"]` | fnmatch host allow-list. Empty list = no host filter. |
| 3 | `allow_remote_scripts: true` | Extra opt-in for `.py`/`.js`/`.sh` URLs (defense-in-depth). |
| 4 | `max_remote_fetch_bytes: 5_000_000` | Cap on response body size. |
| — | `url_headers` | Per-prefix headers; `${VAR}` references env at fetch time. |

A per-compile cache (`_RemoteFetchCache`) ensures the same URL is
fetched at most once per `build_workflow_graph` call. Cache is
keyed by canonical URL form. There is no on-disk cache.

`RemoteURLBlockedError` and `RemoteURLFetchError` surface as node
failures with a clear error message — not silent fallbacks.

### Route nodes (Stage 5a / 5b shape)

A `route` node is a pure case ladder over structured state. It carries
**zero baked-in semantics** (no retry, no halt, no delay): each `when:`
expression is evaluated in order against a sandboxed namespace, and the
first match dispatches via `Command(goto=<node_id>)`. The `else:` branch
is required as a catch-all.

```yaml
- id: decide
  depends_on: [judge]
  execute:
    type: route
    cases:
      - when: "history['judge'][-1]['result']['score'] >= 0.8"
        goto: ship
      - when: "len(history['judge']) >= 3"
        goto: __end__
    else: produce
- id: ship
  execute:
    url: /usr/bin/echo
    params:
      args: ["shipped"]
```

**Namespace bound to `when:` expressions:**

- Each dep's structured output (or raw output if no structured) by id
- `history` — the full `state.evaluations` map (`{node_id: [records...]}`)
- `state` — the full state dict
- Safe functions: `len`, `any`, `all`, `min`, `max`, `sum`

**Goto sentinels:** `__end__` halts the workflow (maps to LangGraph
`END`). All other goto values must resolve to a real node id; the
schema validator rejects unknown targets at compile time.

**Routes are leaves in the depends_on DAG.** A node cannot
`depends_on:` a route — routing is exclusively via `Command(goto=)`,
not static edges. The schema validator rejects this at compile time.

**Goto targets skip the START fallback.** A node that is reached only
via a route's goto won't get an automatic START → node edge; it
runs only when the route activates it.

**Sandbox:** predicates are evaluated by `simpleeval`. Dunder access,
imports, and statements are blocked. Workflow YAML is treated as
author-checked-in code, not untrusted input — this is footgun
prevention, not adversarial sandboxing.

### Recursive subgraphs (Stage 4c / 5b shape)

A node with `execute: { url: path/to/sub.yaml, params: { ... } }` is a
subgraph reference. The referenced YAML is loaded as a `Graph`
(identical schema to the top-level workflow) and recursively compiled.
Graphs and subgraphs are definitionally identical — the same YAML file
is runnable both standalone via `abe-froman run <file>` and as a
subgraph reference from another graph.

State projection across the boundary is explicit and lives under
`execute.params`:

- `params.inputs:` renders parent context into the subgraph's
  `node_inputs` channel. Subgraph nodes see them as plain template
  variables (e.g. `{{topic}}`) alongside their own dep outputs.
- `params.outputs:` exposes named subgraph node outputs as
  `node_outputs[parent_id.key]` in the parent. Default (empty
  `outputs:`) projects the subgraph's terminal-node output as
  `node_outputs[parent_id]`.

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

The subgraph runs in isolation — it never sees the parent's full state,
only what `inputs:` projects in. Failed nodes inside a subgraph surface
as `failed_nodes[parent_id]` in the parent; subgraph internals are not
flattened. Compile-time guards: cycle detection over the
URL-reference DAG (now using `runtime/url.py`'s canonical form), and
`settings.max_subgraph_depth` (default 10).

#### Per-child subgraph fan-out (Stage 5b)

A `fan_out.template.execute.url` ending in `.yaml`/`.yml` runs a
**subgraph per Send branch** instead of a single executor call. Each
manifest item drives one subgraph invocation; the subgraph's terminal
output is what the parent sees in `child_outputs[parent::item_id]`.

```yaml
- id: reviewer_pool
  depends_on: [paper]
  fan_out:
    enabled: true
    template:
      execute:
        url: subgraphs/single_review.yaml
        params:
          inputs:
            persona: "{{style}}"
            paper_summary: "{{paper_summary}}"
      evaluation:
        validator: gates/review_quality.py
```

Demo: `examples/absurd-paper/` — `reviewer_pool` runs a draft → critique
2-node subgraph per reviewer.

### Prompt templating

Prompt files support `{{variable}}` placeholders. Variables are populated from dependency outputs:

```markdown
Here is the research from the previous phase:

{{research-phase}}

Based on this, generate a summary.
```

The variable name matches the `id` of the dependency phase. Its value is the raw output from that phase. For dependencies that ran under foreman, `{{dep_id_worktree}}` is also projected — the absolute path to that phase's git worktree (see Worktrees below). Final phases in a dynamic-subphase group additionally receive `{{parent_id_subphases}}` (JSON-encoded `subphase_id → output` map) and `{{parent_id_subphase_worktrees}}` (JSON-encoded list of subphase worktree paths).

On retry, the orchestrator auto-injects `{{_retry_reason}}` with the previous gate score, threshold, attempt number, and — when the gate produced structured feedback — the narrative `feedback` string plus any `pass_criteria_unmet` bullets. Templates can surface this text to the model so the next attempt acts on specifics, not just a bare score.

`execute.url` (when relative) and `evaluation.validator` paths resolve relative to the working directory (`--workdir`), not the config file location. `execute.url` additionally consults `settings.base_url` for relative-URL resolution — see "URL resolution" above.

### Quality gate validators

**Script validators** (`.py`, `.js`) receive the phase output on stdin and print to stdout.

Environment variables available to script validators:
- `PHASE_ID` — the phase's unique identifier
- `WORKFLOW_NAME` — the workflow's `name` field from config
- `ATTEMPT_NUMBER` — current attempt number (starts at 1, increments on retry)
- `WORKDIR` — the working directory path

```python
# gates/validate.py — reads stdin, prints 0.0–1.0 OR a JSON object
import json, sys
data = json.loads(sys.stdin.read())
print("1.0" if valid(data) else "0.0")
```

Script-gate output is accepted in three shapes (parsed in `runtime/gates.py:_parse_script_output`):

| Shape | Example | What gets populated |
|-------|---------|---------------------|
| Bare float | `0.85` | `score=0.85`; feedback empty |
| JSON with `score` only | `{"score": 0.6}` | `score=0.6`; feedback empty |
| JSON with full feedback | `{"score": 0.6, "feedback": "...", "pass_criteria_met": ["..."], "pass_criteria_unmet": ["..."]}` | everything flows into `_retry_reason` |

**LLM validators** (`.md`) render the file as a Jinja2 template with `{{output}}`, `{{phase_id}}`, `{{attempt}}` available, then call the phase's `PromptBackend`. The model response must be JSON with at least a `score` field; the full feedback schema above is supported. Malformed output fails loudly (`score=0.0` with a diagnostic feedback string) — it does not silently pass. Per-gate model override via `quality_gate.model`; otherwise falls back to `settings.default_model`.

### Gate routing

- `score >= threshold` → **pass** → continue to dependents
- `score < threshold` and retries left → **retry** → re-execute phase
- `score < threshold`, no retries, `blocking: true` → **fail** → dependents skipped
- `score < threshold`, no retries, `blocking: false` → **pass with warning** → continue

Routing is decided inside `_make_phase_node` (`compile/nodes.py`) via `classify_gate_outcome`; the resulting outcome is written to state (`completed_phases` / `failed_phases` / `retries`). The router in `compile/graph.py` is a pure state-reader — it does not re-classify, it just reads which bucket the phase landed in.

### Worktrees + author-written merge phases

Every phase runs in its own git worktree created under `<workdir>/.abe-foreman/wt-<phase-id>-<uuid>/`. `ForemanExecutor` (`runtime/foreman.py`) wraps `DispatchExecutor` and allocates a worktree per `phase.id` on first `execute()`, **reusing the same tree across retries** so a prompt phase can iterate on its own prior files when a quality gate fails. Subphases get worktrees keyed by `{parent_id}::{item_id}`.

Worktree isolation requires a git repo. If `--workdir` is not inside a git working tree, the CLI falls back to running `DispatchExecutor` directly (no foreman, no worktrees) and prints a notice.

Foreman **never cleans worktrees**. Downstream phases read upstream outputs via:
- **Text**: `{{dep_id}}` (unchanged) — the stdout/assistant-text output
- **Files**: `{{dep_id}_worktree}` (absolute path) — author references files in prompts or `cp` commands
- **Dynamic fan-out synthesis**: `{{parent_id}_subphase_worktrees}` (JSON list) in final phases — author iterates through subphase worktrees and merges results

There is no automatic merge. Authors write explicit reconciliation/merge phases (typically `type: command` with `cp` / `git merge-file`) that decide what flows from a worktree into the base workdir. Stray worktrees can be cleaned up with `git worktree remove <path>`.

### Concurrency caps

`settings.max_parallel_jobs` (default 4) bounds all parallel phase execution via an `asyncio.Semaphore`. `settings.per_model_limits` layers model-specific caps inside the global one — e.g. `{opus: 2, sonnet: 4}` ensures no more than 2 opus requests run concurrently. Set `max_parallel_jobs: 1` for fully serialized execution.

### Resume via LangGraph checkpointer

Workflow state is persisted by LangGraph's `AsyncSqliteSaver` to `<workdir>/.abe-froman-checkpoint.db`. The thread_id is a deterministic SHA1 hash of `(workflow_name, resolved_workdir)` (16 hex chars). `--resume` reads the most recent checkpoint for that thread, strips failure bookkeeping (`failed_phases`, `errors`, `retries`), and re-runs from where the previous attempt stopped. `phase_worktrees` survive resume and rehydrate into the new `ForemanExecutor` so retries land back in the same tree.

## Project Layout

- `src/abe_froman/schema/models.py` — Pydantic DSL (WorkflowConfig, Phase, Settings, QualityGate, execution types)
- `src/abe_froman/compile/graph.py` — YAML → LangGraph StateGraph (state-reader routers, dynamic fan-out, checkpointer wiring)
- `src/abe_froman/compile/nodes.py` — Phase node factory + pure helpers (dep check, context build, retry reason, gate classification)
- `src/abe_froman/compile/dynamic.py` — Dynamic subphase node factories
- `src/abe_froman/runtime/state.py` — WorkflowState TypedDict with LangGraph reducers
- `src/abe_froman/runtime/result.py` — ExecutionResult, PhaseExecutor/PromptBackend protocols
- `src/abe_froman/runtime/gates.py` — GateResult, script + LLM gate evaluation, output contract validation
- `src/abe_froman/runtime/foreman.py` — ForemanExecutor: concurrency semaphores + per-phase git worktree pool
- `src/abe_froman/runtime/logging.py` — Structured JSONL event logger
- `src/abe_froman/runtime/runner.py` — Streaming execution (thread_id passed through to LangGraph)
- `src/abe_froman/runtime/executor/dispatch.py` — Routes execution by phase type (takes per-call `workdir`)
- `src/abe_froman/runtime/executor/command.py` — Subprocess executor (per-call `workdir` → `cwd`)
- `src/abe_froman/runtime/executor/prompt.py` — PromptExecutor (template rendering, model downgrade)
- `src/abe_froman/runtime/executor/backends/stub.py` — Stub backend (default)
- `src/abe_froman/runtime/executor/backends/acp.py` — ACP backend (claude-code-acp)
- `src/abe_froman/runtime/executor/backends/factory.py` — Backend factory
- `src/abe_froman/cli/main.py` — Click CLI (wires AsyncSqliteSaver, ForemanExecutor, thread_id)

Persistence is handled by LangGraph's `AsyncSqliteSaver` (DB at `<workdir>/.abe-froman-checkpoint.db`), not a custom JSON envelope.

## Key Design Decisions

- **NodeExecutor Protocol** (not ABC) — duck-typed, agent-agnostic, accepts per-call `workdir` override
- **Single `execute:` block** (Stage 5b) — URL extension drives dispatch (prompt / subgraph / script / exec), with `type: join` and `type: route` carve-outs for non-URL shapes
- **Quality gates owned by orchestrator**, not executor — gates run in `_make_phase_node`, not inside the executor
- **Gate-kit is backend-agnostic** — `evaluate_gate` takes a `PromptBackend` handle, doesn't own one
- **LangGraph state with Annotated reducers** for safe parallel state merging
- **Model per phase** with `settings.default_model` fallback; LLM gates can also override
- **`{{variable}}` templating** in prompt files for parameterized execution (dep output, dep worktree, retry reason)
- **PromptBackend Protocol** — swappable backends (stub, acp, future: API key, OpenAI, etc.)
- **Foreman is LangGraph-free** — `runtime/foreman.py` imports nothing from `compile/` or `langgraph`; enforced by `tests/architecture/test_layers.py`
- **Routers are pure state-readers** — classification logic lives in `_make_phase_node`; the router just reads `completed_phases` / `failed_phases`
- **Persistence via LangGraph checkpointer** — no custom state file; `--resume` is a thread-id lookup
- **Worktree retention across retries** — foreman keys worktrees by phase_id; retries reuse the same tree so agents can iterate on prior work

## Known Limitations

- **Hyphenated phase IDs in templates:** `{{research-phase}}` is parsed by Jinja2 as subtraction (`research` minus `phase`) and will error. Use underscores in phase IDs that need template substitution.
- **Subphase quality gates:** Record scores but do not trigger retries. Retry routing only works for top-level phase gates.
- **Per-model backpressure under downgrade:** Foreman acquires the semaphore for the phase's *original* model. If `PromptExecutor.execute()` downgrades opus→sonnet mid-call (on `OverloadError`), the sonnet semaphore is not acquired for that call — "intent" not "enforcement under downgrade."
- **No automatic worktree cleanup:** Foreman never removes worktrees. Authors write explicit reconciliation phases, and leftover trees can accumulate under `<workdir>/.abe-foreman/`. Clean up manually with `git worktree remove <path>`.
- **Checkpointer migration:** Users on the pre-refactor `.abe-froman-state.json` format cannot `--resume` across the upgrade — the file is ignored; re-run from scratch.

## Backlog

Prioritized features for future development. See `docs/backlog-adapter-inspiration.md` for full details.

### P1 — Next up

1. ~~**Resume from failed phase**~~ — **DONE**. `--resume` uses LangGraph `AsyncSqliteSaver` (DB at `<workdir>/.abe-froman-checkpoint.db`) keyed by deterministic workflow-name+workdir thread_id. Phase worktrees rehydrate into a fresh `ForemanExecutor`.
2. ~~**Enhanced retry with failure context**~~ — **DONE**. On retry, `{{_retry_reason}}` is auto-injected with previous score, threshold, attempt number, gate feedback string, and `pass_criteria_unmet` bullets.
3. ~~**Output contract enforcement**~~ — **DONE**. After execution, before gate evaluation, `validate_output_contract()` checks required files exist. Contract failure is always blocking (hard fail, no retry).
4. ~~**Model downgrade on API overload**~~ — **DONE**. PromptExecutor catches OverloadError and auto-downgrades model tier (opus → sonnet → haiku). Backends detect 529/overload and raise OverloadError.

### P2 — Planned

5. ~~**Phase execution timeout**~~ — **DONE**. Per-phase `timeout` field and `settings.default_timeout`, enforced via `asyncio.wait_for()` on both executor and gate calls. Timeout failures are hard failures (no retry). Subphases inherit parent phase timeout.
6. ~~**Structured JSONL logging**~~ — **DONE**. `--log` flag writes JSONL events (`workflow_start`, `phase_completed`, `phase_failed`, `gate_evaluated`, `phase_retried`, `workflow_end`) via state-diff detection in the runner's `astream` loop.
7. ~~**Output directory scaffolding**~~ — **DONE**. `scaffold_output_directory()` in `runtime/gates.py` pre-creates `base_directory` (with `parents=True`) before execution. Called in `_make_phase_node()` after context setup, before `executor.execute()`.
8. ~~**Preamble injection**~~ — **DONE**. `settings.preamble_file` prepended to all prompt phases for shared project context. PromptExecutor prepends preamble contents before template rendering. Missing preamble file is a hard failure.

### P3 — Nice to have

9. ~~**Stepped retry backoff**~~ — **DONE**. `settings.retry_backoff` list of delay values in seconds. Applied via `asyncio.sleep()` before retry execution in `_make_phase_node`. Clamps to last value for attempts beyond list length. Empty list (default) = no delay.
10. Execution mode fallback chain (ACP → direct API on failure)
11. Post-workflow cleanup (remove intermediate artifacts on success)
13. ~~**Extended env var injection into validators**~~ — **DONE**. Gate validator scripts receive `PHASE_ID`, `WORKFLOW_NAME`, `ATTEMPT_NUMBER`, and `WORKDIR` as environment variables.
14. Git integration for outputs (auto-push to branch on completion)
15. Health check endpoint (for container orchestration)
16. ~~**Per-phase git worktree isolation**~~ — **DONE**. `ForemanExecutor` (`runtime/foreman.py`) allocates a worktree per `phase.id` under `<workdir>/.abe-foreman/` and reuses it across retries. Concurrency capped by `settings.max_parallel_jobs` + `settings.per_model_limits`.
17. ~~**LLM-based quality gates**~~ — **DONE**. `.md` validators are rendered as Jinja2 templates (with `{{output}}`, `{{phase_id}}`, `{{attempt}}`), dispatched to the phase's `PromptBackend`, and parsed with loud-failure semantics.
18. ~~**Structured gate feedback**~~ — **DONE**. `GateResult` carries `score`, `feedback`, `pass_criteria_met`, `pass_criteria_unmet`. Script gates that emit the expanded JSON shape (plus bare-float / `{"score": …}` legacy shapes) feed retry prompts.

### Wishlist (non-prioritized)

- Composite gate predicates (OR / weighted / expression) and multi-dimension `pass_criteria` with `{field, min}` shape
- Multi-tier retry escalation (retain worktree across escalation boundaries)
- Explicit synthesis phase with synthesis gate blocking merge
- Worktree GC policy (`abe-froman worktree list` / `prune --older-than 7d`)
- LLM gate token-usage attribution under a synthetic `{phase.id}::gate` key

## Testing

### Facts about the current suite
- ~688 tests (~30s non-ACP). Layout: `tests/unit/{schema,compile,runtime,cli,workflow}/`, `tests/architecture/`, `tests/builder/`, `tests/e2e/`, `tests/acp/`
- All tests use real execution — no mocks of external systems
- Command phases use real subprocesses (`echo`, `cat`, `false`)
- Gate validators are real Python scripts that inspect stdin
- Foreman tests exercise real `git worktree add` in a temp repo
- Resume tests exercise real `AsyncSqliteSaver` checkpoint roundtrips
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
