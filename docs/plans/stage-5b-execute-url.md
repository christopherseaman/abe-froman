# Stage 5b — Unified `execute: { url, params }` Schema

## Context

Today's YAML carries **seven** execution shapes for "what does this
node do" — the result of incremental Stage 1–4 additions, each of
which made local sense but compounded into sprawl:

| Shape | Today's YAML |
|---|---|
| Prompt (shorthand) | `prompt_file: "x.md"` |
| Prompt (full) | `execution: { type: prompt, prompt_file: "x.md" }` |
| Command | `execution: { type: command, command, args }` |
| Gate-only | `execution: { type: gate_only }` |
| Join | `execution: { type: join }` |
| Subgraph (top) | `config: "x.yaml"` + top-level `inputs:` + `outputs:` |
| Fan-out child | `fan_out.template.prompt_file` |

Every new execution kind so far has added another discriminator path. A
Stage 4 audit confirmed this is overbuild: `FanOutTemplate.config:` was
prototyped to add subgraph dispatch at the per-child level, then
reverted as part of the audit because it doubled down on the wrong
abstraction. The right move is to collapse all of these into one
shape.

## Proposal

A single `execute:` block on every node:

```yaml
- id: my-node
  name: "My Node"
  execute:
    url: "prompts/my.md"
    params:
      model: "opus"
```

**URL extension drives the dispatcher.** No `type:` discriminator. The
dispatch table maps file extension (or URL scheme) to a runtime
handler. `params:` is mode-specific.

### Dispatch table

| URL pattern | Mode | What it does | `params:` shape |
|---|---|---|---|
| `*.md`, `*.txt`, `*.prompt` | prompt | Renders file as Jinja template against context, sends to PromptBackend | `model`, `agent`, `timeout` (mode-specific overrides) |
| `*.yaml`, `*.yml` | subgraph | Loads as a `Graph`, recursively compiles, invokes per call | `inputs: { var: "{{template}}" }`, `outputs: { key: "{{sub_node}}" }` |
| `*.py` | python script | `python <url>` subprocess; stdin = nothing, stdout = output | `args: ["{{arg1}}"]`, `env: {KEY: "{{val}}"}` |
| `*.js`, `*.mjs` | node script | `node <url>` subprocess | same as `*.py` |
| `*.ts` | typescript | `tsx <url>` or `bun <url>` subprocess (configurable) | same as `*.py` |
| `*.sh` | shell script | `bash <url>` subprocess | same as `*.py` |
| `/abs/path/to/binary` or other | direct exec | Treats URL as a binary path; spawns subprocess | `args:`, `env:` |

### Special markers (no URL)

Some node kinds don't have an artifact to point at:

- **Gate-only**: a node with `evaluation:` and no `execute:`. The gate
  runs against the empty output. (Today's `execution: { type: gate_only }`
  collapses by elision.)
- **Join**: a node with multiple `depends_on:` and no `execute:`. The
  join is implicit in topology — the existing LangGraph behavior. The
  explicit `type: join` marker we have today exists for author
  readability but isn't load-bearing; it can stay as a sentinel
  `execute: { type: "join" }` if the team wants the explicitness, or
  be elided like gate-only.

### Fan-out

`fan_out.template:` becomes a recursive node spec — same `execute:`
shape applies:

```yaml
- id: reviewer_pool
  execute:
    url: "prompts/reviewer_pool.md"   # parent's prompt produces manifest
  fan_out:
    enabled: true
    template:
      execute:
        url: "subgraphs/single_review.yaml"  # per-child subgraph
        params:
          inputs:
            reviewer_id: "{{id}}"
            paper_summary: "{{paper_summary}}"
      evaluation:
        validator: "gates/review_quality.py"
```

Per-child subgraph dispatch (the thing the audit prototyped and then
reverted) **falls out for free** under this shape.

## Implementation surface

### Schema (`src/abe_froman/schema/models.py`) — ~50 LOC delta

