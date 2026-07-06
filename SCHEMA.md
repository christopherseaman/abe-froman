# sqrlly — Workflow Schema Reference

The complete field-by-field reference for a sqrlly workflow YAML. For an
orientation and quickstart, see the [README](README.md).

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

Unknown keys in `settings:` are rejected with a `ValidationError` — no silent ignoring. To diagnose typos or removed fields, run `sqrlly validate` and check the error message.

### Retries, timeout, preamble

| Field | Type | Default | Effect |
|---|---|---|---|
| `max_retries` | `int` | `3` | Default retry budget when an evaluation fails. |
| `backend_max_retries` | `int` | `0` | Retries of the SAME backend dispatch on a non-overload backend error (e.g. `claude exited 1` — a transient blip), with `retry_backoff` between attempts. Distinct from `max_retries` (the gate/evaluation budget). `0` = one attempt, terminal. Overload stays on the fixed opus→sonnet→haiku downgrade path and is not counted here. |
| `safe_mode` | `bool` | `false` | Run Claude with `--safe-mode` (cli transport only): operator customizations — output styles, CLAUDE.md, skills, MCP, hooks — are disabled, so generation stays reproducible and free of e.g. an "explanatory" output style prepending commentary into node output. Default off — sqrlly never overrides operator settings unless asked. The `--safe-mode` / `--no-safe-mode` CLI flag overrides this per run. |
| `default_timeout` | `float \| None` | `None` | Per-node timeout (seconds); `None` = no timeout. |
| `preamble_file` | `str \| None` | `None` | Prepended to every prompt before Jinja rendering. A missing file is a hard error. |
| `retry_backoff` | `list[float]` | `[]` | `asyncio.sleep` seconds before each retry; clamps to the last value past the list length. |

### Models and backends

