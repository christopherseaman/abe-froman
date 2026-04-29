# Stage 5a — Route Node Primitive

## Context

Today's routing logic is hidden inside `_make_evaluation_node` (compile/nodes.py)
and a private Criterion DSL inside `compile/evaluation.py`. The Evaluation
block on a Node carries four conflated concerns: scoring, pass/retry/fail
classification, retry-reason injection, and downstream gating. Authors have
no way to express custom routing — "after 3 failed attempts, escalate to
human review" requires monkey-patching the gate's blocking behavior, not
authoring a clear topology decision.

LangGraph's native primitive for this is `Command(goto=...)` returned from a
node — a single function that reads state and emits a routing decision.
Stage 5a introduces a `route` execution type that exposes this as a
first-class node. Routes have **zero baked-in semantics** (no retry, no
halt, no delay): they are pure case ladders over structured state. Retries
become "a case that gotos the producer node"; halts become "a case that
gotos `__end__`."

The existing internal `Criterion` machinery in `compile/evaluation.py` was a
scaffolding bridge toward this exact primitive (its module docstring says
so). Stage 5a does **not** extend that DSL — it builds the user-facing
route node from scratch with `simpleeval`. Stage 5c will desugar the
existing `evaluation:` block into `produce → evaluate → route` triplets at
compile time and delete `evaluation_to_routes` / `walk_routes` /
`Criterion` outright; until then the existing evaluation routing keeps
working unchanged.

## Decisions locked

1. **Predicate language: `simpleeval`.** One small pure-Python dep
   (~500 LOC, sandboxed: no dunders, no I/O, no statements). Authors write
   Python expressions in `when:` strings. Namespace exposes dep outputs,
   evaluation history, full state, and `len`/`any`/`all`/`min`/`max`/`sum`.
2. **Goto sentinels: `__end__` only.** Maps to LangGraph's `END`.
   Multi-target parallel fan-out is a general-purpose primitive (not
   route-specific) → wishlist, deferred.
3. **Compile-time validation:** every route requires an `else:` branch, and
   every `goto:` target must resolve to a real node id (or `__end__`).
4. **Runtime mechanism: `Command(goto=...)`.** Route node functions return
   `langgraph.types.Command` directly. First Command usage in the codebase;
   replaces the two-phase node-then-conditional-edges pattern used by
   evaluation routing.
5. **No retry/backoff/halt semantics on route itself.** Pure case ladder.
   The existing `evaluation:` block keeps its baked-in retry logic (with
   `max_retries`, `retry_backoff`) for backwards compatibility through
   Stage 5b; Stage 5c will desugar it into authored route cases.

## Schema (`src/abe_froman/schema/models.py`)

Add to the `Execution` discriminated union:

```python
class RouteCase(BaseModel):
    when: str            # simpleeval expression — must evaluate truthy to fire
    goto: str            # target node id, or "__end__"

class RouteExecution(BaseModel):
    type: Literal["route"] = "route"
    cases: list[RouteCase] = []        # first-match-wins; may be empty (else-only)
    else_: str = Field(alias="else")   # required catch-all

Execution = Annotated[
    PromptExecution | CommandExecution | GateOnlyExecution
    | JoinExecution | RouteExecution,
    Field(discriminator="type"),
]
```

Top-level Graph validator (post-`Node` validation): walk every
`RouteExecution`, assert each `case.goto` and `else_` resolves to a node
id in `config.nodes` or equals `__end__`. Emits a clear error with the
offending node id and target. Lives at the Graph level (not Node level)
because validation requires the full id-set.

YAML shape:

```yaml
- id: decide
  depends_on: [judge]
  execution:
    type: route
    cases:
      - when: "judge['score'] >= 0.8"
        goto: aggregate
      - when: "len(history['judge']) >= 3"
        goto: __end__
    else: produce
```

## Compile (`src/abe_froman/compile/`)

**New module: `compile/route.py`** (small, ~120 LOC) — the simpleeval
sandbox + namespace builder + case-walker. Imports nothing from
`langgraph` (state-shape utility only, enforced by
`tests/architecture/test_layers.py`).

