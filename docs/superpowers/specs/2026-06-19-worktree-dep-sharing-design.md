# sqrlly #3 — Worktree Dependency Sharing — Design (for review)

**Date:** 2026-06-19

**Status:** **Reviewed 2026-06-19 — the five open questions are resolved (below); ready for build.** Produced by a prior-art + red-team design workflow, then an operator review.

**Origin:** builder-sqrlly request #3 / TODO B6 (`../samus-ai/builder-sqrlly/SQRLLY_REQUEST.md`).

> **TL;DR.** The **`.git/info/exclude` write is the mechanism-independent, load-bearing fix** (a promote-footprint correctness fix, not a node_modules-sharing fix). On top of it, expose **two** sharing paths: a cheap **read-only whole-dir symlink** (no in-worktree install — correct for read-only gates like the builder's `tsc --noEmit`) and the general **rehydrate** path (`settings.worktree_setup` runs pnpm's own install per worktree — earns its cost only when a branch needs write-isolation or dependency mutation). Rehydrate is hardened by the four red-team-mandated pieces: the **sentinel**, the **`.git/info/exclude`** write, **GC-registration before setup**, and a **same-device store**.

---

## Decisions (resolved in operator review — 2026-06-19)

1. **Marker scope → hash the BASE; mutators regenerate in the gate body.** The setup sentinel hashes the *base* lockfile+schema. Documented contract: a branch that mutates `schema.prisma` runs `prisma generate` in the **gate/build body, not setup** — with per-worktree `node_modules` that writes to the branch's own client, so it's safe. Setup stays pure idempotent base hydration (one job, not two). **Do not** hash the worktree's own schema. (Moot for the builder — shared-seam-first means branches never touch `schema.prisma` — but the generic contract is base-hash + gate-body-regenerate.)

2. **Exclude paths → explicit, NO auto-derive; a loud `validate` lint carries footgun-protection.** Parsing `output=` out of `schema.prisma` would bake Prisma-version-specific path logic (relative/absolute/env-interpolated, multiple generators) into sqrlly source — the exact PM-coupling the design otherwise keeps in user YAML. Keep `worktree_setup_exclude` explicit; the `validate`-time lint (`prisma generate` in setup without a matching exclude → warn loudly) catches the mistake without source owning the inference.

3. **Setup failure → FATAL for the branch (blocking), not non-fatal.** Non-fatal converts an infra failure into a *misleading gate failure*: absent deps → `tsc` "cannot find module X" → looks like a code bug → the build node's self-heal burns real tokens trying to fix an unfixable missing-module (exactly the "gate fails for the wrong reason → wasted retries" class we hit this session). A clear `setup failed: <cmd> exit N` is diagnosable and stops the bleed. Scope it to **fail the branch** (per-feature isolation — other valid branches proceed), not abort the whole fan-out. Keep the `validate` pre-flight sync check. Non-fatal stays an explicit opt-in for those who knowingly want it.

