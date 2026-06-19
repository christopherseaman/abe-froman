---
name: sqrlly
description: Author, validate, run, and debug sqrlly workflows. Use when the task involves creating or editing a sqrlly workflow YAML file, wiring LLM / script / subgraph nodes, quality gates, routing, or fan-out, or invoking the `sqrlly` CLI. Trigger words — sqrlly, workflow YAML, workflow orchestrator.
---

# Using sqrlly

sqrlly compiles a YAML workflow into a runnable LangGraph and executes
it. A workflow is a list of **nodes**; each node runs an LLM prompt, a
script, a binary, or a nested workflow. Quality **gates** retry failing
nodes with feedback; **routing** and **fan-out** handle branching and
parallelism.

## Prerequisites

Install the CLI: `pipx install sqrlly` (or `uv tool install sqrlly`).
Use the `[acp]` extra (`pipx install "sqrlly[acp]"`) only for
`transport: acp`.

LLM nodes drive Claude Code, so the chosen backend must be installed
and authenticated — auth is per-CLI, not sqrlly's job:

- `transport: cli` — the `claude` binary on PATH, logged in once with
  `claude /login`. No extra Python deps.
- `transport: acp` — `npm i -g @zed-industries/claude-code-acp`, plus
  the same `claude` login.

Script, binary, and subgraph nodes need no backend.

## Author a workflow

Fastest start: `sqrlly init [dir]` scaffolds a schema-valid
`workflow.yaml` + `prompts/hello.md` to edit from. To author by hand,
write a YAML file with `name`, `version`, `nodes`, and `settings`.

1. **Each node** needs a unique `id` (use underscores, never hyphens —
   see Footguns) and a `name`. Add an `execute:` block unless the node
   is a pure gate or a pure router.
2. **`execute.url`** points at the resource to run; the file extension
   selects the handler — `.md` / `.txt` / `.prompt` = LLM prompt,
   `.py` / `.js` / `.ts` / `.sh` = script, `.yaml` = subgraph, anything
   else = binary.
3. **Wire dependencies** with `depends_on: [other_id]`. A prompt
   template reads an upstream node's output as `{{other_id}}` (full
   Jinja2 — `{% if %}`, `{% for %}`, filters all work).
4. **Pick a backend** under `settings.presets` — one named `LlmPreset`
   marked `default: true`. Two transports drive Claude Code:
   `transport: acp` (warm adapter, streaming) or `transport: cli`
   (subprocess-per-call, real asyncio parallelism — pair with fan-out).
   ```yaml
   settings:
     presets:
       default:
         transport: cli        # or acp
         provider: anthropic   # only provider currently supported
         model: sonnet
         default: true
         # Tool use (optional). Same shape on both transports:
         permission_mode: acceptEdits   # default | acceptEdits | bypassPermissions | plan
         allowed_tools: ["Edit", "Bash(git *)"]
         # disallowed_tools: ["WebFetch"]
         # cli_args: ["--add-dir", "."]   # cli-only escape hatch
   ```
   There is no environment auto-detect. A workflow whose nodes are all
   script / binary / subgraph can omit `settings.presets` (or set it to
   `{}`); any **LLM (prompt) node** needs at least one preset with
   `default: true`, or dispatch fails at run time.

   **Tool use.** `permission_mode` is the portable knob; on `cli` it maps
   to `claude`'s `--permission-mode`, on `acp` it gates by tool *kind*
   (`bypassPermissions` = all, `acceptEdits` = edits+reads not bash,
   `default`/`plan` = read-only). `allowed_tools` / `disallowed_tools`
   are exact claude tool names on `cli` and best-effort (kind/title
   match) on `acp`. `cli_args` is a cli-only escape hatch. Defaults
   (all unset): `cli` runs with no tools; `acp` allows all (its
   historical behavior).