```python
from simpleeval import EvalWithCompoundTypes

_SAFE_FUNCS = {"len": len, "any": any, "all": all,
               "min": min, "max": max, "sum": sum}

def build_route_namespace(state: WorkflowState, deps: list[str]) -> dict[str, Any]:
    """Bind each dep's structured_output (else raw output) by id, plus history."""
    ns: dict[str, Any] = {}
    structured = state.get("node_structured_outputs", {})
    outputs = state.get("node_outputs", {})
    for dep in deps:
        ns[dep] = structured.get(dep, outputs.get(dep))
    ns["history"] = state.get("evaluations", {})
    ns["state"] = dict(state)
    return ns

def evaluate_case(when: str, namespace: dict[str, Any]) -> bool:
    evaluator = EvalWithCompoundTypes(names=namespace, functions=_SAFE_FUNCS)
    return bool(evaluator.eval(when))
```

**Updated module: `compile/nodes.py`** — add `_make_route_node(node, config)`:

```python
def _make_route_node(node: Node, config: Graph):
    route = node.execution
    assert isinstance(route, RouteExecution)

    async def node_fn(state: WorkflowState) -> Command:
        ns = build_route_namespace(state, node.depends_on)
        for case in route.cases:
            try:
                if evaluate_case(case.when, ns):
                    return Command(goto=_resolve_goto(case.goto))
            except Exception as e:
                # Loud failure: surface broken expressions, don't fall through silently
                raise ValueError(f"Route '{node.id}' case {case.when!r}: {e}") from e
        return Command(goto=_resolve_goto(route.else_))

    return node_fn

def _resolve_goto(target: str) -> str:
    return END if target == "__end__" else target
```

**Updated module: `compile/graph.py`** — in the build-loop, dispatch route
nodes to `_make_route_node` and add via `builder.add_node(node.id, fn)`.
**No `add_conditional_edges` needed** — Command-returning nodes
self-route. No outgoing static edges needed either; LangGraph routes
based on the Command's goto target.

Compile-time goto validation runs before `_make_route_node` is called
(see Schema section).

## Runtime

No new state channels. Route node functions return
`langgraph.types.Command` instead of dict. Existing reducers and
state-shape are untouched.

`runtime/executor/dispatch.py` does **not** route `RouteExecution` —
route is a compile-time construct, not an execution dispatched through
NodeExecutor. The `_make_route_node` factory wires it directly at graph
construction time.

## Out of scope (deferred)

- **Stage 5b** (output specification): `schema:` field on Node; backend
  populates `structured_output` end-to-end; templates navigate dict
  fields. Adds the path for `prompt`/`run` nodes to feed route directly
  without going through an evaluate gate.
- **Stage 5c** (`evaluation:` block desugaring): compile-time rewrite of
  `evaluation:` block into `produce → evaluate → route` triplets. Delete
  `compile/evaluation.py` Criterion machinery once nothing emits it.
- **Verb-form rename**: `gate_only` → `evaluate`/`eval`; `command` →
  `run`/`cmd`. Pure rename. Bundle with 5c.

## Wishlist additions

Append to WISHLIST.md (informational, not in 5a scope):

- **Multi-target parallel fan-out as a general primitive**: `goto: [a, b, c]`
  semantics for any node that decides flow (LangGraph supports this via
  list-return conditional edges). Not route-specific — applies to
  evaluation routers too, and could subsume some `fan_out:` cases.
- **Output specification unification**: one `output:` field on Node taking
  `schema` | `contract` | (none). Today `output_contract:` is
  free-floating; folding it under `output:` makes the three modes
  symmetric.
- **Schema enforcement at backend boundary**: ACP and stub backends
  populate `ExecutionResult.structured_output` when `schema:` is set.
  Today the field exists end-to-end but no backend writes to it.
- **Schema-first templates**: `{{judge.score}}` works for structured
  outputs; `{{judge}}` falls back to raw string.
- **Schema sources**: inline JSON-schema dict OR `schema_file:` path OR
  `schema_class: my_module.GateScore` for Pydantic.
- **Per-node delay primitive**: a wrapping concern (orthogonal to route)
  for backoff between attempts when authoring retry-via-route patterns.

