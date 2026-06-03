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

### B3 — Octopus-merge of overlapping isolated trees (DEFERRED — the one rabbit hole)

The 2026-06-02 design narrowed this to its true residual: reconciling
**multiple** independently-isolated worktrees whose diffs **overlap**
(real 3-way / octopus merge). Single-source consolidation is handled by
**promotion** (git-delta apply, see below), so B3 is no longer "the
join engine" — it is only the overlap case. Deferred until a concrete
workflow proves it unavoidable; even then likely a content-aware
acceptance gate, not blind `git merge`. The `build-<N>-snapshot-*`
branch convention is a builder Phase-9 concern, not v2.

### ✅ Promotion — git-delta apply, single-source (supersedes AP3, SHIPPED v2)

`Node.promote` applies one worktree's git delta vs its fork point to the
base (`runtime/promote.py`): **discover by default** (full delta incl.
edits/deletes — handles unanticipated footprints like a bugfix);
**glob-filterable** via `output_contract.required_files` (git pathspec).
Single-source onto an unmoved base = clean; multi-source-overlap = B3
(deferred). Runs before GC, top-level nodes only. Build plan:
`docs/superpowers/plans/2026-06-02-worktree-v2-lifecycle.md`.

Residual low-pri: (1) when several nodes share a `worktree_group` and
each sets `promote`, the shared tree is promoted once per member
(idempotent double-copy) — dedupe by tree path if it ever matters;
(2) `promote` on fan-out children / subgraph inner nodes is not wired
(top-level only) — extend if a consumer needs it; a `collect_warnings`
advisory for `promote` on a fan-out/group node would close the footgun.

Advisory test gaps (each mechanism tested in isolation; combinations
inferred): promote+GC in one run; `worktree_group` across `--resume`
(deterministic `wt-group-<name>` + per-member `node_worktrees`
rehydrate). Add integration coverage when convenient.

### B4 — No worktree / branch GC

### B4 — No worktree GC (PLANNED v2)

Foreman never removes worktrees, so proliferation is a real risk once
groups multiply trees. Design (2026-06-02): opt-in
`settings.worktree_gc: never` (default) `| on_success`, **end-of-run
only** (never mid-run — preserves retry-reuse and resume-rehydrate),
reclaiming each *distinct* tree (per-node + each group tree once, from
`foreman.worktree_map()`). On failure, keep everything so `--resume`
works. Build plan:
`docs/superpowers/plans/2026-06-02-worktree-v2-lifecycle.md`.

### B5 — `output_contract` globs are literal (folded into promotion)

`required_files` are checked verbatim. The 2026-06-02 design makes the
contract **glob-aware** (git pathspec) as part of promotion's
filter-mode — one matcher for declared globs and `git diff` discovery.
Semantics shift: a glob means "at least one match exists" (a literal
path is still a valid glob matching itself, so existing contracts are
unaffected). Tracked in the worktree-v2 build plan.

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

### ✅ AP2 — worktree isolation control (DONE, v1 / 0.5.3)

Was: implicit-on isolation silently broke shared-file workflows; the
only opt-out was a non-git workdir. **Shipped in 0.5.3:**
`settings.worktree` + `Node.worktree` (`auto` / `isolated` / `off`),
inherited via `merge_settings`. `off` runs the node in the shared base
workdir (and, since gates run there too, lets a script gate read the
node's files directly). The silent-degrade opt-out is closed; the
explicit-intent escape hatch is `worktree: off`. (The >1-node-shared-
dir lint was not built — the explicit field makes intent declared, so
the lint is lower value; revisit if needed.)

### ✅ AP3 — cross-worktree output collection (SUPERSEDED by promotion)

The "auto-copy each worktree's `output_contract` files into
`output_directory`" idea is replaced by **git-delta promotion** (see
the Promotion entry above): apply a worktree's git diff to the base,
discover-by-default, glob-filterable. File-copy survives only as the
non-git fallback. The 2026-06-02 design retired AP3-as-copy because
copy can't express deletions or unanticipated footprints.

### 🤞 AP4 — fan-in worktree pairing is positional (PLANNED v2)

A fan-in node gets child outputs as `{{<parent>_branches}}` (id→output
**dict**) but worktree paths as `{{<parent>_branch_worktrees}}` (a bare
**list** of paths — `compile/nodes.py:133-142`). Pairing a child's
output to its worktree relies on **implicit order**. **Fix:** add a
keyed `{{<parent>_branch_map}}` = `{id: {output, worktree}}` (additive;
keep the legacy list). **Blocker first:** fan-out children never write
`node_worktrees` (`dynamic.py` `exec_update` omits it vs `nodes.py:654`),
so `{{<parent>_branch_worktrees}}` renders `[]` under isolation today —
close that write gap as part of v2. Tracked in the worktree-v2 build
plan.

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