5. **Add a gate** to retry a node until its output is good enough:
   ```yaml
   evaluation:
     validator: gates/check.py   # .py / .js script, or .md LLM prompt
     threshold: 0.8
     blocking: true
     max_retries: 3
     # dimensions:               # optional multi-criteria gate
     #   - field: accuracy
     #     threshold: 0.8
     #   - field: clarity
     #     threshold: 0.7
   ```
   The validator's **output contract**: print to stdout a bare number
   (`0.85`) or a JSON object (`{"score": 0.6, "feedback": "..."}`); a
   script gate reads the node output on stdin, exits 0. With
   `dimensions`, the JSON carries one numeric field per dimension
   (`{"accuracy": 0.9, "clarity": 0.6}`) and each is checked against its
   own threshold. LLM (`.md`) gates return the same JSON — a surrounding
   ``` code fence or reasoning preamble is tolerated. A validator that
   **can't produce a score** (crashes, exits non-zero, or emits no
   parseable score) halts the run loudly — distinct from a valid score
   below threshold.
6. **Node options** (any node): `timeout: 30` (seconds; the node is
   killed and fails if it overruns) and `output_contract:` to assert a
   node wrote files —
   ```yaml
   output_contract:
     base_directory: "."                  # base for required_files (required)
     required_files: ["report.md"]        # node fails if missing after it runs
   ```
   In a git repo, files are checked in the node's worktree (where it
   wrote them), so write paths relative to the run, not absolute.
   `required_files` entries are **literal paths** — globs are not
   expanded (`out_*.md` is checked verbatim and will report missing).
7. **Branch** with a `route:` block — an unconditional `goto:`, or a
   `cases:` / `else:` ladder of `when:` predicates:
   ```yaml
   route:
     cases:
       - when: "score('classify') >= 0.8"   # predicate (see below)
         goto: high_quality
       - when: "'urgent' in classify"        # `classify` = its output
         goto: escalate
     else:
       goto: default_path
   ```
   `when:` is a **Python expression** (sandboxed simpleeval) — use
   `True` / `False`, not the string `"true"`. In scope: each upstream
   dependency id (bound to its output), `state` (the full state dict),
   and `history` / `evals` (evaluation records); plus functions
   `passed(id)`, `score(id)`, and `scores(id)`.
8. **Parallelize** with a `fan_out:` block — one branch per manifest
   item, joined by `final_nodes`:
   ```yaml
   fan_out:
     manifest_path: items.json   # JSON: [{"id": "a", ...}, ...] or {"items": [...]}
     template:
       execute:
         url: prompts/item.md     # rendered once per item
       # evaluation: ...          # optional: gate each branch
     final_nodes:                 # inline nodes; run once after all branches join
       - id: summarize
         name: Summarize
         execute:
           url: prompts/summarize.md
   ```
   `final_nodes` is a list of **inline node definitions** (each needs
   `id` / `name` / `execute`; they also accept `evaluation` and
   `output_contract`), not references to top-level node ids. Each
   manifest item is a JSON **object**; its fields become template
   variables (`{{id}}`, `{{name}}`, …). A bare string item is treated
   as `{"id": "<string>"}`. The manifest may instead be produced at run
   time as this node's JSON output — a ``` code fence or surrounding
   prose around the JSON array/object is tolerated — with `manifest_path`
   as the static fallback (a *declared* manifest_path that's missing or
   invalid JSON halts the run; an empty-but-valid manifest warns and
   skips the fan-out).

