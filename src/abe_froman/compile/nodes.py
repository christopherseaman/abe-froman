"""Node node factory and decomposed helpers.

_make_execution_node returns an async callable for StateGraph.add_node.
Pure helpers (check_*, build_context, classify_evaluation_outcome, etc.)
operate on WorkflowState/Node dicts with no langgraph dependency.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from abe_froman.compile.evaluation import (
    EvaluationRecord,
    build_eval_context,
    evaluation_fallback,
    evaluation_to_routes,
    walk_routes,
)
from abe_froman.runtime.gates import (
    EvaluationResult,
    build_eval_preamble,
    scaffold_output_directory,
    validate_output_contract,
)
from abe_froman.runtime.gates import run_evaluation
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Node, Settings, Graph

if TYPE_CHECKING:
    from abe_froman.runtime.result import NodeExecutor


def _get_retry_delay(retry_count: int, backoff: list[float]) -> float:
    """Return delay in seconds for the given retry attempt (1-indexed).

    Uses the backoff list, clamping to the last value for attempts
    beyond the list length. Returns 0.0 if backoff is empty.
    """
    if not backoff:
        return 0.0
    idx = min(retry_count - 1, len(backoff) - 1)
    return backoff[idx]


def check_dep_failed(node: Node, state: WorkflowState) -> dict | None:
    failed = state.get("failed_nodes", [])
    for dep in node.depends_on:
        if dep in failed:
            return {
                "failed_nodes": [node.id],
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

    Multi-predecessor nodes whose preds are gated get triggered by each
    pred's router independently (conditional edges). Returning {} from the
    node body until all preds are done causes LangGraph to re-fire the
    node on each subsequent pred-trigger — a natural join barrier.
    """
    completed = set(state.get("completed_nodes", []))
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
        update["completed_nodes"] = [node.id]
    return update


def build_context(node: Node, state: WorkflowState) -> dict[str, Any]:
    import json as _json

    context: dict[str, Any] = {}
    outputs = state.get("node_outputs", {})
    structured = state.get("node_structured_outputs", {})
    worktrees = state.get("node_worktrees", {})
    sub_outputs = state.get("child_outputs", {})
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
        # a dynamic parent sees `{{dep_subphases}}` (JSON id→output map) and
        # `{{dep_subphase_worktrees}}` (JSON list of worktree paths) — not
        # just the final-node wrapper.
        prefix = f"{dep}::"
        dep_subs = {k: v for k, v in sub_outputs.items() if k.startswith(prefix)}
        if dep_subs:
            context[f"{dep}_subphases"] = _json.dumps(dep_subs)
            dep_wts = [v for k, v in worktrees.items() if k.startswith(prefix)]
            context[f"{dep}_subphase_worktrees"] = _json.dumps(dep_wts)

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


async def execute_with_timeout(
    executor, node: Node, context: dict[str, Any], timeout: float | None,
    *, settings_override: Settings | None = None,
) -> ExecutionResult | str:
    """Run executor.execute with optional timeout + scope settings override.

    ``settings_override`` (Phase 3 / scope-aware): the scope's effective
    settings, threaded into ``NodeExecutor.execute`` so a subgraph's
    ``default_model``, ``base_url``, etc. apply to its own nodes.
    """
    try:
        if timeout is not None:
            result = await asyncio.wait_for(
                executor.execute(
                    node, context, settings_override=settings_override,
                ),
                timeout=timeout,
            )
        else:
            result = await executor.execute(
                node, context, settings_override=settings_override,
            )
        return result
    except asyncio.TimeoutError:
        return "timeout"


def make_failure_update(node_id: str, error_message: str) -> dict[str, Any]:
    return {
        "failed_nodes": [node_id],
        "errors": [{"node": node_id, "error": error_message}],
    }


def assemble_success_update(node: Node, result: ExecutionResult) -> dict[str, Any]:
    update: dict[str, Any] = {
        "node_outputs": {node.id: result.output},
    }
    if result.structured_output is not None:
        update["node_structured_outputs"] = {node.id: result.structured_output}
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
    if evaluation is not None and getattr(evaluation, "dimensions", None):
        for d in evaluation.dimensions:
            scores.setdefault(d.field, 0.0)
    return {
        "score": eval_result.score,
        "scores": scores,
        "reasons": dict(eval_result.reasons),
        "feedback": eval_result.feedback,
        "pass_criteria_met": list(eval_result.pass_criteria_met),
        "pass_criteria_unmet": list(eval_result.pass_criteria_unmet),
    }


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
            f"{d.field}={result.scores.get(d.field, 0.0):.2f}>={d.min}"
            for d in evaluation.dimensions
        ]
        return ", ".join(parts)
    return f"score={result.score:.2f}, threshold={evaluation.threshold}"


