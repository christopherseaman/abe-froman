"""Node node factory and decomposed helpers.

_make_execution_node returns an async callable for StateGraph.add_node.
Pure helpers (check_*, build_context, classify_evaluation_outcome, etc.)
operate on WorkflowState/Node dicts with no langgraph dependency.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langgraph.graph import END
from langgraph.types import Command

from sqrlly.compile.evaluation import (
    EvaluationRecord,
    build_eval_context,
    evaluation_fallback,
    evaluation_to_routes,
    walk_routes,
)
from sqrlly.runtime.gates import (
    EvaluationResult,
    build_eval_preamble,
    scaffold_output_directory,
    validate_output_contract,
)
from sqrlly.runtime.executor.prompt import retry_delay
from sqrlly.runtime.gates import run_evaluation
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Node, Settings, Graph

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


def check_dep_failed(node: Node, state: WorkflowState) -> dict | None:
    failed = state.get("failed_nodes", set())
    for dep in node.depends_on:
        if dep in failed:
            return {
                "failed_nodes": {node.id},
                "errors": [
                    {
                        "node": node.id,
                        "error": f"Skipped: dependency '{dep}' failed",
                    }
                ],
            }
    return None


def all_deps_completed(node: Node, state: WorkflowState) -> bool:
    """True iff every dep is in completed_nodes.

    Hand-rolled join barrier. A node with N gated predecessors gets
    triggered N times — once per predecessor's Decision node firing
    `Command(goto=this_node)`. The node body calls this guard and
    returns `{}` until all N preds are done; LangGraph re-fires it on
    each subsequent pred-trigger until the guard passes.

    Why not LangGraph's native `NamedBarrierValue` multi-edge join:
    that barrier counts only pure-static `add_edge` predecessors —
    `Command(goto)` and conditional edges bypass it. Gated preds reach
    this node via `Command(goto)` from their Decision nodes, and
    `Command(goto)` does NOT suppress a node's static out-edges
    (verified empirically), so there is no clean way to make the
    Decision node a static-edge join predecessor without per-node
    marker nodes that cost more than this guard. See TODO #33
    (closed not-a-defect, 2026-05-20).
    """
    completed = state.get("completed_nodes", set())
    return all(dep in completed for dep in node.depends_on)


def check_dry_run(node: Node, state: WorkflowState) -> dict | None:
    if not state.get("dry_run", False):
        return None
    # Dry-run writes node_outputs + completed_nodes. Gated nodes still
    # route through their Evaluation node, which handles dry-run itself by
    # synthesizing a pass EvaluationRecord — so we don't pre-complete here
    # for gated nodes.
    update: dict[str, Any] = {
        "node_outputs": {node.id: f"[dry-run] {node.name}"},
    }
    if not node.evaluation:
        update["completed_nodes"] = {node.id}
    return update


def build_context(node: Node, state: WorkflowState) -> dict[str, Any]:
    import json as _json

    context: dict[str, Any] = {}
    outputs = state.get("node_outputs", {})
    structured = state.get("node_structured_outputs", {})
    worktrees = state.get("node_worktrees", {})
    child_outputs = state.get("child_outputs", {})
    # Subgraph inputs (Stage 4c): inputs declared on a parent's subgraph-
    # reference node are projected into the subgraph's state.node_inputs
    # before invocation. Subgraph nodes see them as plain template vars,
    # alongside their own dep outputs. Top-level graphs have no inputs.
    inputs = state.get("node_inputs", {}) or {}
    context.update(inputs)
    for dep in node.depends_on:
        if dep in outputs:
            context[dep] = outputs[dep]
        if dep in structured:
            context[f"{dep}_structured"] = structured[dep]
        if dep in worktrees:
            context[f"{dep}_worktree"] = worktrees[dep]
        # Subgraph `outputs:` projects values to `node_outputs[dep.key]`.
        # Bind those under `{dep}_{key}` so templates can reach them
        # without dotted-attribute syntax (Jinja can't dot-into a string).
        dotted_prefix = f"{dep}."
        for k, v in outputs.items():
            if k.startswith(dotted_prefix):
                suffix = k[len(dotted_prefix):]
                context[f"{dep}_{suffix}"] = v
        # Synthesize fan-out aggregates from state. Any node depending on
        # a dynamic parent sees `{{dep_branches}}` (JSON id→output map),
        # `{{dep_branch_worktrees}}` (JSON list of worktree paths), and
        # `{{dep_branch_map}}` (JSON id→{output, worktree}, the preferred
        # id-keyed pairing) — not just the final-node wrapper.
        prefix = f"{dep}::"
        dep_branches = {k: v for k, v in child_outputs.items() if k.startswith(prefix)}
        if dep_branches:
            context[f"{dep}_branches"] = _json.dumps(dep_branches)
            dep_branch_worktrees = [v for k, v in worktrees.items() if k.startswith(prefix)]
            context[f"{dep}_branch_worktrees"] = _json.dumps(dep_branch_worktrees)
            branch_map = {
                cid: {"output": out, "worktree": worktrees.get(cid)}
                for cid, out in dep_branches.items()
            }
            context[f"{dep}_branch_map"] = _json.dumps(branch_map)

    # When a node has multiple deps, provide aggregate collections so
    # templates can iterate inputs generically without hardcoding names.
    if len(node.depends_on) > 1:
        dep_outputs_map = {}
        dep_worktrees_map = {}
        for dep in node.depends_on:
            if dep in outputs:
                dep_outputs_map[dep] = outputs[dep]
            if dep in worktrees:
                dep_worktrees_map[dep] = worktrees[dep]
        context["_deps"] = _json.dumps(dep_outputs_map)
        if dep_worktrees_map:
            context["_dep_worktrees"] = _json.dumps(dep_worktrees_map)

    # Stage 5c: cross-cutting eval access. `evals[node_id]` returns the
    # latest evaluation result dict for any node that has run an eval.
    # Always-on so templates can reference `{{evals.classify.score}}`
    # without declaring classify as a dep.
    history = state.get("evaluations", {}) or {}
    context["evals"] = {
        nid: (records[-1].get("result", {}) or {}) if records else {}
        for nid, records in history.items()
    }

    # Stage 5c: inline-route sender threading. When this node was
    # reached via Command(goto=...) from a `_route_<id>` dispatcher,
    # state carries the source node id. Always-bound identity vars:
    # sender_id (str), sender (raw output), sender_structured
    # (parsed if available), sender_worktree (path or None).
    sender_id = state.get("_route_sender")
    if sender_id is not None:
        context["sender_id"] = sender_id
        if sender_id in outputs:
            context["sender"] = outputs[sender_id]
        if sender_id in structured:
            context["sender_structured"] = structured[sender_id]
        if sender_id in worktrees:
            context["sender_worktree"] = worktrees[sender_id]

    # Pre-built eval preamble (auto-prepended by `_dispatch_prompt`
    # when present and non-empty). The synthetic `_route_<id>` writes
    # this into state when `include_eval: true` on the matched case.
    preamble = state.get("_route_eval_preamble")
    if preamble:
        context["_route_eval_preamble"] = preamble

    return context


def inject_retry_reason(
    context: dict[str, Any],
    node: Node,
    state: WorkflowState,
    max_retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Auto-prepend the eval preamble for retry attempts.

    Reads `state.evaluations[<key>][-1]` for the last result, formats
    it via `build_eval_preamble` with retry context (Attempt N of M),
    and stuffs the rendered string into `context["_retry_reason"]` —
    the prompt template prepends `{{_retry_reason}}` to its body.
    """
    key = node_id or node.id
    retry_count = state.get("retries", {}).get(key, 0)
    if retry_count == 0 or not node.evaluation:
        return context

    records = state.get("evaluations", {}).get(key, [])
    if not records:
        return context
    last_result = records[-1].get("result", {}) or {}

    context["_retry_reason"] = build_eval_preamble(
        last_result, node.evaluation,
        attempt=retry_count, total_attempts=max_retries,
    )
    return context


