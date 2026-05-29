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
   ```
   There is no environment auto-detect. A workflow whose nodes are all
   script / binary / subgraph can omit `settings.presets` (or set it to
   `{}`); any **LLM (prompt) node** needs at least one preset with
   `default: true`, or dispatch fails at run time.
5. **Add a gate** to retry a node until its output is good enough:
   ```yaml
   evaluation:
     validator: gates/check.py   # .py / .js script, or .md LLM prompt
     threshold: 0.8
     blocking: true
     max_retries: 3
   ```
   A script gate reads the node output on stdin and prints a score
   (`0.85`) or a JSON object (`{"score": 0.6, "feedback": "..."}`).
6. **Branch** with a `route:` block — an unconditional `goto:`, or a
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
7. **Parallelize** with a `fan_out:` block — one branch per manifest
   item, joined by `final_nodes`:
   ```yaml
   fan_out:
     manifest_path: items.json   # JSON: [{"id": "a", ...}, ...] or {"items": [...]}
     template:
       execute:
         url: prompts/item.md     # rendered once per item
       # evaluation: ...          # optional: gate each branch
     final_nodes: [summarize]     # run once after all branches join
   ```
   Each manifest item is a JSON **object**; its fields become template
   variables (`{{id}}`, `{{name}}`, …). A bare string item is treated
   as `{"id": "<string>"}`. The manifest may instead be produced at
   run time as this node's JSON output, with `manifest_path` as the
   static fallback.

Do not guess field names. Every model is `extra="forbid"` — a typo'd
key is a hard validation error. The exhaustive field reference is
`docs/schema-reference.md` (online:
https://github.com/christopherseaman/sqrlly/blob/main/docs/schema-reference.md).

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

**File resolution / git.** `execute.url` and `validator` paths resolve
relative to `--workdir` (default the current directory). When the
workdir is a git repo, runs execute inside a fresh git worktree of
`HEAD`, so **commit your workflow and the files it references first**,
and run from the repo root (a repo with no commits can't create a
worktree). Outside a git repo, worktree isolation is off and paths
resolve directly under `--workdir`.

`run` flags: `--workdir/-w <dir>`, `--dry-run` (trace topology without
executing), `--preset/-p <name>` (force a named preset as the default),
`--resume` (continue from the last checkpoint), `--log <path>`.

`sqrlly graph <config>` prints a Mermaid topology diagram;
`sqrlly view <config>` writes a self-contained interactive HTML viewer.
Both show the **static** topology — dynamic `route:` `goto` targets
are emitted as `Command(goto=...)` at run time and are not drawn as
edges, so branch targets may appear as unconnected nodes.

## Debug a run

- Read the `--log` JSONL stream — events: `workflow_start`,
  `node_completed`, `node_failed`, `gate_evaluated`, `node_retried`,
  `workflow_end`. Subgraph events are prefixed `parent::child`.
- A failed `run` exits non-zero and lists the failed nodes.
- A node that keeps retrying is failing its gate — inspect the
  `gate_evaluated` events for the score and feedback.
- `--dry-run` traces topology without calling backends or running
  scripts; use it to confirm the graph shape before a real run.
- `--resume` restarts from the last checkpoint at
  `<workdir>/.sqrlly-checkpoint.db`.

## Footguns

- **Hyphens in node ids** — `{{my-id}}` parses as subtraction in a
  Jinja template. Always use underscores. `validate` warns about this.
- **`extra="forbid"`** — an unknown key on any model is a hard error.
  Confirm exact field names in `docs/schema-reference.md`.
- **Inline-route nodes are DAG leaves** — nothing may `depends_on` a
  node that has a `route:` block.
- **Exactly one default preset** — if any `LlmPreset` exists in
  `settings.presets`, exactly one must have `default: true`.
- **Subgraphs share the schema** — a subgraph `.yaml` is an ordinary
  workflow and must validate standalone.

## Reference

- Install this skill into a repo: `sqrlly init --skill` → writes
  `.agents/skills/sqrlly/SKILL.md` at the repo root (repo-aware).
- Full schema: `docs/schema-reference.md`
- Architecture: `TECHNICAL.md`
- Worked examples: `examples/` — start with `examples/jokes/`.