def build_evaluation_outcome_update(
    node: Node,
    result: EvaluationResult,
    outcome: str,
    retries: int,
    max_retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    key = node_id or node.id
    record = EvaluationRecord.now(
        invocation=retries,
        result=_evaluation_result_payload(result, node.evaluation),
    )
    update: dict[str, Any] = {
        "evaluations": {key: [record.to_dict()]},
    }
    summary = _evaluation_summary(node, result)

    if outcome == "pass":
        update["completed_nodes"] = [key]
    elif outcome == "retry":
        update["retries"] = {key: retries + 1}
    elif outcome == "fail_blocking":
        update["failed_nodes"] = [key]
        update["errors"] = [
            {
                "node": key,
                "error": f"Evaluation failed after {max_retries} retries ({summary})",
            }
        ]
    elif outcome == "warn_continue":
        update["completed_nodes"] = [key]
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


async def run_evaluation_and_outcome(
    node: Node,
    config: Graph,
    state: WorkflowState,
    result: ExecutionResult,
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

    eval_call = run_evaluation(
        node.evaluation,
        key,
        workdir=state.get("workdir", "."),
        node_output=result.output,
        workflow_name=config.name,
        attempt_number=retries + 1,
        backend=backend,
        default_model=s.default_model,
    )
    try:
        if timeout is not None:
            eval_result = await asyncio.wait_for(eval_call, timeout=timeout)
        else:
            eval_result = await eval_call
    except asyncio.TimeoutError:
        return make_failure_update(
            key, f"Evaluation timed out after {timeout}s"
        )

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
                update["completed_nodes"] = [node.id]
            return update

        context = build_context(node, state)
        retry_count = state.get("retries", {}).get(node.id, 0)
        if retry_count > 0:
            delay = _get_retry_delay(retry_count, settings.retry_backoff)
            if delay > 0:
                await asyncio.sleep(delay)
        context = inject_retry_reason(context, node, state, max_retries)

        if node.output_contract:
            scaffold_output_directory(
                node.output_contract, state.get("workdir", ".")
            )

        exec_result = await execute_with_timeout(
            executor, node, context, timeout,
            settings_override=effective_settings,
        )
        if exec_result == "timeout":
            return make_failure_update(
                node.id, f"Node timed out after {timeout}s"
            )
        if not exec_result.success:
            return make_failure_update(node.id, exec_result.error)

        if node.output_contract:
            missing = validate_output_contract(
                node.output_contract, state.get("workdir", ".")
            )
            if missing:
                return {
                    "failed_nodes": [node.id],
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
            update["completed_nodes"] = [node.id]
        # Gated nodes hand off to _eval_{node.id} via plain edge; the
        # Evaluation node writes completed_nodes / retries / failed_nodes.

        return update

    node_fn.__name__ = f"node_{node.id}"
    return node_fn


def _make_evaluation_node(
    node: Node,
    config: Graph,
    executor: "NodeExecutor | None" = None,
    *,
    node_id_resolver: Callable[[WorkflowState], str] | None = None,
    effective_settings: Settings | None = None,
):
    """Create the Evaluation node — second half of a gated node pair.

    Reads `node_outputs[node_id]` (produced by the upstream Execution
    node), runs the gate, walks routes (first-match + catch-all fallback),
    and writes an `EvaluationRecord` plus the outcome's state transitions.

    `node_id_resolver` lets child eval nodes derive the per-branch id
    from `state._fan_out_item`. Defaults to `node.id` for top-level use.

    ``effective_settings`` (Phase 3 / scope-aware): drives the eval
    timeout and feeds ``run_evaluation_and_outcome`` the scope's
    ``default_model`` for ``.md`` LLM gates.
    """
    settings = effective_settings or config.settings
    timeout = node.effective_timeout(settings)
    resolve = node_id_resolver or (lambda _s: node.id)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        node_id = resolve(state)

        if node_id in state.get("failed_nodes", []):
            return {}

        if state.get("dry_run", False):
            record = EvaluationRecord.now(
                invocation=0,
                result={
                    "score": 1.0,
                    "scores": {},
                    "feedback": "[dry-run]",
                    "pass_criteria_met": [],
                    "pass_criteria_unmet": [],
                },
            )
            return {
                "evaluations": {node_id: [record.to_dict()]},
                "completed_nodes": [node_id],
            }

        history = list(state.get("evaluations", {}).get(node_id, []))
        outputs = state.get("node_outputs", {})
        # Defer until upstream wrote node_outputs[node_id]. Key-absence
        # rather than empty-value because join nodes (Execute(type="join"))
        # legitimately write "". See test_defers_when_upstream_output_absent.
        if node_id not in outputs:
            return {}
        output = outputs[node_id]
        structured = state.get("node_structured_outputs", {}).get(node_id)
        synthetic_result = ExecutionResult(
            success=True, output=output, structured_output=structured
        )

        backend = (
            executor.get_backend()
            if (executor is not None and hasattr(executor, "get_backend"))
            else None
        )
        return await run_evaluation_and_outcome(
            node,
            config,
            state,
            synthetic_result,
            timeout,
            backend=backend,
            node_id=node_id,
            history=history,
            effective_settings=effective_settings,
        )

    node_fn.__name__ = f"eval_{node.id}"
    return node_fn