- Define `Execute(BaseModel)` with `url: str` and `params: dict[str, Any] = {}`.
- Replace `Node.execution: Execution | None`, `Node.config: str | None`,
  `Node.inputs: dict`, `Node.outputs: dict`, `Node.prompt_file: str | None`
  with a single `Node.execute: Execute | None`.
- Drop the `Execution` discriminated union (`PromptExecution`,
  `CommandExecution`, `GateOnlyExecution`, `JoinExecution`).
- Drop `_normalize_prompt_shorthand` (no shorthand any more).
- `FanOutTemplate` becomes `{ execute: Execute, evaluation: Evaluation | None }`.
- `FanOutFinalNode` simplifies the same way.

### Dispatch (`src/abe_froman/runtime/executor/dispatch.py`) — ~100 LOC

- Add a `_DISPATCH_TABLE: list[tuple[Pattern, Handler]]` keyed by URL
  pattern.
- `DispatchExecutor.execute(node, ...)` reads `node.execute.url`,
  matches against the table, calls the handler with `(node, params,
  context, workdir)`.
- Handlers: `_dispatch_prompt`, `_dispatch_subgraph`, `_dispatch_script`,
  `_dispatch_binary`. Each returns `ExecutionResult`.

### Compile (`compile/graph.py`, `compile/dynamic.py`, `compile/subgraph.py`) — ~150 LOC

- `compile/graph.py` no longer keys on `node.config` to detect subgraph
  references. Instead: `node.execute and node.execute.url.endswith('.yaml')`.
- `compile/subgraph.py::detect_config_cycle` walks `node.execute.url`
  when extension matches `.yaml`. Same recursive structure, just keyed
  off `Execute` instead of `Node.config`.
- `compile/dynamic.py::_make_fan_out_node` reads `template.execute` and
  dispatches via the same handler table. The retry-loop wrapper is
  unchanged; only the per-Send "what runs" lookup changes.

### Migrate tool (`src/abe_froman/cli/migrate.py`) — ~80 LOC delta

The Stage 4 migrate tool already rewrites `phases:` → `nodes:`,
`quality_gate:` → `evaluation:`, etc. Stage 5b extends it with
post-Stage-4 → post-Stage-5b transforms:

| From | To |
|---|---|
| `prompt_file: "x.md"` | `execute: { url: "x.md" }` |
| `execution: { type: prompt, prompt_file }` | `execute: { url: prompt_file }` |
| `execution: { type: command, command, args }` | `execute: { url: command, params: { args } }` (or `url: "/usr/bin/<cmd>"`) |
| `execution: { type: gate_only }` | omit `execute:` block entirely (gate-only by elision) |
| `execution: { type: join }` | drop, OR `execute: { type: "join" }` if keeping explicit marker |
| `config: "x.yaml" + inputs: + outputs:` | `execute: { url: "x.yaml", params: { inputs, outputs } }` |
| `fan_out.template.prompt_file` | `fan_out.template.execute: { url }` |

The migrate tool gains a `--from-stage` flag (`--from-stage=3` for
pre-Stage-4 input, `--from-stage=4` for current input). The transforms
chain.

### Examples (`examples/`) — ~200 LOC delta across 4 workflows

Every checked-in workflow gets rewritten through the new shape. Run
the migrate tool against itself:

```bash
for f in examples/**/*.yaml; do
  uv run abe-froman migrate "$f" --from-stage=4 --in-place
done
```

