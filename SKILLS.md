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

## Author a workflow

Write a YAML file with `name`, `version`, `nodes`, and `settings`.

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
   `settings.presets` is required — there is no environment auto-detect.
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
6. **Branch** with a `route:` block (`goto: <id>` unconditional, or a
   `cases:` / `else:` predicate ladder) or **parallelize** with a
   `fan_out:` block (one branch per item in a JSON manifest).

Do not guess field names. Every model is `extra="forbid"` — a typo'd
key is a hard validation error. The exhaustive field reference is
`docs/schema-reference.md`.

## Validate and run

Always validate before running:

```bash
sqrlly validate path/to/workflow.yaml
sqrlly run path/to/workflow.yaml --log run.jsonl
```

`validate` compile-checks the graph and reports the node count plus any
advisory warnings. `run` executes it.

`run` flags: `--workdir/-w <dir>`, `--dry-run` (trace topology without
executing), `--preset/-p <name>` (force a named preset as the default),
`--resume` (continue from the last checkpoint), `--log <path>`.

`sqrlly graph <config>` prints a Mermaid topology diagram;
`sqrlly view <config>` writes a self-contained interactive HTML viewer.

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

- Full schema: `docs/schema-reference.md`
- Architecture: `TECHNICAL.md`
- Worked examples: `examples/` — start with `examples/jokes/`.