## Files to change

| File | Change |
|---|---|
| `pyproject.toml` | Add `simpleeval>=0.9` to dependencies |
| `src/abe_froman/schema/models.py` | Add `RouteCase`, `RouteExecution`; extend `Execution` union; add Graph-level goto-resolution validator |
| `src/abe_froman/compile/route.py` | **New** — `build_route_namespace`, `evaluate_case`, `_SAFE_FUNCS` |
| `src/abe_froman/compile/nodes.py` | Add `_make_route_node`, `_resolve_goto`; import `Command` from `langgraph.types` |
| `src/abe_froman/compile/graph.py` | Dispatch `RouteExecution` nodes to `_make_route_node` in the build loop |
| `tests/unit/compile/test_route.py` | **New** — see Tests section |
| `tests/unit/schema/test_route_schema.py` | **New** — RouteExecution parsing + validator tests |
| `tests/e2e/test_route_node.py` | **New** — end-to-end produce→judge→route flows |
| `tests/architecture/test_layers.py` | Add rule: `compile/route.py` imports no `langgraph` |
| `CLAUDE.md` | Document the `route` execution type in the Workflow Schema section |
| `WISHLIST.md` | Append the deferred items listed above |

## Tests (per project methodology: function-level → multi-function flows)

### Single-function (`tests/unit/compile/test_route.py`)
- `build_route_namespace`: dep with `structured_output` → bound as dict; dep with only `node_outputs` string → bound as string; missing dep → bound as None; `history` populated from `state.evaluations`; `state` is the full state dict; safe-funcs (`len`, `any`, `all`) callable.
- `evaluate_case` known-good: `"score >= 0.8"` against ns `{"score": 0.9}` → True; `"len(history) >= 3"` against ns with 3-entry history → True.
- `evaluate_case` known-bad: malformed expression `"score >="` raises a parse error (caught by route node and surfaced loudly with case context).
- `evaluate_case` sandbox: `"__import__('os')"` raises (simpleeval blocks dunders).
- `_resolve_goto`: `"__end__"` → LangGraph END constant; `"foo"` → `"foo"`.

