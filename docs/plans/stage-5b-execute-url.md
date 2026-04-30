# Stage 5b — Unified `execute: { url, params }` Schema

## Context

Today's YAML carries **seven** execution shapes for "what does this
node do" — the result of incremental Stage 1–4 additions, each of
which made local sense but compounded into sprawl:

| Shape | Today's YAML |
|---|---|
| Prompt (shorthand) | `prompt_file: "x.md"` |
| Prompt (full) | `execution: { type: prompt, prompt_file: "x.md" }` |
| Command | `execution: { type: command, command, args }` |
| Gate-only | `execution: { type: gate_only }` |
| Join | `execution: { type: join }` |
| Subgraph (top) | `config: "x.yaml"` + top-level `inputs:` + `outputs:` |
| Fan-out child | `fan_out.template.prompt_file` |

Every new execution kind so far has added another discriminator path. A
Stage 4 audit confirmed this is overbuild: `FanOutTemplate.config:` was
prototyped to add subgraph dispatch at the per-child level, then
reverted as part of the audit because it doubled down on the wrong
abstraction. The right move is to collapse all of these into one
shape.

## Proposal

A single `execute:` block on every node:

```yaml
- id: my-node
  name: "My Node"
  execute:
    url: "prompts/my.md"
    params:
      model: "opus"
```

**URL extension drives the dispatcher.** No `type:` discriminator. The
dispatch table maps file extension (or URL scheme) to a runtime
handler. `params:` is mode-specific.

### Dispatch table

Dispatch is **two-stage**: (1) URL resolution produces a *resolved
URL* (with explicit protocol), (2) the resolved URL's protocol +
extension picks a handler.

| Resolved URL pattern | Mode | What it does | `params:` shape |
|---|---|---|---|
| `file://*.md`, `*.txt`, `*.prompt` | prompt | Reads the file, renders as Jinja template against context, sends to PromptBackend | `model`, `agent`, `timeout` (mode-specific overrides) |
| `https://*.md`, etc. | prompt (remote) | Fetches over HTTPS, then same as file prompt | same as file prompt |
| `file://*.yaml`, `*.yml` | subgraph | Loads as a `Graph`, recursively compiles, invokes per call | `inputs: { var: "{{template}}" }`, `outputs: { key: "{{sub_node}}" }` |
| `https://*.yaml` | subgraph (remote) | Fetches over HTTPS, then same as file subgraph | same as file subgraph |
| `file://*.py` | python script | `python <path>` subprocess; stdin = nothing, stdout = output | `args: ["{{arg1}}"]`, `env: {KEY: "{{val}}"}` |
| `file://*.js`, `*.mjs` | node script | `node <path>` subprocess | same as `*.py` |
| `file://*.ts` | typescript | `tsx <path>` or `bun <path>` subprocess (configurable) | same as `*.py` |
| `file://*.sh` | shell script | `bash <path>` subprocess | same as `*.py` |
| `https://*.{py,js,ts,sh}` | remote script | Fetches to a temp file, exec via interpreter, deletes temp on completion | same as file script |
| `file:///abs/path/to/binary` or unrecognized extension | direct exec | Treats path as a binary; spawns subprocess | `args:`, `env:` |

### URL resolution

A URL goes through this resolution before reaching the dispatch table.
Resolution is **deterministic** and runs at compile time so cycle
detection and caching see canonical URLs.

```
def resolve_url(url: str, base_url: str | None, workdir: str) -> str:
    # 1. Explicit protocol — pass through unchanged.
    if "://" in url:
        return url

    # 2. Absolute path — wrap as file://.
    if url.startswith("/"):
        return f"file://{url}"

    # 3. Relative path — resolve against base_url, else workdir.
    if base_url:
        # urllib.parse.urljoin handles both file:// and https:// bases.
        return urljoin(base_url, url)
    return f"file://{Path(workdir).resolve()}/{url}"
```

**Examples** with `settings.workdir = /home/me/proj` and various
`settings.base_url` values:

