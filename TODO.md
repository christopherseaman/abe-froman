# TODO

Review-surfaced fixes deferred for focused work. Distinct from
`WISHLIST.md` (feature wants) — everything here is a known defect or
cleanup with a diagnosis attached, deferred because the fix is
non-trivial or a judgment call.

Source: full-repo review, 2026-05-20 (four-layer agent review).

---

## Builder-required functionality gaps

Prioritized missing functionality blocking the downstream **builder**
(see its `BUILDER-REQUESTS.md`). These are feature gaps, not defects —
listed here for visibility alongside the deferred work. Several overlap
existing `WISHLIST.md` items; this is the builder-facing view.

### B1 — No direct-API backend

Planning docs claim it "landed," but the repo stripped Anthropic /
OpenAI / DeepSeek backends in the 0.2.x transport rework — only Claude
Code via `acp` / `cli` remains; `transport: api` is roadmap. Fine for
the builder today (already direct-CLI), but the "use the direct SDK to
dodge ACP process leaks" plan is **not currently available**.

### B2 — `--resume` re-executes completed nodes

Fault-recovery, not skip-completed: completed nodes re-run (LLM phases
diverge on re-run), expensive for multi-hour builds. Known limitation —
`WISHLIST.md` items 26/31 (the `--resume` rewrite). Behavior now
documented in README/SKILLS/CLAUDE; the skip-completed fix is the open
work.

### B3 — No Foreman branch/commit/octopus-merge

Worktrees exist, but the git lifecycle the builder needs — the
`build-<N>-snapshot-*` branch convention and merge-on-acceptance
(octopus merge of accepted branches) — is **unbuilt**. BUILDER-REQUESTS
flags it Phase-9-required; substantial.

### B4 — No worktree / branch GC

Foreman never removes worktrees and there's no branch cleanup, so
snapshot-branch / worktree proliferation is a real risk once B3 lands.
(Today, stray worktrees under `<workdir>/.sqrlly/` accrue and are
cleaned manually.)

### B5 — `output_contract` globs are literal

`required_files` entries are checked verbatim — no glob expansion (now
documented). The builder's directory / glob `requiredFiles` need the
"flexible output contracts" item (Phase 5).

## Low-priority / judgment calls

### 🤞 U1 (residual) — `file://` URLs still bypass script + workdir gates

`runtime/url.py`. The `max_remote_fetch_bytes` size cap now applies to
`file://` reads, but two gaps remain: `file://` still skips
`allow_remote_scripts`, and there is no path-within-workdir
confinement — `url: /etc/passwd` reads unconfined. Low severity under
the current trust model (a workflow author already controls what
executes), but a real robustness gap if workflow YAML ever comes from
a less-trusted source. Workdir confinement is the larger of the two
and deserves its own design pass.

The behavior is now **explicitly documented** as trusted-input (no
`file://` confinement) in `docs/schema-reference.md`, `TECHNICAL.md`,
and `SKILLS.md` — so it's no longer a silent gap; only the
confinement *fix* remains deferred here.

### 🤞 V1 — terminal-compatible graph visualization

`sqrlly graph` emits raw Mermaid (needs an external renderer) and
`view` writes HTML — neither renders in a terminal, and both show only
static topology (route `goto` edges are runtime `Command(goto=...)`,
not drawn). Explore rendering a terminal-friendly diagram, e.g. via
[beautiful-mermaid](https://github.com/lukilabs/beautiful-mermaid), and
ideally overlay the route edges the static graph omits. DX nice-to-have.

Package it as an **optional `[viz]` extra**, mirroring `[acp]`: the
renderer dep stays out of the core install, the `graph`/`view` command
lazy-imports it (clear "install `sqrlly[viz]`" error if absent), and
the layer/lazy-import pattern follows `factory._build_acp`.

---

## Adapter-port findings (2026-05-30)

Surfaced by using sqrlly **as a tool** to run a real workflow (claude-flow
adapter spine on pawswipe EARS inputs) with native LLM gates + routing.
Source detail: the consumer repo's `adapter-sqrlly/FINDINGS.md`. **No
remaining blockers** — the shared-FS port runs green. These are the
sqrlly-side improvements that would let **file-producing fan-out run
under worktree isolation** + assorted papercuts. Resolved upstream
(verified): H1 permission → 0.5.0 `permission_mode`; per-node model
logging → 0.5.1 `node_model`; fan-out `final_nodes` gate retry → 0.5.0;
per-gate model → `evaluation.model`.

### 🤞 AP1 — worktree-aware fan-out manifest source (highest value)

`fan_out` manifest sources (`compile/_manifest.py`): (a) raw `json.loads`
of the parent node's **entire** stdout (no fence/embedded-array
tolerance); (b) `manifest_path` resolved against `state["workdir"]` (the
**base** workdir, `_manifest.py:88`) — not a node worktree. Under
isolation neither works for a file-producing parent: (a) an LLM parent
that also writes files can't reliably emit whole-stdout-as-array
(2-leaf smoke → "manifest resolved to zero items"); (b) the parent
writes `manifest.json` into its **worktree**, but `manifest_path` reads
the base workdir, so they never meet. **Desired:** a worktree-aware
`manifest_path` / `manifest_from_file:` that resolves against the
**parent's worktree**, so a fan-out consumes a manifest the parent
*writes* (structured artifact, not LLM stdout). Also: have
`_read_manifest` extract a fenced/embedded JSON array from stdout
(defensive), not require bare-JSON whole output.