async def run_with_timeout(
    awaitable: Any, timeout: float | None,
) -> ExecutionResult:
    """Await an execute-coroutine with an optional timeout.

    On timeout, returns a failure ``ExecutionResult`` (rather than a
    sentinel) so callers handle it through the ordinary
    ``if not result.success`` path. ``timeout=None`` awaits unbounded.
    The awaitable must resolve to an ``ExecutionResult``.
    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        return ExecutionResult(
            success=False, error=f"Node timed out after {timeout}s",
        )


def make_failure_update(node_id: str, error_message: str) -> dict[str, Any]:
    return {
        "failed_nodes": {node_id},
        "errors": [{"node": node_id, "error": error_message}],
    }


def assemble_success_update(node: Node, result: ExecutionResult) -> dict[str, Any]:
    update: dict[str, Any] = {
        "node_outputs": {node.id: result.output},
    }
    if result.structured_output is not None:
        update["node_structured_outputs"] = {node.id: result.structured_output}
    if result.model is not None:
        update["node_models"] = {
            node.id: {"model": result.model, "preset": result.preset}
        }
    return update


def _evaluation_result_payload(
    eval_result: EvaluationResult, evaluation: Any | None = None
) -> dict[str, Any]:
    """Flatten EvaluationResult into the `result` dict the route walker sees.

    When the evaluation declares dimensions, backfill any missing dims with
    0.0 so numeric comparisons don't silently evaluate against None and
    escape both pass and retry routes.
    """
    scores = dict(eval_result.scores)
    dims = getattr(evaluation, "dimensions", None) if evaluation is not None else None
    if dims:
        for d in dims:
            scores.setdefault(d.field, 0.0)
    payload: dict[str, Any] = {
        "score": eval_result.score,
        "scores": scores,
        "reasons": dict(eval_result.reasons),
        "feedback": eval_result.feedback,
        "pass_criteria_met": list(eval_result.pass_criteria_met),
        "pass_criteria_unmet": list(eval_result.pass_criteria_unmet),
    }
    # Surface the pass decision + its inputs so the gate_evaluated event
    # (and any programmatic consumer) can tell a pass from a non-blocking
    # warn-continue without recomputing score-vs-threshold. Multi-dim uses
    # the per-dimension mins (weakest-link), matching the route walker.
    if evaluation is not None:
        if dims:
            passed = all(scores.get(d.field, 0.0) >= d.threshold for d in dims)
            # Echo the per-dimension floors so a consumer can attribute which
            # dimension blocked: the overall `threshold` is not what's enforced
            # for a dims gate (the per-dimension mins are).
            payload["dimension_thresholds"] = {d.field: d.threshold for d in dims}
        else:
            passed = eval_result.score >= evaluation.threshold
        payload["passed"] = passed
        payload["threshold"] = evaluation.threshold
        payload["blocking"] = evaluation.blocking
    return payload


def classify_evaluation_outcome(
    node: Node,
    eval_result: EvaluationResult,
    retries: int,
    max_retries: int,
    *,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Walk the routes generated from the Evaluation sugar.

    Kept as public API (tests + external callers). Internally this is just
    a thin adapter over `walk_routes` from compile/evaluation.py — the
    string return value ("pass", "retry", "fail_blocking", "warn_continue")
    is the matched route's destination label.
    """
    evaluation = node.evaluation
    routes = evaluation_to_routes(evaluation, max_retries)
    context = build_eval_context(
        _evaluation_result_payload(eval_result, evaluation),
        invocation=retries,
        history=list(history or []),
    )
    matched = walk_routes(routes, context)
    if matched is not None:
        return matched.to
    return evaluation_fallback(evaluation)