4. **No dedicated setup semaphore by default — lean on pnpm's store lock + `max_parallel_jobs`.** pnpm's store mutex already serializes the dangerous part (cold-package store writes); setup concurrency is naturally bounded by branch concurrency (`max_parallel_jobs`, =4 in the builder). With a warm store (which shared-seam-first guarantees — branches build features against existing deps, they don't add packages), contention is near-zero. A separate semaphore is a second concept + config for a problem pnpm mostly owns — defer it to a tuning knob if measured contention appears. Document the "store must be warm" precondition (done).

5. **Ship `promote_exclude` as a general `Settings` field — not scope-creep.** Promote is the single highest-risk operation (silent last-write-wins = request #1; data loss on a green run). The two mechanisms are NOT redundant — different layers: the `.git/info/exclude` write filters `git status` (filesystem level); `promote_exclude` filters the promote pathspec in `cli/main.py` (promote level). If a generator writes a path the exclude-write missed, the promote-layer filter still catches it before `shutil.copy2` follows it into base. Small, orthogonal, reusable by any promoting node, cheap insurance on the operation you most can't afford to get wrong. Ship independently.

---

## Two paths + the universal fix (review refinement)

**The `.git/info/exclude` write is mechanism-independent.** Of the four "mandatory" pieces, the exclude-write is load-bearing *regardless of which sharing mechanism wins* — it is a promote-footprint correctness fix, not a node_modules-sharing fix. Even the cheap interim path below needs it. **Highest-confidence takeaway; adopt it first.**

**Expose both a read-only symlink path AND rehydrate.** The workflow red-teamed the *general* symlink-share into the ground, but the variant validated this session (T1) is narrower than what it rejects: a *whole-dir, read-only* symlink with *no `pnpm install` in the worktree*. That sidesteps both surviving objections — per-child fragility (we never touch pnpm's farm) and pnpm-refuses-install-on-symlinked-`node_modules` (we never install). The only objection that survives against the read-only variant is the `?? node_modules` footprint leak — which the `.git/info/exclude` write fixes anyway. Therefore:

- **Read-only in-branch gate (e.g. the builder's `tsc --noEmit`, scoped tests):** a whole-dir symlink + the exclude-write is correct and avoids the per-branch install + `prisma generate` cost the doc honestly flags as "not near-instant."
- **Rehydrate** earns its cost when a branch needs write-isolation or dependency mutation — which shared-seam-first explicitly prevents in the builder.
- **Recommendation to the sqrlly dev:** ship rehydrate for generality, but consider exposing **both** — a `worktree_share`-style read-only symlink alongside the `worktree_setup` rehydrate — rather than forcing every consumer to pay install-per-branch. **The builder can adopt the exclude-write for its current symlink now, ahead of the full feature.**

---

## Recommended design

Adopt the **per-worktree package-manager rehydrate** mechanism — a generic, ordered `settings.worktree_setup` command list that sqrlly runs in each fresh worktree so pnpm builds its **own** real `node_modules` against the warm global store and `prisma generate` writes into that worktree's own tree — hardened with the four pieces the red-team proved mandatory: (1) a **setup-completion sentinel** keyed on the base lockfile+schema hash, checked on *every* path that can hand back a worktree (`_create_worktree`, the group `is_dir()` early-return, and the `--resume` rehydrate early-return), so a half-built or stale tree can never serve a gate; (2) a **sqrlly-owned `.git/info/exclude` write** for the configured artifact paths (`node_modules`, plus any in-tree Prisma `output=` path), because the consumer's `.gitignore` cannot be trusted (anchored `/node_modules/`, monorepo `packages/*/node_modules`, in-tree Prisma clients all leak into the promote footprint otherwise); (3) **GC-path registration before setup runs** plus bounded retry on transient install failure, so a failed install never orphans a tree; (4) a **store-on-same-device** requirement (point pnpm's store under `<base>/.sqrlly/.pnpm-store`) so hardlinks actually work instead of EXDEV-copying the whole tree per branch. This is the only candidate that *dissolves* rather than *relocates* both the pnpm-resolution and the Prisma-race problems, and it carries no FUSE/overlay/reflink kernel dependency.

## Why it actually works

**pnpm `.pnpm` symlink farm.** sqrlly never touches the layout. pnpm itself runs `pnpm install` inside the worktree and writes the worktree's own `node_modules/.pnpm/<pkg>@<ver>/node_modules/...` virtual store plus the top-level relative symlinks into it. Relative-path resolution from `.pnpm` is correct *because pnpm authored it*, not because we re-pointed children — so the prior design's per-child-symlink fragility cannot arise. The `node_modules` root is a **real directory**, which sidesteps pnpm issue #9973 (pnpm refuses to install when `node_modules` is a symlink — the failure mode that kills every symlink-share scheme). Byte sharing happens one layer down inside pnpm's content-addressed store via hardlinks — *provided the store is on the same device* (see fix #4).

**Per-branch `prisma generate`.** Each worktree owns a distinct `node_modules`, so the legacy `node_modules/.prisma` + `@prisma/client` client lands in *that* tree. Parallel branches write disjoint paths — the mutation race is structurally gone, not serialized. For the modern in-tree `output=` generator the client lands in the worktree's checked-out tree, also per-branch; but that path is **not** automatically clean for promote (see below).

**Clean promote footprint.** A real-directory `node_modules` is hidden by a canonical `node_modules/` rule, so `discover_changes`' `git status --porcelain=v1 --untracked-files=all` stays empty *in the simple case*. But the red-team proved this is **not** free: anchored `/node_modules/`, monorepo `packages/*/node_modules`, churned `pnpm-lock.yaml`, and in-tree Prisma clients all leak. The design therefore makes sqrlly **write its own `.git/info/exclude`** entries (bare basenames + the configured Prisma output path) into the per-worktree exclude file, independent of the consumer's `.gitignore`. That keeps every machine-generated artifact out of the footprint so `apply_changes`' `shutil.copy2` never follows anything into base.

**`git worktree remove --force` GC.** The worktree's `node_modules` is a real dir of hardlinks (delete = refcount drop; store untouched) or symlinks into the store (delete never follows). Base deps and the global store survive. No mounts to unmount, no EBUSY, no daemonized-FUSE orphan, no teardown ordering — the existing `reclaim()` path works unchanged except for the orphan-registration fix.

## Prior-art lineage

- **pnpm's official "Git Worktrees for Multi-Agent Development" recipe** — bare clone, `git worktree add` per branch, `pnpm install` per worktree against the shared store. This is the documented, supported shape; we are not inventing a sharing scheme, we are wiring sqrlly into pnpm's own answer.
- **Content-addressed store + symlink-farm discipline (Nix `/nix/store`, Bazel execroot forest, pnpm store).** The unifying lesson: the store is immutable and read-mostly, each consumer gets a cheap *view*, and you **never hand-roll a farm over the package manager's farm**. The prior per-child-symlink hybrid violated exactly this; the rehydrate approach respects it.
- **The "real directory, not symlink" footprint insight** comes from empirical work on git 2.53: a dir-slash ignore rule matches a real dir but not a symlink — which is why every symlink-share design leaks `?? node_modules` and this one does not.

## Mechanism & sqrlly surface

**Schema (`src/sqrlly/schema/models.py`).** Add to `Settings`:

- `worktree_setup: list[str] = []` — ordered shell commands run in each fresh worktree (e.g. `["pnpm install --prefer-offline", "pnpm exec prisma generate"]`). Per-`Node` override merged via `settings_merge.py`.
- `worktree_setup_exclude: list[str] = []` — repo-relative paths sqrlly writes into the worktree's `.git/info/exclude` before promote can run (e.g. `["node_modules", "src/generated/prisma"]`). Defaults empty; documented that pnpm/Prisma users must set the Prisma output path here.
- `worktree_setup_store_dir: str | None = None` — when set, exported as `PNPM_HOME`/`store-dir` (or generically as an env prefix) so the store sits on the worktree device.
- `worktree_share: list[str] = []` — the cheap **read-only path**: whole-dir symlinks of these base paths into each worktree (no in-worktree install). For read-only gates (`tsc --noEmit`, scoped tests). Each shared path still gets the `.git/info/exclude` write. Mutually informative with `worktree_setup` — a consumer picks the path that fits (read-only gate → `worktree_share`; write-isolation/dependency-mutation → `worktree_setup`).
- `promote_exclude: list[str] = []` — **shipped per decision 5**: git-pathspec entries filtered out of *every* promoting node's discovered footprint in `cli/main.py` (promote-layer defense-in-depth beneath the filesystem-layer `.git/info/exclude` write).

Keep `worktree_setup` / `worktree_share` PM-agnostic (works for `npm ci`, `uv sync`, `bundle install`); the pnpm/Prisma specifics live in user YAML, not sqrlly source (KISS/YAGNI).

**Wiring (`src/sqrlly/runtime/foreman.py`).** The setup is gated on a **sentinel**, not on worktree existence:

1. Define `_setup_marker(dest) = dest/".sqrlly/setup-ok"` whose contents are `sha256(base/pnpm-lock.yaml + base/prisma/schema.prisma + worktree_setup)`.
2. Factor a `async def _ensure_setup(dest)` helper that: checks the marker; if absent or hash-mismatched, writes the `.git/info/exclude` lines first (so a crash mid-install still can't leak), **registers `dest` in `_worktrees` before** running commands (so a failure is reclaimable), runs each command via `asyncio.create_subprocess_exec` with `cwd=dest` and bounded retry/backoff on non-zero exit; on exhausted retry it **fails the branch** (blocking, per decision 3 — a clear `setup failed: <cmd> exit N`, not a misleading downstream gate failure), and writes the marker only on success.
3. Call `_ensure_setup(dest)` from **three** places, not one: in `_create_worktree`, immediately before `return str(dest)` (after `git worktree add ... HEAD` succeeds); on the group-tree `dest.is_dir()` early-return; and on the `_acquire_worktree` rehydrate early-return (the `--resume` path).

Because `_ensure_setup` is idempotent (marker-gated) and serialized per pool-key by the existing `_worktree_tasks` dedup, group trees run setup exactly once and resumed trees re-validate against current base state.

**Promote (`src/sqrlly/runtime/promote.py` / `cli/main.py`).** `discover_changes` reads the per-worktree `.git/info/exclude` (filesystem layer). **Per decision 5, also ship `settings.promote_exclude`** — a git-pathspec applied to every promoting node's footprint (not only `output_contract` nodes) at the promote layer in `cli/main.py`. The two are defense-in-depth at different layers, not redundant: if a generator writes a path the exclude-write missed, the promote-layer filter still stops `shutil.copy2` from following it into base. Worth shipping independently of this feature, given promote is the highest-risk operation.

## Red-team holes closed

- **Fatal: in-tree Prisma `output=` / churned lockfile leak into promote (both filesystem adversaries).** Forced the `worktree_setup_exclude` → `.git/info/exclude` write. We do *not* claim "git already isolates it"; sqrlly explicitly excludes the generated paths.
- **Fatal: group-retry skip — `git worktree add` succeeds, install fails, retry sees `.git` and returns un-set-up (integration adversary).** Forced the **sentinel** to replace `is_dir() && .git exists` as the completeness signal, and forced writing the GC registration **before** setup runs.
- **Fatal: `--resume` never re-runs setup, gates run against stale generated client (integration adversary).** Forced calling `_ensure_setup` on the rehydrate early-return, with the hash-keyed marker so a schema/lockfile change invalidates the stale tree.
- **Serious: EXDEV — store on `/dev/sda1`, worktrees on `/dev/sdb`, hardlinks fall back to full copies, "near-instant" is false (filesystem adversary).** Forced `worktree_setup_store_dir` to relocate the store onto the worktree device.
- **Serious: `--frozen-lockfile` aborts the whole fan-out on any pre-existing lockfile drift (integration adversary).** Forced the canonical command framing to `--prefer-offline` (not `--frozen-lockfile`), setup-failure-is-fatal as an explicit opt-in, plus a documented pre-flight lockfile/package.json sync check.
- **Serious: orphaned half-installed trees (filesystem adversary).** Forced GC registration before setup + bounded install retry.
- **Serious: pnpm store concurrency under cold packages (integration adversary).** Mitigated by an optional dedicated setup semaphore (distinct from `max_parallel_jobs`); documented that the store must be warm for race-free parallelism.
- **Minor: non-git/off-node silently no-ops `worktree_setup` (integration adversary).** Forced a `validate`-time warning when `worktree_setup` is set but the run will use the non-git `DispatchExecutor` path.

The two rejected candidates were forced out by attacks they could not survive cleanly: **fuse-overlayfs** dies on the *daemonized-mount-survives-crash* + *group-trees-share-one-upper-so-Prisma-still-races* + *anchored/monorepo gitignore leak* trio (mount lifecycle is a genuinely new corruption-adjacent surface), and **`cp --reflink`** dies on ext4 (this host has no reflink) to a multi-GB super-linear I/O storm per fan-out plus a temp-sibling-leaks-into-promote self-own. Rehydrate is the only mechanism with no kernel-feature dependency and no new mount/copy lifecycle.

## Residual risks

- **Consumer correctness burden.** The pnpm/Prisma footprint correctness lives partly in user YAML (`worktree_setup_exclude` must name the Prisma output path). sqrlly can warn but cannot infer arbitrary generators. A user who forgets it leaks the client into base — the `validate`-time lint should detect a `prisma generate` in `worktree_setup` without a corresponding exclude entry and warn loudly.
- **Wall-clock cost.** Even warm + same-device, `pnpm install` (lockfile read + symlink-farm build) + `prisma generate` (3-15s codegen, CPU-bound) runs per worktree, multiplied across fan-out. Real, bounded, acceptable for gating — but not "near-instant." Document the per-branch cost honestly.
- **Store warmth precondition.** A branch that adds a dependency triggers a cold-store write. Bounded by the setup semaphore but not free.
- **Schema-mutating branches.** A branch editing `schema.prisma` and re-running `prisma generate` in setup is correct *only if* the marker hash includes the worktree's (not base's) schema — as written the marker hashes *base* schema; for branches that mutate schema before the gate, `prisma generate` must run as part of the gate body, not setup. Document the boundary. (See Open Question 1.)
- **Non-pnpm consumers.** The mechanism is PM-agnostic but the footprint-cleanliness reasoning was verified for pnpm + Prisma only; other PMs need their own exclude entries.

## Validation before merge

Against the **real builder-sqrlly repo** (actual `pnpm-lock.yaml`, actual `schema.prisma`, real `node_modules` size), on this ext4 host:

1. **Store device + hardlink check.** Set `worktree_setup_store_dir` under `<base>/.sqrlly/.pnpm-store`; create a worktree; assert `stat -c %i` of a file in `node_modules/.pnpm/...` equals its store counterpart (hardlink, same inode) — proves no EXDEV full-copy. Measure wall-clock for a 4-way fan-out; assert it is link-time, not copy-time.
2. **Promote footprint empty.** After `pnpm install` + `prisma generate` (both layouts: legacy `node_modules/.prisma` AND in-tree `output=src/generated/prisma`), run the exact `git status --porcelain=v1 --untracked-files=all` from `discover_changes` in the worktree. Assert **empty**. Repeat with an **anchored** `/node_modules/` rule and a **monorepo** `packages/app/node_modules` layout — the cases that forced the exclude write; assert the sqrlly-written `.git/info/exclude` keeps them empty.
3. **Per-branch Prisma isolation.** Two concurrent worktrees, both `prisma generate`; assert each `node_modules/.prisma/client` differs only as expected and base `node_modules` is byte-for-byte untouched (`diff -r` / inode check).
4. **Crash/retry idempotency.** Kill a worktree mid-`pnpm install`; re-enter `_acquire_worktree`; assert the sentinel is absent so setup re-runs and the tree ends fully built (not the half-built `.git`-exists trap).
5. **`--resume` freshness.** Run, checkpoint, mutate base `pnpm-lock.yaml`, `--resume`; assert the marker hash mismatches and setup re-runs (gate does not validate against a stale client).
6. **GC.** `git worktree remove --force` on a fully-installed tree; assert exit 0, base `node_modules` + global store intact, no orphan under `.sqrlly`.

A regression that wrote an empty `node_modules` and let `tsc` pass-by-absence is the worst failure mode — every test above must assert a **specific** artifact (inode equality, non-empty client file, empty-vs-nonempty git status), never just exit code 0.

---

*Process note: this design came from a propose → 6-family prior-art research → 3-candidate synthesis → 2-lens red-team → synthesis workflow. The two rejected candidates (fuse-overlayfs, cp --reflink) and the four red-team-mandated hardening pieces are recorded above so the review can see what was already attacked.*