✅ **Partially done (0.5.2):** `_read_manifest` now strips a ``` fence /
extracts the embedded JSON array-or-object from stdout. The
**worktree-aware `manifest_path`** (resolve against the parent's
worktree) is the remaining, higher-value half.

### 🤞 AP2 — worktree isolation is implicit-on and silently breaks shared-file workflows

`_is_git_repo(workdir)` (walks up) → per-node `ForemanExecutor`
isolation. Correct for stdout/`node_outputs` data-flow, but a workflow
that flows data through a **shared file tree** (adapter's `prd/`) gets
each node its own worktree → downstream nodes don't see upstream files.
**Fails silently** (fan-out sees 0 items, gates score empty), not a
crash. Only opt-out today is a non-git workdir (external staging).
**Recommended (composes):** (1) a loud warning/error when >1 node
targets the same `output_directory` / `output_contract.base_directory`
under isolation; (2) an explicit `settings.worktree_isolation: false`
opt-out so a shared-FS workflow declares intent in YAML and runs in-repo.
Related to B3/B4 (foreman git lifecycle) but distinct.

### 🤞 AP3 — no cross-worktree output collection / tree assembly

Under isolation, files a node writes live in its worktree; no native
way to gather them into one output tree (`foreman.py:17-19`:
author-written reconciliation nodes; `output_contract` validates
in-tree only). A workflow whose **deliverable is a directory of files**
can't materialize it without a bespoke assembly node or shared workdir.
**Suggested:** a first-class "collect outputs" step (auto-copy each
worktree's `output_contract` files into `settings.output_directory`),
or a documented assembly-node recipe. NOTE: solvable consumer-side
(harvest `.sqrlly/wt-*/<dir>` by path) → convenience, not blocker.
Pairs with AP2.

### 🤞 AP4 — fan-in worktree pairing is positional

A fan-in node gets child outputs as `{{<parent>_branches}}` (id→output
**dict**) but worktree paths as `{{<parent>_branch_worktrees}}` (a bare
**list** of paths — `compile/nodes.py:133-142`). Pairing a child's
output to its worktree relies on **implicit order**. **Fix:** key the
worktree map by child id, or fold both into `{id: {output, worktree}}`.

### 🤞 AP5 — schema papercuts (C1/C5)

- **C1 ✅ (0.5.2):** `output_contract` added to `FanOutFinalNode` and
  threaded into the final node's synthetic `Node`, so it's enforced via
  the standard execution path.
- **C5:** `depends_on` can't name an inline fan-out final-node id
  (`_validate_depends_on` only knows top-level ids; workaround: depend on
  the fan-out parent). Fix: allow/document depending on a final-node id.

### 🤞 AP6 — smaller papercuts

- **Gate result schema** — add optional `retry_recommended`
  (fail-fast, no retry) and `critical` (immediate hard fail) keys to the
  gate JSON.
- **`required_files` globs** — literal today; support globs (== B5 /
  the "flexible output contracts" builder want).
- **`--version` flag ✅ (0.5.2)** — `@click.version_option` on the CLI
  group (reads package metadata).
- **Py 3.14 langchain warning** — `Core Pydantic V1 functionality…` on
  every `uv run`; cosmetic now, real if langchain drops the V1 shim;
  pin/track the dep.
- **`DEPS_JSON` arg-size (usage note, optional hardening)** — script
  gates (`gates.py::run_evaluation_script`) pass all upstream outputs as
  the `DEPS_JSON` **env var**; on a large fan-out final node this can
  exceed `MAX_ARG_STRLEN` (~128 KiB) → `execve E2BIG`. Workaround: use
  `.md` LLM gates (read files), or pass `DEPS_JSON` via tempfile/stdin.
