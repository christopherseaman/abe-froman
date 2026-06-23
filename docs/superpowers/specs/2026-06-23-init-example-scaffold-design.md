# `sqrlly init --example` — scaffold a bundled example

**Status:** approved 2026-06-23

## Problem

`sqrlly init` exists for users who installed via `pipx`/`uv tool install` and have no repo checkout. Those users cannot run any bundled example — `sqrlly run examples/jokes/workflow.yaml` only works from a checkout, and examples do **not** ship in the wheel today (verified: a built wheel has 50 entries, zero examples). `init --example <name>` closes that gap: scaffold a real, runnable example onto disk.

## Decisions (settled during brainstorming)

- **Delivery: bundle in the main wheel.** Python extras gate optional *dependencies* (other packages), not data files inside a wheel — so an `[examples]` extra cannot conditionally add `examples/` to the `sqrlly` wheel (that would require a separate `sqrlly-examples` package). The curated run-essential set is ~32K, so a separate gated package is not worth its ongoing release overhead. Reserve the separate-package + `[examples]`-extra route for if/when heavy showcase examples (e.g. `absurd-paper`, 256K + acp/PDF tooling) are wanted on demand.
- **Curated set (run-essential files only):**
  - `jokes` — LLM prompt + quality gate + multi-node (the front door). Needs an authenticated `claude` CLI (the `cli`-transport default).
  - `route_classify` — inline routing on structured output. Pure script (python + echo) — no backend.
  - `explicit_join` — fan-in / join topology. Pure script (echo) — no backend.
  - `pipeline_style` — forward-edge `route: goto` authoring. Pure script (echo) — no backend.
  - Three of four are pure-script and run with nothing beyond `sqrlly` installed (no backend, no auth). Only `jokes` needs an authenticated `claude` CLI — sqrlly's `cli`/`acp` transports authenticate via the local Claude tooling, not an API key (the `api` transport was removed).
- **Path self-containment via scaffold-time rewrite.** Repo workflows use root-relative URLs (`url: examples/jokes/generate.md`). At scaffold time, strip the `examples/<name>/` prefix from `url:` / `validator:` paths so the scaffolded copy is flat and self-contained (`url: generate.md`), runnable from its own directory. One source of truth (repo examples); transform happens at scaffold.

## CLI surface

Extends the existing `init` Click command (`cli/main.py` + `cli/init.py`):

- `sqrlly init --example <name> [directory]` — scaffold `<name>` into `directory` (default `./<name>/`). Refuses to clobber an existing target file; prints `cd` / `validate` / `run` next-step hints (matching current `init`). Unknown `<name>` → error listing available examples.
- `sqrlly init --list-examples` — print the bundled example names with a one-line description each.
- Existing `init` (bare) and `init --skill` behaviors are unchanged.

## Components

- **Curated catalog** — a name → (description, file list) table in `cli/init.py`. The file list names the run-essential files per example (relative to the example dir). Single source of the curated set.
- **Resource loader** — read each file from the packaged resource `importlib.resources.files("sqrlly") / "_examples" / <name> / <relpath>`; in a source checkout (resource absent) fall back to the repo `examples/<name>/<relpath>` by walking up from the module (same fallback shape as `_load_skill_doc`).
- **Scaffolder** — copy the catalog's files into the target dir (creating subdirs like `gates/`, `scripts/`), and for the workflow YAML rewrite `examples/<name>/` → `` on `url:`/`validator:` path values so the scaffold is self-contained.
- **Packaging** — `force-include` the curated run-essential files into the wheel under `sqrlly/_examples/<name>/…` (mirroring `SKILLS.md → sqrlly/_skill.md`).

## Data flow

`init --example jokes ./j` → resolve catalog entry `jokes` → for each listed file, load bytes from `_examples/jokes/<f>` (or repo fallback) → write to `./j/<f>` (mkdir parents) → rewrite `./j/workflow.yaml` URL prefixes → print hints (`cd j`, `sqrlly validate workflow.yaml`, `sqrlly run workflow.yaml`).

## Error handling

- Target file already exists → `ClickException` (don't clobber), as current `init`.
- Unknown example name → `ClickException` listing valid names.
- Neither packaged resource nor repo fallback found for a listed file → `ClickException` (packaging/catalog drift; surfaced loudly).

## Testing

- **Unit:** `init --example jokes <tmp>` creates exactly the catalog's files; the scaffolded `workflow.yaml` contains no `examples/jokes/` prefix (rewrite applied) and the original URL basenames survive; clobber refused; unknown name errors; `--list-examples` lists every curated name.
- **E2E:** scaffold each curated example into a tmp dir and run `sqrlly validate <tmp>/workflow.yaml` (for `explicit_join`, the single YAML) — must pass, proving the rewrite yields a resolvable, self-contained workflow. Run a no-backend one (`explicit_join`) end-to-end with `sqrlly run` to confirm it executes.
- **Packaging:** assert the curated files are present under `sqrlly/_examples/` in a freshly built wheel (build + zip-list), guarding against force-include drift.
- No mocks — real filesystem, real `sqrlly validate`/`run` subprocess or in-process invocation per repo testing doctrine.

## Out of scope

- Heavy examples (`absurd-paper`, `wave_planner`, `command_preset`, `smoke_test`, `run_all_examples`).
- A separate `sqrlly-examples` package / `[examples]` extra (revisit only for heavy content).
- Fetching examples over the network.