Then hand-review the diffs. Compose-and-validate subgraph for
absurd-paper stays as-is (it's a `.yaml` referenced via `execute:
{ url: }`).

### Tests (`tests/`) — ~300 LOC delta

- All test fixtures using `prompt_file:`, `execution:`, `config:` get
  rewritten through migrate.
- New unit tests for the dispatch table (extension matching, handler
  selection, params validation per mode).
- New e2e tests confirming each dispatch mode fires correctly on a
  single workflow that exercises all of: prompt, subgraph, python
  script, command.

## Migration path

1. **Land Stage 5a first** (route node — independent of execute shape).
2. **Build Stage 5b on its own branch** (`stage-5b-execute-url`).
3. **Schema change is breaking** (no compat aliases — same policy as
   Stage 4's hard cutover). Migrate tool does the lift.
4. **Verification** identical to Stage 4 closeout: full pytest green,
   all four examples run via ACP, JSONL events unchanged (the schema
   change is upstream of the runtime telemetry).

## Estimated size

| Component | Net LOC |
|---|---|
| Schema | -100 (removing discriminator union) +50 = **-50** |
| Dispatch | +100 (handler table) -40 (existing type-switch) = **+60** |
| Compile (graph/dynamic/subgraph) | -50 (collapse type checks) +30 (URL extension dispatch) = **-20** |
| Migrate tool | +80 |
| Examples | ~0 (rewrite, no net add) |
| Tests | +100 (new dispatch tests) -50 (kill type-discriminator tests) = **+50** |
| **Total** | **~+120 net LOC, lots of churn** |

The schema *shrinks* (one shape replaces seven). The dispatch table
*grows* in one place to absorb the dispatch logic that used to live
scattered across `dispatch.py`, the `Execution` union, `Node.config`
handling, and `FanOutTemplate.prompt_file` handling. Net: smaller
mental footprint, modestly larger code surface in one well-bounded
place.

## Open design questions

1. **Join marker**: keep `execute: { type: "join" }` as an explicit
   sentinel, or rely on "no `execute:` block + multiple `depends_on:`"?
   Recommend: keep the sentinel for author intent.
2. **Bare commands**: `command: echo` doesn't have a file extension.
   Options: (a) treat any URL not matching an extension as a binary
   path, run via subprocess; (b) require `url: "/bin/echo"` (absolute
   path); (c) add `url: "echo"` + `params: { mode: "command" }`
   override. Recommend (a) for simplicity.
3. **Params validation**: do we validate `params:` shape per mode at
   schema time (e.g. subgraph requires no `args:`), or trust the
   handler to fail at runtime? Recommend schema-time per-mode dataclass
   for prompt/subgraph/script; keep `params:` open for direct-exec.
4. **Migration of `inputs:` / `outputs:`**: currently top-level on
   Node; in the new shape they're nested under `execute.params`. The
   migrate tool needs to lift them in. (Mechanical.)

## What this unlocks

- **Per-child subgraph fan-out** comes for free (audit's prototyped
  `FanOutTemplate.config:` becomes just another `execute.url` value
  that happens to be a `.yaml`).
- **Polyglot scripts** — `.py`, `.js`, `.ts`, `.sh` all dispatch
  through the same shape. Currently any non-prompt non-subgraph
  execution requires a `command` invocation that authors hand-write.
- **Future modes** plug in cleanly: a new dispatch handler is one
  table entry. No new `Execution` union member, no new `Node.<thing>`
  field.

## What this does NOT do

- Doesn't change runtime semantics: gating, retry, evaluation,
  worktrees, output_contract, fan-out are all unchanged.
- Doesn't change state channels (`node_outputs`, `child_outputs`,
  `evaluations` etc.) — those stay as Stage 4 left them.
- Doesn't change checkpointer behavior.
- Doesn't change CLI surface (`abe-froman run`, `validate`, etc.) —
  except `migrate` gains the `--from-stage=4` transform.

## Exit criteria

- [ ] `Execute` schema landed; `Execution` union deleted.
- [ ] Dispatch table operational; one handler per supported URL pattern.
- [ ] All examples migrated; all examples run via ACP.
- [ ] Migrate tool extended to lift Stage 4 → Stage 5b shape.
- [ ] Per-child subgraph fan-out works (the absurd-paper reviewer_pool
      carve becomes a one-line `template.execute.url` change). Re-run
      reviewer_pool with a real multi-step subgraph and ensure timing
      is acceptable (raise reviewer_pool's timeout to ~360s if needed
      so a sequential draft + critique fits under the per-Send cap).
- [ ] Full pytest green; test count comparable.
- [ ] No `Execution` / `PromptExecution` / `CommandExecution` /
      `GateOnlyExecution` symbols anywhere in src/.