Do not guess field names. Every model is `extra="forbid"` — a typo'd
key is a hard validation error. The exhaustive field reference is
`SCHEMA.md` (online:
https://github.com/christopherseaman/sqrlly/blob/main/SCHEMA.md).

## Validate and run

Always validate before running:

```bash
sqrlly validate path/to/workflow.yaml
sqrlly run path/to/workflow.yaml --log run.jsonl
```

`validate` compile-checks the graph (schema + wiring) and reports the
node count plus advisory warnings. It does **not** check that every
`execute.url` / `validator` file exists on disk — those surface at run
time. `run` executes the workflow.

**File resolution / git.** Relative `execute.url` / `validator` paths
resolve against `--workdir` (default the current directory). When the
workdir is a git repo, runs execute inside a fresh git worktree of
`HEAD`, so paths resolve from the **repo root** — **commit your
workflow and the files it references first**, set `--workdir` to the
repo root (not a subdirectory), and write urls repo-root-relative
(`examples/x/prompt.md`). A repo with no commits can't create a
worktree. Outside a git repo, worktree isolation is off and paths
resolve directly under `--workdir`.

**Controlling worktree isolation.** `worktree` (on `settings` or a
node) picks the isolation mode: `auto` (default — isolate per node iff
in a git repo), `isolated` (force a worktree), `off` (run in the shared
base workdir — use this when nodes must see each other's files, or so a
script gate can read the node's files directly). For a shared tree
across *some* nodes, give them the same `worktree_group: <name>` — they
write into one worktree (the feature-team pattern) with no merge step.
`worktree` and `worktree_group` are mutually exclusive on one node;
both inherit graph→subgraph and resolve by scope specificity (a node's
setting beats the subgraph's beats the graph's). **Footgun:** a
`worktree_group` on a *fan-out template* collapses all dynamic children
into one shared tree — keep fan-out children on `auto`/`isolated`
unless you intend that.

**Getting results out + cleanup.** An isolated/group node's files live in
its worktree, not the base workdir. Set `promote: true` on a top-level
node to apply its worktree's git delta (adds/edits/**deletes**) back to
the base workdir after a clean run — the change set is discovered from
git, so it works for open-ended edits (bugfixes/refactors) you can't
enumerate in advance; an `output_contract` (glob paths) narrows it.
Set `settings.worktree_gc: on_success` to remove worktrees after a clean
run (default `never` keeps them for inspection / `--resume`). The full
lifecycle is **fork → produce → read/share → promote → GC**.

**Sharing base deps into worktrees.** A fresh worktree is a clean
checkout — gitignored base dependencies (`node_modules`, generated
clients) are not in it, so a node that needs them must get them. Two
settings supply them without leaking them into the promote footprint.
`settings.worktree_share: [<path>, …]` makes a read-only whole-dir
**symlink** of each listed base path into every worktree (no install) —
the cheap path, correct for read-only in-branch gates (`tsc --noEmit`,
scoped tests). `settings.worktree_setup: [<cmd>, …]` runs ordered shell
commands in each fresh worktree so the package manager builds its
**own** real deps there (e.g. `["pnpm install --prefer-offline", "pnpm
exec prisma generate"]`) — the rehydrate path, for branches that mutate
deps or need write isolation; it is package-manager-agnostic,
sentinel-gated (runs once per base state, not per node), and **fatal for
that branch** on a non-zero exit (other branches proceed). Keep
`worktree_setup` to pure base hydration — a branch that mutates
`schema.prisma` should re-run `prisma generate` in its gate/build body,
not in setup. If a setup command needs the package store on the same
device as the worktree (pnpm's EXDEV/hardlink fix), set
`settings.worktree_setup_store_dir: <path>` — it is exported into the
setup env as both `npm_config_store_dir` and `PNPM_HOME`.

**Keeping rehydrated deps out of promote.** Both mechanisms write the
shared paths to the repo's shared `info/exclude` so they stay out of
`git status` and therefore out of a `promote` delta. Rehydrate artifacts
that `worktree_setup` creates are NOT inferred — list them explicitly in
`settings.worktree_setup_exclude: [<path>, …]` (e.g. `node_modules`, an
in-tree generated-client dir like `src/generated/prisma`). As a
promote-layer backstop, `settings.promote_exclude: [<pathspec>, …]`
filters those pathspecs out of **every** promoting node's footprint even
if one slips past the `info/exclude` write — cheap defense-in-depth on
the one operation (promote) you most can't afford to get wrong.

**Subgraph fan-out isolation.** When a fan-out template is a subgraph
(`.yaml`), each Send branch gets its own worktree (keyed by the branch
id) and the subgraph's inner nodes all run *inside* that one branch tree
— the **branch is the isolation unit**. Inner nodes share the branch
tree (so a later inner node reads an earlier one's files for free; two
inner nodes writing the *same* path race — give them distinct paths). An
inner node's own `worktree`/`worktree_group` is neutralized — it can't
escape the branch tree or join a cross-branch group. A fan-out nested
*inside* a branch shares the outer branch tree (it isn't a second
isolation level).

**Remote sources.** `settings.base_url` sets the base for relative
urls — including an `http(s)://` base, which fetches **prompt
templates** over the network (gated by `allow_remote_urls`,
`allowed_url_hosts`, `url_headers`, `max_remote_fetch_bytes`). Remote
**scripts/binaries** are not supported (they fail with a clear error;
use `file://` paths for those). `url_headers` is keyed by URL
**prefix** (most-specific first), each mapping to a header dict:
`{ "https://api.host/": { "Authorization": "Bearer ${TOKEN}" } }`.
`${VAR}` expands from the process env, then the project-local `.env`.

`run` flags: `--workdir/-w <dir>`, `--dry-run` (trace topology without
executing), `--preset/-p <name>` (force a named preset as the default),
`--resume` (skip completed nodes, retry failed ones — see Debug),
`--rerun-all` (full replay of every node), `--resume-from <node>`
(re-run node + downstream; implies `--resume`),
`--log <path>`.

`sqrlly graph <config>` prints a Mermaid topology diagram of the
**static** compiled LangGraph — dynamic `route:` `goto` targets are
emitted as `Command(goto=...)` at run time and are not drawn, so branch
targets may appear as unconnected nodes there. `sqrlly view <config>`
writes a self-contained interactive HTML viewer that *does* draw
declared `route:` edges (dotted, labeled with the `when` predicate) and
fan-out parents (hexagon); only realized per-manifest fan-out children,
created at run time, are absent.

## Debug a run

- Read the `--log` JSONL stream — events: `workflow_start`,
  `node_model` (LLM nodes: the `preset` + `model` that ran the node),
  `node_completed`, `node_failed`, `gate_evaluated`, `node_retried`,
  `workflow_end`. The node id is the `node` field (not `node_id`).
  Subgraph events are prefixed `parent::child` (one level — a deeper
  nest shows the *immediate* parent, so keep child ids unique across
  sibling subgraphs). Note: events carry status/score, not the node's
  full output text — capture that from the node itself if you need it.
- A failed `run` exits non-zero and lists the failed nodes.
- A node that keeps retrying is failing its gate — inspect the
  `gate_evaluated` events for the `score` (and, for a `dimensions:`
  gate, per-dimension `scores` plus `dimension_thresholds` — the
  configured per-dimension floors, so you can see which dimension
  blocked without reading the YAML).
- `--dry-run` traces topology without calling backends or running
  scripts; use it to confirm the graph shape before a real run.
- `--resume` reseeds from `<workdir>/.sqrlly-checkpoint.db` and **skips
  nodes that completed cleanly** and aren't downstream of a failure —
  completed work is not re-billed. `--rerun-all` forces full replay of
  every node (pre-0.6 behavior). `--resume-from <node>` re-runs a
  specific node and everything downstream (implies `--resume`). **v1
  limitation:** a subgraph re-runs in full unless its reference node
  completed cleanly; inner nodes aren't individually skippable.

## Footguns

- **Hyphens in node ids** — `{{my-id}}` parses as subtraction in a
  Jinja template. Always use underscores. `validate` warns about this.
- **`extra="forbid"`** — an unknown key on any model is a hard error.
  Confirm exact field names in `SCHEMA.md`.
- **Inline-route nodes are DAG leaves** — nothing may `depends_on` a
  node that has a `route:` block.
- **Exactly one default preset** — if any `LlmPreset` exists in
  `settings.presets`, exactly one must have `default: true`.
- **Subgraphs share the schema** — a subgraph `.yaml` is an ordinary
  workflow and must validate standalone.
- **Workflow YAML is trusted input** — `file://` / local paths are NOT
  confined to the workdir: an absolute (`/etc/…`) or `../`-relative
  `execute.url` or `validator` reads/runs with the orchestrator
  process's full filesystem access, and `allow_remote_scripts` gates
  only *remote* schemes. Don't run workflow files you don't trust.
- **`prisma generate` in `worktree_setup` needs a matching exclude** —
  a `prisma generate` command without its output path in
  `worktree_setup_exclude` lets the generated client leak into the
  promote footprint; `validate` warns about this. Add the output dir
  (e.g. `src/generated/prisma`) and `node_modules` to
  `worktree_setup_exclude`.
- **The worktree exclude is the base repo's shared `info/exclude`** —
  git has no per-worktree exclude file
  (`git rev-parse --git-path info/exclude` resolves to the common git
  dir), so the `worktree_share` / `worktree_setup_exclude` write mutates
  the one `.git/info/exclude` shared by the base repo and all worktrees.
  Those entries persist after the run and are not reclaimed by
  `worktree_gc`; harmless because `info/exclude` only affects
  **untracked** paths, so it can never mask edits to tracked files.

## Reference

- Install this skill into a repo: `sqrlly init --skill` → writes
  `.agents/skills/sqrlly/SKILL.md` at the repo root (repo-aware).
- Full schema: `SCHEMA.md`
- Worked examples: `examples/` — start with `examples/jokes/`.