| Field | Type | Default | Effect |
|---|---|---|---|
| `presets` | `dict[str, Preset]` | `{}` | Named execution bundles. See [Presets](#presets). |

> The pre-rework `default_model` and `executor` settings no longer
> exist. Backend selection is entirely through `presets`; there is no
> auto-detection (empty `presets` is valid for script-only workflows —
> LLM nodes then fail at dispatch).

### Concurrency

| Field | Type | Default | Effect |
|---|---|---|---|
| `max_parallel_jobs` | `int` | `4` | Foreman global semaphore. |
| `memory_threshold_pct` | `float \| None` | `None` | Foreman blocks new dispatches while host memory percent is above this (0–100). `None` disables. |

### Worktree isolation

| Field | Type | Default | Effect |
|---|---|---|---|
| `worktree` | `Literal["auto","isolated","off"]` | `"auto"` | Graph-level isolation **mode**, inherited graph→subgraph. `auto` = isolate each node in its own git worktree iff the workdir is a git repo (else no worktree). `isolated` = force a per-node worktree. `off` (alias `none`) = no worktree; the node runs in the shared base workdir. Bare YAML `off`/`on` booleans are accepted (`off`→`off`, `on`→`isolated`). A non-mode value is rejected (a shared-tree **name** goes in `worktree_group`, not here). Per-node override: `Node.worktree`. |
| `worktree_group` | `str \| None` | `None` | Names a **shared worktree** so multiple nodes write into one tree (the "feature team" pattern). Mutually exclusive with an explicit `worktree` mode (`isolated`/`off`) — setting both is a validation error; `worktree` left at `auto` is fine. Inherited graph→subgraph; a child authoring either field clears the inherited sibling. Resolution is by scope specificity (node → subgraph → graph). |
| `worktree_gc` | `Literal["never","on_success"]` | `"never"` | Opt-in worktree cleanup. `on_success` removes every allocated worktree (per-node and shared group trees, each once) after a **clean** run (no `failed_nodes`); `never` keeps them for inspection / `--resume`. GC is end-of-run only — never mid-run — so retry-reuse and resume rehydrate are unaffected. A failed run never GCs. |
| `on_promote_conflict` | `"fail" \| "warn" \| "overwrite" \| "skip"` | `"warn"` | Resolution when two same-wave promoting nodes touch the same path. `warn` logs the overlap and applies last-write-wins (run stays green); `fail` aborts before any write; `overwrite` is silent last-write-wins; `skip` keeps the first promoting node's version (by `nodes` order) and drops the path from later nodes (their other paths still promote). Detection runs discover-first, so `fail` never half-promotes. |
| `promote_exclude` | `list[str]` | `[]` | Git pathspecs filtered out of every promoting node's footprint (promote layer). Defense-in-depth keeping generated artifacts (node_modules, build caches) out of base. |
| `promote_include` | `list[str]` | `[]` | Git pathspecs RE-INCLUDED into every promoting node's footprint after `promote_exclude` removes them — the allow-list half. `promote_exclude: ["log/"]` + `promote_include: ["log/phases/**"]` promotes `log/phases/**` while dropping the rest of `log/`. (Git `:(exclude)` has no in-list negation, so this is a second pass; `include` overrides `exclude`.) |
| `worktree_share` | `list[str]` | `[]` | Read-only whole-dir symlinks of these base paths into each worktree (no install). For read-only in-branch gates (`tsc --noEmit`, scoped tests). Each is also written to the repo's shared `.git/info/exclude` (git has no per-worktree exclude) so it stays out of the promote footprint. |
| `worktree_setup` | `list[str]` | `[]` | Ordered shell commands run in each fresh worktree (e.g. `pnpm install --prefer-offline`, `pnpm exec prisma generate`). Sentinel-gated (runs once per base state); fatal-per-branch on failure. PM-agnostic. |
| `worktree_setup_exclude` | `list[str]` | `[]` | Paths written to the repo's shared `.git/info/exclude` (git has no per-worktree exclude) before promote, so rehydrate artifacts (node_modules, in-tree generated clients) stay out of the footprint. Explicit (sqrlly does not infer them). |

The memory gate composes (AND) with the semaphores — every gate must
allow dispatch. In-flight jobs are never aborted by the gate; only new
acquisitions wait.

### URL fetch / remote

| Field | Type | Default | Effect |
|---|---|---|---|
| `base_url` | `str \| None` | `None` | Default base for relative `execute.url`. |
| `allow_remote_urls` | `bool` | `false` | Master switch for non-`file://` fetches. |
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
| `permission_mode` | `"default" \| "acceptEdits" \| "bypassPermissions" \| "plan" \| None` | `None` | Tool-use policy. `cli` → `--permission-mode`; `acp` → kind-gate in the permission callback (`bypassPermissions`=all, `acceptEdits`=edits+reads not execute, `default`/`plan`=read-only). |
| `allowed_tools` | `list[str] \| None` | `None` | Tools to permit. `cli` → `--allowedTools` (exact claude names); `acp` → best-effort match vs tool kind/title. |
| `disallowed_tools` | `list[str] \| None` | `None` | Tools to deny. `cli` → `--disallowedTools`; `acp` → best-effort denylist by kind/title. |
| `env` | `dict[str, str]` | `{}` | Environment overlay for the spawned backend process (cli + acp), e.g. `CLAUDE_CODE_EFFORT_LEVEL`. Overlaid on the inherited environment at spawn; never replaces it. Per-node tuning is done by selecting a preset variant via `params.preset`. |
| `cli_args` | `list[str] \| None` | `None` | **cli only** — extra args appended verbatim to the `claude` argv (escape hatch). Ignored by `acp`. |

> Tool-use defaults (all unset): `cli` runs bare `claude -p` (no tools);
> `acp` allows all tool calls (its historical behavior). The shape is
> unified across transports; `permission_mode` is the portable knob,
> the tool lists are exact on `cli` and best-effort on `acp` (which
> gates by tool *kind*, not claude tool names).

Granting `Task` in `allowed_tools` (with a `permission_mode` that permits
it) enables an in-node *managed-team* pattern — Claude inside the node
spawns and supervises sub-agent members itself. See SKILLS.md for the full
coordinator pattern and its caveats (the members are sub-agents internal to
one sqrlly node, not isolated sqrlly nodes).

Both supported transports drive Claude Code; the choice is invocation
shape, not vendor:

| `transport` | Implementation | Characteristics |
|---|---|---|
| `acp` | `claude-code-acp` adapter via the `acp` SDK; one warm process per preset, sessions reused across `send_prompt`. | Streaming chunks, MCP-via-session, lower per-call overhead once warm. |
| `cli` | `claude -p --model <model>` subprocess per `send_prompt`; stdin carries the prompt, stdout is the response. | No warm state, real `asyncio` parallelism per call (each subprocess is independent), simpler lifecycle (`close()` is a no-op). |

Both currently pair with `provider: anthropic` because both run
Claude Code. Additional `cli` providers (codex / gemini / custom) are
tracked as TODO 36. The api transport — direct calls to
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
`codex auth`, etc.). Workflow-defined values reach sqrlly through
`settings.url_headers` `${VAR}` expansion: process env first, then a
project-local `.env` file (discovered by walking up from CWD).
sqrlly never reads machine-global keystores. Script nodes needing
third-party credentials read their own env (`params.env` /
inherited environment).

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
| `worktree` | `Literal["auto","isolated","off"] \| None` | `None` | Per-node isolation **mode** override; `None` inherits. Values: `auto` / `isolated` / `off` (alias `none`); bare YAML `off`/`on` booleans accepted. `off` runs the node in the shared base workdir (so e.g. a script gate, which runs at the base workdir, can read the node's files). See `settings.worktree`. |
| `worktree_group` | `str \| None` | `None` | Per-node shared-tree name; mutually exclusive with an explicit `worktree` mode. Nodes sharing a name share one worktree. See `settings.worktree_group`. |
| `promote` | `bool` | `False` | After a **clean** run, apply this node's worktree git delta (adds/edits/deletes vs the fork point) to the base workdir. Discovers the footprint from git — no need to predict which files changed — so it handles open-ended work (bugfixes/refactors) including deletions. If `output_contract.required_files` is set, those entries (git pathspec; `*`/`**`/`?`/`[…]`) filter what's promoted; otherwise the whole delta is promoted. Runs before GC. Top-level nodes only. An `off` node already writes to the base workdir, so `promote` is a no-op there. Multi-source reconciliation of *overlapping* isolated trees (3-way merge) is out of scope. |

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
`exec` — it forces the dispatch *kind* when the URL extension is
missing or misleading. To run a script under an arbitrary interpreter
(a specific venv, `uv run`, …), name a **command preset** via
`params.preset`, not `mode`.

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
| Size cap | `max_remote_fetch_bytes` | Default 5 MB; also applied to `file://` reads. |
| Headers | `url_headers` | Per-URL-prefix; `${VAR}` expands from env. |

A per-compile cache keyed by canonical URL fetches each URL at most once
per `build_workflow_graph` call.

> **⚠️ `file://` is not confined — treat workflow YAML as trusted
> input.** The "local files only" default is about *remote-vs-local*,
> not a sandbox. A `file://` path — absolute (`/etc/passwd`) or
> `../`-relative — resolves to that exact location and reads/executes
> with the orchestrator process's full filesystem access; the gates
> above apply only to *remote* schemes. There is no workdir confinement
> on local paths. Don't run workflow files from untrusted sources.

> **Remote execution scope.** Only remote **prompt** templates
> (`.md` / `.txt` / `.prompt`) are fetched-and-run today. Remote
> **script** and **binary** execution is not wired — a non-`file://`
> URL on a script/binary node halts with a clear error ("Remote script
> execution not yet wired … use `file://`").

---

## Evaluation

A quality gate that scores a node's output and drives retries.

| Field | Type | Default | Effect |
|---|---|---|---|
| `validator` | `str` | required | Path to a `.py` / `.js` script or `.md` LLM-prompt gate. |
| `threshold` | `float` | `0.0` | Minimum passing score (`0`–`1`). |
| `blocking` | `bool` | `false` | If `true`, exhausted retries fail the workflow; if `false`, they pass with a warning. A gate with a positive `threshold` but `blocking: false` is advisory only (a below-threshold score won't halt) — `validate`/`run` emit an advisory warning so a hollow "green" run isn't mistaken for a real pass. The `gate_evaluated` JSONL event carries `passed` / `threshold` / `blocking` so a consumer can tell a pass from a warn-continue without recomputing. For a `dimensions:` gate it also carries `scores` (per-dimension values) and `dimension_thresholds` (the configured per-dimension floors), so a consumer can attribute which dimension blocked without reading the YAML. |
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
`{{output}}`, `{{node_id}}`, `{{attempt}}`, then dispatched through the
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

> `required_files` are interpreted relative to `base_directory` for **both** the existence check and the promote glob filter. A node with `base_directory: reference` and `required_files: [x.json]` validates and promotes `reference/x.json`.

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
| `promote` | `bool` | `false` | When true, each Send branch's worktree delta is promoted back to base at the end of a clean run (top-level nodes only, before GC), through `reconcile_promotions` + `on_promote_conflict` + `promote_exclude` — the native merge-back for an isolated parallel build. Distinct from `Node.promote` (which promotes the parent's own manifest-only worktree delta, not the branch deltas); setting `node.promote: true` on a fan-out parent is a no-op, and `sqrlly validate` warns to steer you to `fan_out.promote` instead. |

The manifest is read from the parent node's JSON output, falling back to
`manifest_path`. Each Send branch runs `template.execute` (and
`template.evaluation`, with its own inline retry loop). Each branch's
template renders against a per-item Jinja context: dep outputs,
`{{<parent_id>}}` (the parent's output), and every key on the manifest
item.

**Manifest structure and validation:**

- Manifest items without an `id` field log a WARNING — all such items collapse onto a single `<parent_id>::unknown` child (a silent N→1 collapse, often unintended).
- Duplicate `id` values within the manifest raise a `ManifestError` before dispatch, listing the duplicate child ids. Each item must have a unique `id` to produce one branch per item.

**FanOutTemplate** fields:

| Field | Type | Default | Effect |
|---|---|---|---|
| `execute` | `Execute` | required | What each Send branch runs. |
| `evaluation` | `Evaluation \| None` | `None` | Optional gate applied to each branch (with its own inline retry loop). |
| `worktree` | `"auto" \| "isolated" \| "off"` | `null` (inherit `settings.worktree`) | Per-fan-out isolation override. Lets one workflow run an isolated build fan-out AND a shared-base planner fan-out under one top-level `settings.worktree`: this value overrides `settings.worktree` for every branch of THIS fan-out. `off` runs branches in the shared base workdir (their writes are visible to a downstream join node); `isolated`/`auto` give each branch its own git worktree. Same optional-override semantics as `Node.worktree`. Applies to both subgraph (`.yaml`) and script (`.md`/`.py`) templates. |

**FanOutFinalNode**: `id: str`, `name: str` (required),
`description: str?`, `execute: Execute?`, `evaluation: Evaluation?`,
`output_contract: OutputContract?` (enforced via the standard
execution path, like a top-level node's).

When `template.execute.url` ends in `.yaml`, each Send runs a per-child
subgraph instead of a single executor call.

---

## Fan-out resume behavior

On `--resume` after a failed run, fan-out children that failed are re-run; siblings that completed successfully are frozen (not re-billed). This asymmetry means a workflow that fans out over a manifest can efficiently re-test only the broken items without re-running good ones.

When using `--resume-from <fan_out_parent>`, the parent re-fans over its manifest, but only non-completed children from that fan-out are dispatched — completed siblings remain frozen. (Prior behavior: all children were re-run; now: only failed/missing children run.)

---

## Node types in depth

### Prompt nodes

`.md` / `.txt` / `.prompt` files are read, optionally prepended with
`settings.preamble_file`, then rendered with full Jinja2. Template
context:

- `{{dep_id}}` — raw output of each dep; `{{dep_id_structured}}` — parsed structured output when present; `{{dep_id_worktree}}` — the dep's git worktree path.
- Fan-out parent dep: `{{dep_branches}}` (JSON id→output), `{{dep_branch_worktrees}}` (JSON list of worktree paths), `{{dep_branch_map}}` (JSON id→`{output, worktree}`, the preferred id-keyed pairing). Worktree paths are **absolute** (resolved from `base_workdir`), so a fan-in consumer can read a sibling branch tree directly without re-resolving.
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
guards: cycle detection over the URL-reference DAG, and a fixed
nesting-depth cap (`MAX_SUBGRAPH_DEPTH = 10`).

### Join

`execute: { type: join }` — a no-op fan-in topology marker. No params.

### Gate-only by elision

A node with `evaluation:` and no `execute:` runs the gate against an
empty output — useful as a synthetic checkpoint between phases.
