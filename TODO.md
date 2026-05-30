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