### Schema (`tests/unit/schema/test_route_schema.py`)
- Parse a YAML node with `execution.type: route` + cases + else → Pydantic builds RouteExecution. Round-trip.
- Missing `else:` field → ValidationError mentioning the field.
- Empty cases + present else → parses (else-only is legal: it's an unconditional goto).
- Graph-level validator: `goto: nonexistent_node` → raises with offending node + target named.
- Graph-level validator: `goto: __end__` → passes.
- Graph-level validator: `goto:` to a real node id → passes.

### Compile (`tests/unit/compile/test_route.py` continued)
- `_make_route_node` with cases `[{when: "True", goto: "X"}]` + else "Y" → returns Command(goto="X").
- `_make_route_node` with all cases False → returns Command(goto=else_target).
- `_make_route_node` with broken `when:` expression → raises ValueError naming the route id and the offending case.
- Goto sentinel: case `goto: __end__` → returned Command(goto=END).

### Multi-function end-to-end (`tests/e2e/test_route_node.py`)

**Flow A — score-based routing (no retry):**
```yaml
nodes:
  - id: produce
    execution: { type: command, command: echo, args: ["draft"] }
  - id: judge
    depends_on: [produce]
    evaluation: { validator: "gates/score.py", threshold: 0.5 }
  - id: route
    depends_on: [judge]
    execution:
      type: route
      cases:
        - when: "judge['score'] >= 0.8"
          goto: ship
      else: __end__
  - id: ship
    execution: { type: command, command: echo, args: ["shipped"] }
```
Two known-good/bad fixtures under `tests/fixtures/route/`:
- gate `score.py` returns `{"score": 0.9, ...}` → asserts ship ran (in completed_nodes).
- gate `score.py` returns `{"score": 0.3, ...}` → asserts ship NOT in completed_nodes; workflow halted.

**Flow B — retry-via-goto pattern:**
```yaml
- id: route
  execution:
    type: route
    cases:
      - when: "judge['score'] >= 0.8"
        goto: ship
      - when: "len(history['judge']) >= 3"
        goto: __end__
    else: produce
```
Fixture: gate score script alternates score by `ATTEMPT_NUMBER` env var. Assert: workflow loops produce→judge→route→produce until `len(history['judge']) >= 3`, then halts. Assert `state.evaluations["judge"]` has 3 records, oldest first.

**Flow C — route on prompt structured-output (forward-compat for 5b):**
A `prompt` node whose stub backend is monkey-patched to return `structured_output={"category": "urgent"}`. Route on `produce['category'] == 'urgent'`. Asserts the route fires on structured output, not just evaluation history. Documents the path 5b unlocks; today this requires a stub override since no backend writes structured_output yet.

### Architecture (`tests/architecture/test_layers.py`)
- New rule: `compile/route.py` imports nothing from `langgraph` (it's a state-shape utility, not a graph builder). Enforced via AST walk like the existing rules.

## Verification

1. `git checkout -b stage-5a-route-node`
2. `uv sync` — pulls in `simpleeval`.
3. `uv run pytest tests/unit/compile/test_route.py tests/unit/schema/test_route_schema.py tests/e2e/test_route_node.py -v` — all green.
4. Full suite: `uv run pytest tests/ -v` — 490 prior tests still pass; new tests add ~15.
5. `uv run pytest tests/architecture/test_layers.py` — layer rules clean (compile/route.py is langgraph-free).
6. `uv run abe-froman validate examples/smoke_test.yaml` — smoke runs unchanged (no route node yet in examples).
7. `uv run abe-froman run tests/fixtures/route/flow_b_retry.yaml --workdir <tmp>` (manually authored) — completes; logs show 3 produce→judge→route loops then halt.
8. `rg "Command\\(" src/` — confirms `Command` introduced only in `compile/nodes.py` (route factory).

## Exit criteria

- [ ] `simpleeval` added to `pyproject.toml` dependencies.
- [ ] `RouteExecution` parses from YAML; `else:` is required; `cases:` may be empty.
- [ ] Graph-level validator rejects unresolved `goto:` targets and unknown sentinels.
- [ ] `_make_route_node` returns `Command(goto=...)`; case ladder is first-match-wins; else fires when no case matches.
- [ ] simpleeval namespace exposes each dep's structured (or raw) output by id, full evaluations history, full state, and len/any/all/min/max/sum.
- [ ] Broken `when:` expressions raise loudly with route id + case context (no silent fall-through).
- [ ] All three end-to-end flows pass (score routing, retry-via-goto with history-length halt, structured-output routing).
- [ ] Architecture layer rule for `compile/route.py` enforced.
- [ ] CLAUDE.md documents the `route` execution type with a worked example.
- [ ] WISHLIST.md captures multi-goto, output-spec unification, schema enforcement, schema-first templates, schema sources, per-node delay primitive.
- [ ] No regression: `uv run pytest tests/` — 490 prior tests still pass.

## Risks & open considerations

- **simpleeval CVE history.** Modest sandbox; not as battle-tested as CEL or
  RestrictedPython. We're not running untrusted YAML — workflow configs are
  in-repo author code — so the threat model is "footgun prevention,"
  which simpleeval handles. If we ever accept untrusted route configs
  (e.g., loaded from a database), revisit.
- **Loud failures on broken expressions.** A typo in `when:` becomes a
  runtime error, not a compile-time one (we don't statically verify name
  bindings against schemas). Stage 5b's schema work makes static
  verification possible later; for now, fast loud failure is the safety
  net.
- **`history` namespace shape.** `history` is a dict keyed by node id, mapping
  to a list of evaluation records. It does NOT include execution history
  for non-evaluation producers (those replace their own output on retry,
  state doesn't accumulate). Documented behavior; if authors need
  per-attempt producer history, that's a separate feature.
- **Stage 5c is the cleanup pass.** Until it lands, both the new route
  primitive AND the legacy `evaluation:` block coexist. The two systems
  don't interact — a single node has either an `evaluation:` block (legacy
  retry routing) or it's a `route` execution (new pure routing), never
  both. This is enforced by the existing Node validator (one execution
  type per node).