def _evaluation_summary(node: Node, result: EvaluationResult) -> str:
    evaluation = node.evaluation
    if evaluation.dimensions:
        parts = [
            f"{d.field}={result.scores.get(d.field, 0.0):.2f} (min {d.threshold})"
            for d in evaluation.dimensions
        ]
        return ", ".join(parts)
    return f"score={result.score:.2f}, threshold={evaluation.threshold}"


def build_record_only_update(
    node: Node,
    result: EvaluationResult,
    retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    """An EvaluationRecord write, nothing else.

    Used by the gate node's already-completed branch (verdict not
    enforced, but the record is still written) and composed into the
    combined record+outcome update by ``build_evaluation_outcome_update``.
    """
    key = node_id or node.id
    record = EvaluationRecord.now(
        invocation=retries,
        result=_evaluation_result_payload(result, node.evaluation),
    )
    return {"evaluations": {key: [record.to_dict()]}}


def build_outcome_only_update(
    node: Node,
    result: EvaluationResult,
    outcome: str,
    retries: int,
    max_retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    """The outcome-state writes only (no record).

    Returns ONLY the routing-state writes (``completed_nodes`` /
    ``failed_nodes`` / ``retries`` / ``errors``); composed with the
    record write by ``build_evaluation_outcome_update``.
    """
    key = node_id or node.id
    update: dict[str, Any] = {}
    summary = _evaluation_summary(node, result)

    if outcome == "pass":
        update["completed_nodes"] = {key}
    elif outcome == "retry":
        update["retries"] = {key: retries + 1}
    elif outcome == "fail_blocking":
        update["failed_nodes"] = {key}
        update["errors"] = [
            {
                "node": key,
                "error": f"Evaluation failed after {max_retries} retries ({summary})",
            }
        ]
    elif outcome == "warn_continue":
        update["completed_nodes"] = {key}
        update["errors"] = [
            {
                "node": key,
                "error": (
                    f"Evaluation below threshold after {max_retries} retries "
                    f"({summary}), continuing (non-blocking)"
                ),
            }
        ]

    return update


def build_evaluation_outcome_update(
    node: Node,
    result: EvaluationResult,
    outcome: str,
    retries: int,
    max_retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Combined record + outcome state — the gate node's pass/retry/fail
    update, and the fan-out branch inline retry loop's per-attempt update
    (``compile/dynamic.py::_make_fan_out_node``). One helper call yields
    both the EvaluationRecord and the routing-state writes.
    """
    update = build_record_only_update(node, result, retries, node_id=node_id)
    outcome_update = build_outcome_only_update(
        node, result, outcome, retries, max_retries, node_id=node_id,
    )
    update.update(outcome_update)
    return update


def _scope_dep_outputs_for_gate(
    node: Node, state: WorkflowState,
) -> tuple[
    dict[str, str] | None,
    dict[str, Any] | None,
    dict[str, str] | None,
]:
    """Pick the dep-output dicts a gate validator should see.

    A gate normally sees only its node's declared deps (matches
    ``build_context``'s scoping). But a "gate-only" phase — a node
    with ``evaluation:`` and no ``execute:`` and no ``depends_on:`` —
    has nothing to scope to and gets the full set of completed-node
    outputs (the TODO bug case: gate-only phases need a useful
    signal somewhere).
    """
    node_outputs = state.get("node_outputs", {}) or {}
    structured = state.get("node_structured_outputs", {}) or {}
    worktrees = state.get("node_worktrees", {}) or {}

    deps = list(node.depends_on or [])
    is_gate_only = (
        not deps
        and node.execute is None
        and node.evaluation is not None
    )

    if is_gate_only:
        chosen_outputs = dict(node_outputs)
        chosen_structured = dict(structured)
        chosen_worktrees = dict(worktrees)
    else:
        chosen_outputs = {d: node_outputs[d] for d in deps if d in node_outputs}
        chosen_structured = {d: structured[d] for d in deps if d in structured}
        chosen_worktrees = {d: worktrees[d] for d in deps if d in worktrees}

    return (
        chosen_outputs or None,
        chosen_structured or None,
        chosen_worktrees or None,
    )


async def _run_eval_core(
    node: Node,
    config: Graph,
    state: WorkflowState,
    *,
    node_id: str,
    node_output: str,
    retries: int,
    backend: Any,
    timeout: float | None,
    effective_settings: Settings | None,
) -> tuple[EvaluationResult | None, dict[str, Any] | None]:
    """Run the gate validator and return ``(result, None)`` on success
    or ``(None, failure_update)`` on timeout.

    Shared core between ``_make_gate_node`` and
    ``run_evaluation_and_outcome`` — everything between the eligibility
    checks and the per-caller outcome update.
    """
    s = effective_settings or config.settings
    dep_outputs, dep_structured, dep_worktrees = _scope_dep_outputs_for_gate(
        node, state,
    )
    # Gate LLM model: the default preset's model. Gates ignore the
    # node's params.preset — they're a separate dispatch and use the
    # workflow-level default. Falls back to "sonnet" only when no
    # presets are declared (script-only workflows that somehow declare
    # an LLM gate — unusual, but harmless default).
    default_gate_model = "sonnet"
    for preset in s.presets.values():
        if preset.default:
            default_gate_model = preset.model
            break
    eval_call = run_evaluation(
        node.evaluation,
        node_id,
        workdir=state.get("workdir", "."),
        node_output=node_output,
        workflow_name=config.name,
        attempt_number=retries + 1,
        backend=backend,
        default_model=default_gate_model,
        dep_outputs=dep_outputs,
        dep_structured_outputs=dep_structured,
        dep_worktrees=dep_worktrees,
    )
    try:
        if timeout is not None:
            eval_result = await asyncio.wait_for(eval_call, timeout=timeout)
        else:
            eval_result = await eval_call
    except asyncio.TimeoutError:
        return None, make_failure_update(
            node_id, f"Evaluation timed out after {timeout}s"
        )
    return eval_result, None


async def run_evaluation_and_outcome(
    node: Node,
    config: Graph,
    state: WorkflowState,
    node_output: str,
    timeout: float | None,
    backend: Any = None,
    *,
    node_id: str | None = None,
    history: list[dict[str, Any]] | None = None,
    effective_settings: Settings | None = None,
) -> dict[str, Any]:
    """``effective_settings`` (Phase 3 / scope-aware): when set, drives
    ``effective_max_retries`` and the LLM-gate ``default_model`` so a
    subgraph's own settings apply to its evaluations. Falls back to
    ``config.settings`` for top-level scope."""
    s = effective_settings or config.settings
    key = node_id or node.id
    max_retries = node.effective_max_retries(s)
    retries = state.get("retries", {}).get(key, 0)

    eval_result, failure = await _run_eval_core(
        node, config, state,
        node_id=key, node_output=node_output, retries=retries,
        backend=backend, timeout=timeout,
        effective_settings=effective_settings,
    )
    if failure is not None:
        return failure

    outcome = classify_evaluation_outcome(
        node, eval_result, retries, max_retries, history=history
    )
    return build_evaluation_outcome_update(
        node, eval_result, outcome, retries, max_retries, node_id=key
    )




def _make_execution_node(
    node: Node,
    config: Graph,
    executor: NodeExecutor | None = None,
    *,
    effective_settings: Settings | None = None,
):
    """Build the LangGraph node body for an execution node.

    ``effective_settings`` (Phase 3 / scope-aware): captured into the
    closure so ``effective_timeout``, ``effective_max_retries``,
    ``retry_backoff``, and the executor's ``settings_override`` all
    reflect this scope (top-level *or* subgraph). When ``None``, falls
    through to ``config.settings`` (top-level case).
    """
    settings = effective_settings or config.settings
    max_retries = node.effective_max_retries(settings)
    timeout = node.effective_timeout(settings)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        # Resume skip: a node frozen in the prior-run snapshot does not
        # re-execute. Its completed_nodes/node_outputs are already reseeded,
        # so downstream context and the deps join-barrier still see it.
        skip = state.get("_resume_skip")
        if skip and node.id in skip:
            return {}
        for check in (check_dep_failed, check_dry_run):
            if (r := check(node, state)) is not None:
                return r
        if node.depends_on and not all_deps_completed(node, state):
            # A gated predecessor routed here before its siblings finished.
            # Return no-op — subsequent pred completions re-fire this node.
            return {}
        if executor is None:
            update: dict[str, Any] = {
                "node_outputs": {node.id: f"[no-executor] {node.name}"},
            }
            if not node.evaluation:
                update["completed_nodes"] = {node.id}
            return update

        context = build_context(node, state)
        retry_count = state.get("retries", {}).get(node.id, 0)
        if retry_count > 0:
            delay = retry_delay(retry_count, settings.retry_backoff)
            if delay > 0:
                await asyncio.sleep(delay)
        context = inject_retry_reason(context, node, state, max_retries)

        if node.output_contract:
            scaffold_output_directory(
                node.output_contract, state.get("workdir", ".")
            )

        exec_result = await run_with_timeout(
            executor.execute(
                node, context, settings_override=effective_settings,
            ),
            timeout,
        )
        if not exec_result.success:
            return make_failure_update(node.id, exec_result.error)

        if node.output_contract:
            # Validate where the node actually ran — its foreman worktree
            # when active, else the workdir. Checking the workdir alone
            # reports worktree-written files as missing.
            contract_dir = exec_result.worktree or state.get("workdir", ".")
            missing = validate_output_contract(
                node.output_contract, contract_dir
            )
            if missing:
                return {
                    "failed_nodes": {node.id},
                    "errors": [
                        {
                            "node": node.id,
                            "error": (
                                f"Output contract violated: missing files: "
                                f"{', '.join(missing)}"
                            ),
                        }
                    ],
                    "node_outputs": {node.id: exec_result.output},
                }

        update = assemble_success_update(node, exec_result)
        if hasattr(executor, "get_worktree"):
            wt = executor.get_worktree(node.id)
            if wt:
                update["node_worktrees"] = {node.id: wt}
        if not node.evaluation:
            update["completed_nodes"] = {node.id}
        # Gated nodes hand off to _eval_{node.id} via plain edge; the
        # Evaluation node writes completed_nodes / retries / failed_nodes.

        return update

    node_fn.__name__ = f"node_{node.id}"
    return node_fn


def _make_gate_node(
    node: Node,
    config: Graph,
    executor: "NodeExecutor | None" = None,
    *,
    exec_id: str,
    pass_targets: list[str],
    effective_settings: Settings | None = None,
):
    """Create a gated node's gate — ONE Command-returning body that runs
    the validator, writes the ``EvaluationRecord``, classifies, and
    routes in a single super-step.

    Registered under ``_eval_<exec_id>`` and reached by a single plain
    edge ``exec_id → _eval_<exec_id>``. Returns
    ``Command(update=record+outcome, goto=...)`` (or a plain ``{}`` on
    defer). ``pass_goto`` is unwrapped to a single id when
    ``pass_targets`` has one entry, else emitted as the list (which may
    contain ``END``). ``exec_id`` is the retry goto target — parameterized
    per call site so a gated fan-out final retries its ``_final_<...>``
    synthetic id, not ``node.id``.

    ``executor`` (threaded from the registration site) is used for
    ``executor.get_backend()`` when the validator is an LLM ``.md`` judge;
    ``None`` yields the pinned no-PromptBackend ``ValueError`` if such a
    validator runs.

    History is read from state BEFORE this run's record (the pre-Stage-5d
    combined-factory convention); for the stock Evaluation sugar this is
    equivalent to including the current record, since the route criteria
    only reference ``result.*`` and ``invocation``.
    """
    settings = effective_settings or config.settings
    timeout = node.effective_timeout(settings)
    max_retries = node.effective_max_retries(settings)

    def _pass_goto() -> Any:
        return pass_targets[0] if len(pass_targets) == 1 else list(pass_targets)

    async def node_fn(state: WorkflowState):
        node_id = node.id

        # 1. Already failed (incl. the exec-infra-failure path) → END.
        if node_id in state.get("failed_nodes", set()):
            return Command(goto=END)

        # 2. Frozen on resume: do NOT re-run the validator (often an LLM
        #    judge). Still emit the pass goto so downstream — which has no
        #    static in-edge from a gated node — is reached with zero bill.
        if (skip := state.get("_resume_skip")) and node_id in skip:
            return Command(goto=_pass_goto())

        # 3. Dry-run: synthesize a passing record AND complete. Keeping the
        #    record is load-bearing — it drives the gate_evaluated event,
        #    {{evals.<id>.score}} templates, and include_eval preambles.
        if state.get("dry_run", False):
            record = EvaluationRecord.now(
                invocation=0,
                result={
                    "score": 1.0, "scores": {}, "feedback": "[dry-run]",
                    "pass_criteria_met": [], "pass_criteria_unmet": [],
                },
            )
            return Command(
                update={
                    "evaluations": {node_id: [record.to_dict()]},
                    "completed_nodes": {node_id},
                },
                goto=_pass_goto(),
            )

        # 4. Defer until upstream wrote node_outputs[node_id]. Key-absence
        #    (not empty value) because join nodes legitimately write "".
        #    A plain {} — LangGraph re-fires the gate when upstream writes.
        outputs = state.get("node_outputs", {})
        if node_id not in outputs:
            return {}
        output = outputs[node_id]

        history = list(state.get("evaluations", {}).get(node_id, []))
        retries = state.get("retries", {}).get(node_id, 0)
        backend = (
            executor.get_backend()
            if (executor is not None and hasattr(executor, "get_backend"))
            else None
        )

        # 5. Run the validator. A timeout is an infrastructure failure,
        #    distinct from a content evaluation: write failed_nodes with NO
        #    evaluations record (clearer than a synthetic score=0.0) and
        #    route END in one Command (no static out-edge remains).
        eval_result, failure = await _run_eval_core(
            node, config, state,
            node_id=node_id, node_output=output, retries=retries,
            backend=backend, timeout=timeout,
            effective_settings=effective_settings,
        )
        if failure is not None:
            return Command(update=failure, goto=END)

        # 6. Already completed (subgraph-reference wrappers pre-write
        #    completed_nodes; a goto re-fire of an already-passed node): the
        #    validator ran (record written above via step 5), but the verdict
        #    is NOT enforced — route pass with the record only.
        if node_id in state.get("completed_nodes", set()):
            return Command(
                update=build_record_only_update(
                    node, eval_result, retries, node_id=node_id,
                ),
                goto=_pass_goto(),
            )

        # 7. Classify and route. build_evaluation_outcome_update merges the
        #    record with the outcome-state writes in one dict.
        outcome = classify_evaluation_outcome(
            node, eval_result, retries, max_retries, history=history,
        )
        update = build_evaluation_outcome_update(
            node, eval_result, outcome, retries, max_retries, node_id=node_id,
        )
        if outcome == "retry":
            target = exec_id
        elif outcome == "fail_blocking":
            target = END
        else:  # pass or warn_continue
            target = _pass_goto()
        return Command(update=update, goto=target)

    node_fn.__name__ = f"eval_{node.id}"
    return node_fn
