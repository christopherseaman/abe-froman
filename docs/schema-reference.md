# sqrlly — Workflow Schema Reference

The complete field-by-field reference for a sqrlly workflow YAML. For an
orientation and quickstart, see the [README](../README.md); for
architecture, see [TECHNICAL.md](../TECHNICAL.md).

All defaults below are the actual defaults declared on the Pydantic
models in `src/sqrlly/schema/models.py` and `src/sqrlly/schema/params.py`.

---

## Graph

The top-level document.

| Field | Type | Default | Effect |
|---|---|---|---|
| `name` | `str` | required | Workflow name. Part of the checkpoint thread id. |
| `version` | `str` | required | Free-form version string. |
| `nodes` | `list[Node]` | required | The workflow's nodes. |
| `settings` | `Settings` | `Settings()` | Global settings (see below). |

Compile-time validation rejects: duplicate node ids; `depends_on`
references to a missing or self node; `route` targets that are neither a
real node id nor `__end__`; a node that `depends_on` an inline-route
node; a `params.preset` that names no preset in `settings.presets`.

---

## Settings

`settings:` holds workflow-global configuration. Every field is optional.

### Retries, timeout, preamble

| Field | Type | Default | Effect |
|---|---|---|---|
| `output_directory` | `str` | `"output"` | Default base directory for output contracts. |
| `max_retries` | `int` | `3` | Default retry budget when an evaluation fails. |
| `default_timeout` | `float \| None` | `None` | Per-node timeout (seconds); `None` = no timeout. |
| `preamble_file` | `str \| None` | `None` | Prepended to every prompt before Jinja rendering. A missing file is a hard error. |
| `retry_backoff` | `list[float]` | `[]` | `asyncio.sleep` seconds before each retry; clamps to the last value past the list length. |

### Models and backends