| `url:` value | `base_url:` | Resolved URL |
|---|---|---|
| `prompts/x.md` | (unset) | `file:///home/me/proj/prompts/x.md` |
| `prompts/x.md` | `examples/foo/` | `file:///home/me/proj/examples/foo/prompts/x.md` |
| `prompts/x.md` | `https://prompts.example.com/v1/` | `https://prompts.example.com/v1/prompts/x.md` |
| `/etc/scripts/run.sh` | (any) | `file:///etc/scripts/run.sh` (absolute always wins) |
| `https://x.com/y.yaml` | (any) | `https://x.com/y.yaml` (explicit protocol always wins) |
| `file:///abs/x.md` | (any) | `file:///abs/x.md` (explicit protocol always wins) |

`base_url` lives on `Settings` (top-level workflow setting). Subgraphs
**inherit the parent's resolved base_url** by default but may override
in their own `settings:` block — this lets a self-contained subgraph
declare its own root for relative refs.

### Remote URL support (http/https)

Fetching artifacts over the network introduces three concerns; the
implementation addresses each:

**1. Opt-in by default (security).** `Settings.allow_remote_urls:
bool = False`. With the default, any non-`file://` resolved URL raises
`RemoteURLBlockedError` at compile time. Users who want remote fetch
flip this on explicitly.

**2. Allowlist (defense in depth).** `Settings.allowed_url_hosts:
list[str] = []` — when non-empty, only URLs whose host matches one of
these patterns (substring or glob) are permitted. Lets a workflow opt
into "fetches from `*.internal.example.com` only" without flipping the
broad allow-remote switch off.

**3. Caching.** `_RemoteFetchCache` (a per-compile dict) caches
fetched bodies keyed by resolved URL. A subgraph referenced 100 times
in fan-out fetches once. Cache is per-compile, not persistent — keeps
the model simple; if persistent caching is wanted later, it's a
separate feature.

**4. Auth headers.** `Settings.url_headers: dict[str, dict[str, str]] =
{}` — keyed by URL prefix; first-prefix-wins. Example:
```yaml
settings:
  base_url: "https://prompts.example.com/v1/"
  allow_remote_urls: true
  url_headers:
    "https://prompts.example.com/":
      Authorization: "Bearer ${PROMPTS_API_TOKEN}"
```
`${VAR}` expansion is environment-driven so secrets stay out of YAML.

**5. Fetch errors.** Network failures during compile surface as
`RemoteURLFetchError` with the URL, status code, and body excerpt —
hard fail. We don't retry at compile time; a network blip should be
visible, not silently re-attempted.

**6. Cycle detection across protocols.** `detect_config_cycle` walks
the resolved URL set. A subgraph at `https://x.com/a.yaml` whose
`execute.url` ends up resolving back to `https://x.com/a.yaml`
(directly or indirectly) raises `SubgraphCycleError`. URLs are
compared as strings after resolution; canonical-URL form (no
trailing-slash variance, lowercase host) is enforced via
`urllib.parse.urlsplit` + reassembly.

**7. Remote scripts (.py/.js/.sh) — extra opt-in.**
`Settings.allow_remote_scripts: bool = False`. Remote prompts and
subgraphs are one risk class; remote *executables* are a higher one.
Even with `allow_remote_urls=True`, a remote script URL fails compile
unless `allow_remote_scripts` is also explicitly set. Remote scripts
are fetched to a temp dir, made executable, run via the interpreter,
deleted on completion. The temp file path goes into the subprocess
command line, not the URL — so the script sees its own filesystem
location, not its origin URL.

### Special markers (no URL)

Some node kinds don't have an artifact to point at:

- **Gate-only**: a node with `evaluation:` and no `execute:`. The gate
  runs against the empty output. (Today's `execution: { type: gate_only }`
  collapses by elision.)
- **Join**: a node with multiple `depends_on:` and no `execute:`. The
  join is implicit in topology — the existing LangGraph behavior. The
  explicit `type: join` marker we have today exists for author
  readability but isn't load-bearing; it can stay as a sentinel
  `execute: { type: "join" }` if the team wants the explicitness, or
  be elided like gate-only.

### Fan-out

`fan_out.template:` becomes a recursive node spec — same `execute:`
shape applies:

```yaml
- id: reviewer_pool
  execute:
    url: "prompts/reviewer_pool.md"   # parent's prompt produces manifest
  fan_out:
    enabled: true
    template:
      execute:
        url: "subgraphs/single_review.yaml"  # per-child subgraph
        params:
          inputs:
            reviewer_id: "{{id}}"
            paper_summary: "{{paper_summary}}"
      evaluation:
        validator: "gates/review_quality.py"
```

Per-child subgraph dispatch (the thing the audit prototyped and then
reverted) **falls out for free** under this shape.

## Implementation surface

### Schema (`src/abe_froman/schema/models.py`) — ~80 LOC delta

- Define `Execute(BaseModel)` with `url: str | None = None`, `type: Literal["join"] | None = None`, and `params: PromptParams | SubgraphParams | ScriptParams | ExecParams = {}` (discriminated by resolved URL extension/scheme; see `schema/params.py`). Validator: exactly one of `url` or `type` set.
- New `src/abe_froman/schema/params.py` (~60 LOC): per-mode Pydantic models (`PromptParams`, `SubgraphParams`, `ScriptParams`, `ExecParams`) plus a resolver that picks the right shape from a resolved URL.
- Replace `Node.execution: Execution | None`, `Node.config: str | None`,
  `Node.inputs: dict`, `Node.outputs: dict`, `Node.prompt_file: str | None`
  with a single `Node.execute: Execute | None`.
- Drop the `Execution` discriminated union (`PromptExecution`,
  `CommandExecution`, `GateOnlyExecution`, `JoinExecution`).
- Drop `_normalize_prompt_shorthand` (no shorthand any more).
- `FanOutTemplate` becomes `{ execute: Execute, evaluation: Evaluation | None }`.
- Extend `Settings`:
  ```python
  base_url: str | None = None
  allow_remote_urls: bool = False
  allow_remote_scripts: bool = False
  allowed_url_hosts: list[str] = []
  url_headers: dict[str, dict[str, str]] = {}
  ```
- Add a new module `src/abe_froman/runtime/url.py` (~80 LOC) with:
  - `resolve_url(url, base_url, workdir) -> str` (per the resolution
    rules above)
  - `fetch_url(resolved_url, settings) -> bytes` (validates against
    `allow_remote_urls` / `allowed_url_hosts` / `allow_remote_scripts`,
    consults `_RemoteFetchCache`, applies `url_headers`)
  - `RemoteURLBlockedError`, `RemoteURLFetchError` exception types
  - `_RemoteFetchCache` (per-compile dict, threaded through compile)
- `FanOutFinalNode` simplifies the same way.

### Dispatch (`src/abe_froman/runtime/executor/dispatch.py`) — ~100 LOC

- Add a `_DISPATCH_TABLE: list[tuple[Pattern, Handler]]` keyed by URL
  pattern.
- `DispatchExecutor.execute(node, ...)` reads `node.execute.url`,
  matches against the table, calls the handler with `(node, params,
  context, workdir)`.
- Handlers: `_dispatch_prompt`, `_dispatch_subgraph`, `_dispatch_script`,
  `_dispatch_binary`. Each returns `ExecutionResult`.

### Compile (`compile/graph.py`, `compile/dynamic.py`, `compile/subgraph.py`) — ~150 LOC

- `compile/graph.py` no longer keys on `node.config` to detect subgraph
  references. Instead: `node.execute and node.execute.url.endswith('.yaml')`.
- `compile/subgraph.py::detect_config_cycle` walks `node.execute.url`
  when extension matches `.yaml`. Same recursive structure, just keyed
  off `Execute` instead of `Node.config`.
- `compile/dynamic.py::_make_fan_out_node` reads `template.execute` and
  dispatches via the same handler table. The retry-loop wrapper is
  unchanged; only the per-Send "what runs" lookup changes.

### Migrate tool (`src/abe_froman/cli/migrate.py`) — ~80 LOC delta

The Stage 4 migrate tool already rewrites `phases:` → `nodes:`,
`quality_gate:` → `evaluation:`, etc. Stage 5b extends it with
post-Stage-4 → post-Stage-5b transforms:

| From | To |
|---|---|
| `prompt_file: "x.md"` | `execute: { url: "x.md" }` |
| `execution: { type: prompt, prompt_file }` | `execute: { url: prompt_file }` |
| `execution: { type: command, command, args }` | `execute: { url: command, params: { args } }` (or `url: "/usr/bin/<cmd>"`) |
| `execution: { type: gate_only }` | omit `execute:` block entirely (gate-only by elision) |
| `execution: { type: join }` | drop, OR `execute: { type: "join" }` if keeping explicit marker |
| `config: "x.yaml" + inputs: + outputs:` | `execute: { url: "x.yaml", params: { inputs, outputs } }` |
| `fan_out.template.prompt_file` | `fan_out.template.execute: { url }` |

The migrate tool gains a `--from-stage` flag (`--from-stage=3` for
pre-Stage-4 input, `--from-stage=4` for current input). The transforms
chain.

### Examples (`examples/`) — ~200 LOC delta across 4 workflows

Every checked-in workflow gets rewritten through the new shape. Run
the migrate tool against itself:

```bash
for f in examples/**/*.yaml; do
  uv run abe-froman migrate "$f" --from-stage=4 --in-place
done
```

Then hand-review the diffs. Compose-and-validate subgraph for
absurd-paper stays as-is (it's a `.yaml` referenced via `execute:
{ url: }`).

### Tests (`tests/`) — ~300 LOC delta

- All test fixtures using `prompt_file:`, `execution:`, `config:` get
  rewritten through migrate.
- New unit tests for the dispatch table (extension matching, handler
  selection, params validation per mode).
- New e2e tests confirming each dispatch mode fires correctly on a
  single workflow that exercises all of: prompt, subgraph, python
  script, command.

## Migration path

1. **Land Stage 5a first** (route node — independent of execute shape).
2. **Build Stage 5b on its own branch** (`stage-5b-execute-url`).
3. **Schema change is breaking** (no compat aliases — same policy as
   Stage 4's hard cutover). Migrate tool does the lift.
4. **Verification** identical to Stage 4 closeout: full pytest green,
   all four examples run via ACP, JSONL events unchanged (the schema
   change is upstream of the runtime telemetry).

## Estimated size

| Component | Net LOC |
|---|---|
| Schema | -100 (removing discriminator union) +50 = **-50** |
| Dispatch | +100 (handler table) -40 (existing type-switch) = **+60** |
| Compile (graph/dynamic/subgraph) | -50 (collapse type checks) +30 (URL extension dispatch) = **-20** |
| Migrate tool | +80 |
| Examples | ~0 (rewrite, no net add) |
| Tests | +100 (new dispatch tests) -50 (kill type-discriminator tests) = **+50** |
| **Total** | **~+120 net LOC, lots of churn** |

The schema *shrinks* (one shape replaces seven). The dispatch table
*grows* in one place to absorb the dispatch logic that used to live
scattered across `dispatch.py`, the `Execution` union, `Node.config`
handling, and `FanOutTemplate.prompt_file` handling. Net: smaller
mental footprint, modestly larger code surface in one well-bounded
place.

## Resolved design decisions

The following were open going into Stage 5b and are now locked:

1. **Join marker** — **Keep `execute: { type: "join" }` as an explicit
   sentinel.** Authors can name a fan-in point even when topology
   alone implies a join. Carve-out in the dispatch table: a `type:
   "join"` entry on `Execute` short-circuits URL resolution.
2. **Bare commands** — **Treat any URL not matching a known extension
   as a binary path.** `url: "echo"` resolves to `file://<workdir>/echo`
   (won't exist) → fails fast; `url: "/bin/echo"` works; `url: "echo"`
   with absolute resolution via `$PATH` lookup is **not** supported
   (too magical). The migrate tool rewrites `command: echo` →
   `url: "/bin/echo"` (using `shutil.which` at migrate time).
3. **Params validation** — **Per-mode Pydantic dataclasses.** Define
   `PromptParams`, `SubgraphParams`, `ScriptParams`, `ExecParams` in
   `schema/params.py`. Schema validator looks at the resolved URL's
   extension/scheme and selects the matching dataclass to coerce
   `Execute.params` into. Catches typos like `arg` vs `args` at
   compile time, before runtime.
4. **Remote fetch cache scope** — **Per-compile only.**
   `_RemoteFetchCache` is a dict threaded through compile context.
   Persistent caching deferred — would require ETag/cache-control
   handling and a `cache clear` CLI knob.

## Open design questions

1. **Migration of `inputs:` / `outputs:`**: currently top-level on
   Node; in the new shape they're nested under `execute.params`. The
   migrate tool needs to lift them in. (Mechanical.)
2. **Remote fetch size cap**: should `fetch_url` enforce a max body
   size (e.g. 5 MB) to bound memory on a misconfigured allowlist?
   Recommend yes — `Settings.max_remote_fetch_bytes: int = 5_000_000`,
   exceeded fetches raise `RemoteURLFetchError`.
3. **`${VAR}` expansion scope**: env-var expansion in `url_headers`
   values is non-negotiable (secrets). Should it also apply to `url:`
   itself (e.g. `url: "${PROMPTS_BASE}/x.md"`)? Recommend no — keeps
   the resolution algorithm pure-string; users put base in
   `Settings.base_url` instead.
4. **Allowlist match semantics**: `allowed_url_hosts` patterns —
   substring, glob, or regex? Recommend glob (`fnmatch.fnmatch`) on
   the host component only; rejects path-injection tricks like
   `https://attacker.com/?fake=trusted.example.com`.

## What this unlocks

- **Per-child subgraph fan-out** comes for free (audit's prototyped
  `FanOutTemplate.config:` becomes just another `execute.url` value
  that happens to be a `.yaml`).
- **Polyglot scripts** — `.py`, `.js`, `.ts`, `.sh` all dispatch
  through the same shape. Currently any non-prompt non-subgraph
  execution requires a `command` invocation that authors hand-write.
- **Future modes** plug in cleanly: a new dispatch handler is one
  table entry. No new `Execution` union member, no new `Node.<thing>`
  field.

## What this does NOT do

- Doesn't change runtime semantics: gating, retry, evaluation,
  worktrees, output_contract, fan-out are all unchanged.
- Doesn't change state channels (`node_outputs`, `child_outputs`,
  `evaluations` etc.) — those stay as Stage 4 left them.
- Doesn't change checkpointer behavior.
- Doesn't change CLI surface (`abe-froman run`, `validate`, etc.) —
  except `migrate` gains the `--from-stage=4` transform.

## Exit criteria

- [ ] `Execute` schema landed; `Execution` union deleted.
- [ ] `schema/params.py` per-mode dataclasses landed; schema-time
      validator rejects mode-mismatched keys (e.g. `args:` on a
      prompt URL).
- [ ] `execute: { type: "join" }` sentinel works end-to-end (no URL
      resolution, no fetch, dispatcher returns empty output).
- [ ] Migrate tool rewrites `command: <bare>` → `url: <shutil.which(bare)>`
      (or fails loudly if not found on `$PATH`).
- [ ] Dispatch table operational; one handler per supported URL pattern.
- [ ] `runtime/url.py::resolve_url` covers all six rows of the
      examples table; unit tests pin each.
- [ ] `runtime/url.py::fetch_url` enforces `allow_remote_urls`,
      `allowed_url_hosts`, `allow_remote_scripts`, and
      `max_remote_fetch_bytes`; each gate has a dedicated unit test
      asserting the right exception type.
- [ ] Cycle detection extended over resolved URL set (not just
      `node.config` paths); test pins a cross-protocol cycle
      (`file://a.yaml` → `https://x.com/b.yaml` → `file://a.yaml`).
- [ ] `_RemoteFetchCache` exercised by an e2e that references the
      same `https://` subgraph from 3 fan-out children; assert one
      fetch attempt, three uses.
- [ ] `${VAR}` expansion in `url_headers` honors process env;
      missing var raises a clear error at compile time.
- [ ] All examples migrated; all examples run via ACP.
- [ ] Migrate tool extended to lift Stage 4 → Stage 5b shape.
- [ ] Per-child subgraph fan-out works (the absurd-paper reviewer_pool
      carve becomes a one-line `template.execute.url` change). Re-run
      reviewer_pool with a real multi-step subgraph and ensure timing
      is acceptable (raise reviewer_pool's timeout to ~360s if needed
      so a sequential draft + critique fits under the per-Send cap).
- [ ] Full pytest green; test count comparable.
- [ ] No `Execution` / `PromptExecution` / `CommandExecution` /
      `GateOnlyExecution` symbols anywhere in src/.