| Field | Type | Default | Effect |
|---|---|---|---|
| `presets` | `dict[str, Preset]` | `{}` | Named execution bundles. See [Presets](#presets). |
| `model_downgrade_chain` | `list[str]` | `["opus", "sonnet", "haiku"]` | Tier list for `OverloadError` auto-downgrade. |

> The pre-rework `default_model` and `executor` settings no longer
> exist. Backend selection is now entirely through `presets` (or
> auto-detection when `presets` is empty).

### Concurrency

| Field | Type | Default | Effect |
|---|---|---|---|
| `max_parallel_jobs` | `int` | `4` | Foreman global semaphore. |
| `per_model_limits` | `dict[str, int]` | `{}` | Per-model caps layered inside the global semaphore. |
| `memory_threshold_pct` | `float \| None` | `None` | Foreman blocks new dispatches while host memory percent is above this (0–100). `None` disables. |
| `memory_min_available_bytes` | `int \| str \| None` | `None` | Foreman blocks new dispatches while available memory is below this. Accepts raw bytes or a binary-suffixed string (`"4GB"`, `"500MiB"`, `"2T"`; `KB = 1024`). `None` disables. |
| `max_subgraph_depth` | `int` | `10` | Cap on recursive subgraph nesting. |

Both memory gates compose (AND) with each other and with the
semaphores — every gate must allow dispatch. In-flight jobs are never
aborted by the gates; only new acquisitions wait.

### URL fetch / remote

| Field | Type | Default | Effect |
|---|---|---|---|
| `base_url` | `str \| None` | `None` | Default base for relative `execute.url`. |
| `allow_remote_urls` | `bool` | `false` | Master switch for non-`file://` fetches. |
| `allow_remote_scripts` | `bool` | `false` | Extra opt-in for remote `.py` / `.js` / `.sh`. |
| `allowed_url_hosts` | `list[str]` | `[]` | fnmatch host allow-list. `[]` = no filter. |
| `url_headers` | `dict[str, dict[str, str]]` | `{}` | URL-prefix → headers; `${VAR}` expands from env at fetch time. |
| `max_remote_fetch_bytes` | `int` | `5_000_000` | Body size cap (5 MB). Also applies to `file://` reads. |

---

## Presets

A preset is a named execution bundle under `settings.presets`. A node
selects one by name via `params.preset`; the matching default applies
otherwise. Two kinds, discriminated by a `kind` field (absent `kind`
defaults to `llm`, so pre-`CommandPreset` YAML still parses).

### LlmPreset (`kind: llm`)

| Field | Type | Default | Effect |
|---|---|---|---|
| `kind` | `"llm"` | `"llm"` | Discriminator. |
| `transport` | `"acp" \| "cli"` | required | Invocation shape — see comparison below. |
| `provider` | `"anthropic"` | required | Vendor / model family (only option for both transports today). |
| `model` | `str` | required | Model id. |
| `default` | `bool` | `false` | Exactly one `LlmPreset` must be `true`. |

Both supported transports drive Claude Code; the choice is invocation
shape, not vendor:

| `transport` | Implementation | Characteristics |
|---|---|---|
| `acp` | `claude-code-acp` adapter via the `acp` SDK; one warm process per preset, sessions reused across `send_prompt`. | Streaming chunks, MCP-via-session, lower per-call overhead once warm. |
| `cli` | `claude -p --model <model>` subprocess per `send_prompt`; stdin carries the prompt, stdout is the response. | No warm state, real `asyncio` parallelism per call (each subprocess is independent), simpler lifecycle (`close()` is a no-op). |

Both currently pair with `provider: anthropic` because both run
Claude Code. Additional `cli` providers (codex / gemini / custom) are
tracked as WISHLIST 36. The api transport — direct calls to
Anthropic / OpenAI / DeepSeek / custom OpenAI-compatible endpoints —
was removed in 0.2.x; re-introduction remains on the roadmap.

### CommandPreset (`kind: command`)

| Field | Type | Default | Effect |
|---|---|---|---|
| `kind` | `"command"` | required | Discriminator. |
| `command` | `str` | required | Interpreter command, e.g. `"uv run --no-project"`. |

A `CommandPreset` overrides the default interpreter for a script node.
The command is assembled as `shlex.split(command)` + the script path +
`params.args`; the placeholders `{{file}}` and `{{args}}` allow explicit
positioning. `CommandPreset` has no `default` — a script node opts in by
naming the preset in `params.preset`; scripts without a preset use the
built-in extension map.

### The "exactly one default" rule

When any `LlmPreset` exists in `settings.presets`, exactly one must have
`default: true` — zero or more than one is a `ValidationError`.
Command-preset-only workflows are exempt.

### Declare presets explicitly

sqrlly does not synthesize defaults from the local environment and
does not pre-flight CLI availability. Empty `settings.presets` is
valid for script-only workflows; any LLM-dispatching node will fail
at its call site with a clear "no prompt backend wired" error. A
missing adapter (`npx`, the npm package) surfaces as a backend error
at the first prompt call, not as a pre-flight check. This keeps
sqrlly out of the business of probing your toolchain and lets
earlier workflow steps install dependencies that later steps need.

`sqrlly run --preset <name>` overrides which `LlmPreset` is treated
as the default at run time. The named preset must exist in
`settings.presets`.

### Secrets

Both transports inherit credentials from the local `claude` CLI
session — no API keys are required for `transport: acp` or
`transport: cli`. **Auth is per LLM CLI, not sqrlly's job**: log in
to each tool with its own command (`claude /login`, future
`codex auth`, etc.). The generic secret resolver
(`runtime/secrets.py::resolve_secret`) is still available for
workflow-defined values (e.g., a script node calling out to a
third-party service); resolution order is process env →
project-local `.env` file (discovered by walking up from CWD).
sqrlly never reads machine-global keystores.

---

## Node

| Field | Type | Default | Effect |
|---|---|---|---|
| `id` | `str` | required | Unique id; referenced by `depends_on` and template `{{id}}`. |
| `name` | `str` | required | Human-readable label. |
| `description` | `str \| None` | `None` | Free text. |
| `execute` | `Execute \| None` | `None` | Execution descriptor. Omit it with `evaluation:` for a gate-only node, or with `route:` for a standalone router. |
| `depends_on` | `list[str]` | `[]` | Static DAG edges. Cannot reference an inline-route node. |
| `evaluation` | `Evaluation \| None` | `None` | Quality gate. |
| `output_contract` | `OutputContract \| None` | `None` | Required-files check after execution. |
| `fan_out` | `FanOut \| None` | `None` | Manifest-driven `Send` fan-out. |
| `route` | `Route \| None` | `None` | Inline forward-edge routing block. |
| `timeout` | `float \| None` | `None` | Per-node timeout; falls back to `settings.default_timeout`. |

`Node` declares `extra="forbid"` — an unknown key (including the
removed `model`, `prompt_file`, `inputs`, `outputs`) surfaces as a
`ValidationError` naming the offending key. Per-model selection is now
done with `params.preset`, not a node `model` field.

> **Footgun:** hyphenated node ids parse as subtraction in Jinja
> templates (`{{my-id}}` → `my - id`). Use underscores. `validate` and
> `run` emit an advisory warning for any hyphenated node id.

---

## Execute

| Field | Type | Default | Effect |
|---|---|---|---|
| `url` | `str \| None` | `None` | Path or URL to the resource to execute. |
| `type` | `"join" \| None` | `None` | Topology sentinel; the only value is `join`. |
| `mode` | `ExecuteMode \| None` | `None` | Override extension-driven dispatch. |
| `params` | `dict` | `{}` | Mode-specific params; coerced + validated at dispatch. |

Exactly one of `url` or `type: join` must be set. `type: join` rejects
`params` and `mode`. `ExecuteMode` is one of `prompt`, `subgraph`,
`exec`, `python`, `node`, `tsx`, `bash`.

```yaml
execute:
  url: "prompts/draft.md"
  params:
    preset: opus_preset
```

```yaml
execute:
  type: join          # no-op fan-in marker
```

### Per-mode params

Each mode's `params:` is validated by a Pydantic model with
`extra="forbid"` — a typo like `arg:` for `args:` fails at compile time.

| Mode (URL extension) | Model | Fields |
|---|---|---|
| Prompt (`.md`, `.txt`, `.prompt`) | `PromptParams` | `preset: str?`, `agent: str?`, `timeout: float?` |
| Subgraph (`.yaml`, `.yml`) | `SubgraphParams` | `inputs: dict[str,str]`, `outputs: dict[str,str]` |
| Script / binary (everything else) | `SubprocessParams` | `args: list[str]`, `env: dict[str,str]`, `preset: str?` |

`PromptParams.preset` names an `LlmPreset`; `SubprocessParams.preset`
names a `CommandPreset`. `mode:` overrides the extension-driven choice.

### URL dispatch table

| URL pattern | Handler | Params |
|---|---|---|
| `*.md` / `*.txt` / `*.prompt` (`file://`) | Prompt → `PromptBackend` | `PromptParams` |
| `*.md` (`https://`, allowed) | Prompt after fetch | `PromptParams` |
| `*.yaml` / `*.yml` (`file://`) | Subgraph (compile-time) | `SubgraphParams` |
| `*.py` / `*.js` / `*.mjs` / `*.ts` / `*.sh` (`file://`) | Script (interpreter dispatch) | `SubprocessParams` |
| `/abs/path` or extensionless (`file://`) | Direct exec | `SubprocessParams` |
| `type: join` / `execute: None` | no-op | — |

Script interpreter map: `.py` → `python3`, `.js` / `.mjs` → `node`,
`.ts` → `tsx`, `.sh` → `bash`. A `CommandPreset` overrides the
interpreter.

### URL resolution

`resolve_url(url, base_url, workdir)` applies three rules in order:

1. **Explicit protocol passthrough** — a URL with `://` is canonicalized (lowercase host) and returned.
2. **Absolute path → `file://`** — a URL starting with `/` becomes `file:///abs/path`.
3. **Relative resolves against base** — against `settings.base_url` if set, else against `--workdir`.

### Remote URL gates

Non-`file://` URLs pass only when **all** gates clear; defaults
reproduce a "local files only" policy.

| Gate | Setting | Effect |
|---|---|---|
| Master switch | `allow_remote_urls` | Must be `true` for any non-`file://` fetch. |
| Host allow-list | `allowed_url_hosts` | fnmatch; empty = no filter. |
| Script opt-in | `allow_remote_scripts` | Required for remote `.py` / `.js` / `.sh` / `.ts` / `.mjs`. |
| Size cap | `max_remote_fetch_bytes` | Default 5 MB; also applied to `file://` reads. |
| Headers | `url_headers` | Per-URL-prefix; `${VAR}` expands from env. |

A per-compile cache keyed by canonical URL fetches each URL at most once
per `build_workflow_graph` call.

> **⚠️ `file://` is not confined — treat workflow YAML as trusted
> input.** The "local files only" default is about *remote-vs-local*,
> not a sandbox. A `file://` path — absolute (`/etc/passwd`) or
> `../`-relative — resolves to that exact location and reads/executes
> with the orchestrator process's full filesystem access;
> `allow_remote_scripts` and the gates above apply only to *remote*
> schemes. There is no workdir confinement on local paths. Don't run
> workflow files from untrusted sources.

> **Remote execution scope.** Only remote **prompt** templates
> (`.md` / `.txt` / `.prompt`) are fetched-and-run today. Remote
> **script** and **binary** execution is not yet wired — a non-`file://`
> URL on a script/binary node halts with a clear error ("Remote script
> execution not yet wired … use `file://`"). `allow_remote_scripts`
> gates the *fetch* path in preparation for that, but execution still
> requires a local path.

---

## Evaluation

A quality gate that scores a node's output and drives retries.

| Field | Type | Default | Effect |
|---|---|---|---|
| `validator` | `str` | required | Path to a `.py` / `.js` script or `.md` LLM-prompt gate. |
| `threshold` | `float` | `0.0` | Minimum passing score (`0`–`1`). |
| `blocking` | `bool` | `false` | If `true`, exhausted retries fail the workflow; if `false`, they pass with a warning. |
| `max_retries` | `int \| None` | `None` | Overrides `settings.max_retries`. |
| `model` | `str \| None` | `None` | Model for `.md` LLM gates. |
| `dimensions` | `list[DimensionCheck] \| None` | `None` | Per-dimension score gates. |

### DimensionCheck

| Field | Type | Default | Effect |
|---|---|---|---|
| `field` | `str` | required | Dimension name. |
| `threshold` | `float` | required | Minimum for this dimension (`0`–`1`). YAML key `min:` is accepted as a back-compat alias. |

### Validator shapes

**Script validators (`.py`, `.js`)** read the node output on stdin and
print to stdout. Environment: `NODE_ID`, `WORKFLOW_NAME`,
`ATTEMPT_NUMBER`, `WORKDIR`. Output is accepted as a bare float
(`0.85`), `{"score": 0.6}`, or a full feedback object
(`{"score", "feedback", "pass_criteria_met", "pass_criteria_unmet"}`).

**LLM validators (`.md`)** are rendered as Jinja2 templates with
`{{output}}`, `{{phase_id}}`, `{{attempt}}`, then dispatched through the
node's backend. The response must be JSON with at least a `score`.
Malformed output fails loudly (`score=0.0` + diagnostic) — it never
silently passes.

### Routing

| Outcome | Result |
|---|---|
| `score >= threshold` | pass — dependents continue. |
| `score < threshold`, retries left | retry — re-execute the node. |
| `score < threshold`, retries exhausted, `blocking: true` | fail — dependents skipped. |
| `score < threshold`, retries exhausted, `blocking: false` | pass with warning — dependents continue. |

On retry, the previous score, per-dimension thresholds and reasons, and
gate feedback are formatted into a neutral preamble. Reference it in a
prompt template as `{{_retry_reason}}`.

---

## OutputContract

| Field | Type | Default | Effect |
|---|---|---|---|
| `base_directory` | `str` | required | Directory checked after the node runs. |
| `required_files` | `list[str]` | `[]` | Literal paths (no glob) that must exist for the node to succeed, checked under `base_directory` in the node's run dir (its worktree in a git repo). |

---

## Route — inline forward-edge dispatch

`Node.route` declares where flow goes next. A node with `route:` and no
`execute:` is a standalone router; a node with both runs its execute
body (and eval), then a synthetic dispatcher resolves the route.

### Shapes

**Goto shorthand** — unconditional:

```yaml
route:
  goto: write              # str → Command(goto="write")
```

```yaml
route:
  goto: [draft_a, draft_b] # list → static fan-out (concurrent edges)
```

**Conditional ladder** — first-match-wins:

```yaml
route:
  cases:
    - when: "passed('judge') and score('judge') >= 0.8"
      goto: ship
    - when: "len(history['judge']) >= 3"
      goto: __end__
  else: produce
```

| Field | Type | Default | Effect |
|---|---|---|---|
| `goto` | `str \| list[str] \| None` | `None` | Unconditional target(s). Mutually exclusive with `cases`. |
| `cases` | `list[RouteCase]` | `[]` | Predicate ladder. Requires `else`. |
| `else` | `RouteElse \| str \| list` | `None` | Fallback when no case matches. A bare string/list is auto-promoted. |
| `include_eval` | `bool` | `false` | (goto shorthand only) opt the target into the eval preamble. |

**RouteCase**: `when: str` (required), `goto: str | list` (required),
`include_eval: bool` (default `false`).
**RouteElse**: `goto: str | list` (required), `include_eval: bool`.

`__end__` halts the workflow (LangGraph `END`). All other targets must
resolve to a real node id. Inline-route nodes are leaves in the
`depends_on` DAG — nothing may `depends_on` them.

### `include_eval`

By default a goto target does **not** receive the previous node's
evaluation feedback (success paths usually do a different task). Set
`include_eval: true` on a case/else to auto-prepend the neutral eval
preamble to the target's prompt — no template syntax needed.

### Route predicate namespace

Case `when:` expressions evaluate in a sandboxed namespace (no dunders,
no imports, no statements):

| Name | Type | Meaning |
|---|---|---|
| `<dep_id>` | structured-or-raw | Each dep's structured output if present, else raw. |
| `history` | `dict[str, list[dict]]` | Full `state.evaluations`. |
| `state` | `dict` | Full state dict. |
| `evals` | `dict[str, dict]` | `evals[id]` → latest eval result, or `{}`. |
| `passed(id)` | `bool` | In `completed_nodes`, not in `failed_nodes`. |
| `score(id)` | `float` | Latest top-level score (0.0 if absent). |
| `scores(id)` | `dict[str, float]` | Latest per-dimension scores. |
| `len`, `any`, `all`, `min`, `max`, `sum` | builtins | Safe functions. |

---

## FanOut

A `fan_out:` block activates manifest-driven `Send` fan-out — its
presence is the activation; there is no enable flag.

| Field | Type | Default | Effect |
|---|---|---|---|
| `manifest_path` | `str \| None` | `None` | On-disk JSON manifest fallback. |
| `template` | `FanOutTemplate \| None` | `None` | What each Send branch runs. |
| `final_nodes` | `list[FanOutFinalNode]` | `[]` | Nodes that run after all branches, consuming aggregated output. |

The manifest is read from the parent node's JSON output, falling back to
`manifest_path`. Each Send branch runs `template.execute` (and
`template.evaluation`, with its own inline retry loop). Each branch's
template renders against a per-item Jinja context: dep outputs,
`{{<parent_id>}}` (the parent's output), and every key on the manifest
item.

**FanOutTemplate**: `execute: Execute` (required),
`evaluation: Evaluation?`.
**FanOutFinalNode**: `id: str`, `name: str` (required),
`description: str?`, `execute: Execute?`, `evaluation: Evaluation?`.

When `template.execute.url` ends in `.yaml`, each Send runs a per-child
subgraph instead of a single executor call.

---

## Node types in depth

### Prompt nodes

`.md` / `.txt` / `.prompt` files are read, optionally prepended with
`settings.preamble_file`, then rendered with full Jinja2. Template
context:

- `{{dep_id}}` — raw output of each dep; `{{dep_id_structured}}` — parsed structured output when present; `{{dep_id_worktree}}` — the dep's git worktree path.
- `{{_retry_reason}}` — auto-injected on retry (previous score, threshold, attempt, feedback).
- `{{evals}}` — `evals[id]` → latest eval result dict for any node, or `{}`.
- Inline-route goto target: `{{sender_id}}`, `{{sender}}`, `{{sender_structured}}`, `{{sender_worktree}}`.
- Fan-out child: every manifest-item key plus `{{<parent_id>}}`.
- Subgraph: whatever `params.inputs` projects in.

### Script and binary nodes

Scripts Jinja-render `params.args` and `params.env` against the dep
context, then run via `asyncio.create_subprocess_exec`. Exit code zero
is success; non-zero surfaces `stderr` as the error. A `CommandPreset`
overrides the interpreter.

### Subgraph nodes

A node whose `execute.url` is a `.yaml` references another graph, loaded
with the same `Graph` schema and recursively compiled.
`params.inputs` projects parent context into the subgraph;
`params.outputs` maps named subgraph node outputs back. Compile-time
guards: cycle detection over the URL-reference DAG, and
`settings.max_subgraph_depth`.

### Join

`execute: { type: join }` — a no-op fan-in topology marker. No params.

### Gate-only by elision

A node with `evaluation:` and no `execute:` runs the gate against an
empty output — useful as a synthetic checkpoint between phases.
