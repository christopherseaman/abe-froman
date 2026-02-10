from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from abe_froman.engine.gates import evaluate_gate
from abe_froman.engine.state import WorkflowState
from abe_froman.schema.models import Phase, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.executor.base import PhaseExecutor


def _find_terminal_phases(config: WorkflowConfig) -> set[str]:
    """Return IDs of phases that no other phase depends on."""
    depended_on: set[str] = set()
    for phase in config.phases:
        depended_on.update(phase.depends_on)
    return {p.id for p in config.phases if p.id not in depended_on}


def _detect_cycles(config: WorkflowConfig) -> None:
    """Detect circular dependencies via DFS."""
    adj: dict[str, list[str]] = {p.id: list(p.depends_on) for p in config.phases}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {pid: WHITE for pid in adj}

    def dfs(node: str) -> None:
        color[node] = GRAY
        for dep in adj[node]:
            if dep not in color:
                continue
            if color[dep] == GRAY:
                raise ValueError(
                    f"Circular dependency detected involving '{node}' and '{dep}'"
                )
            if color[dep] == WHITE:
                dfs(dep)
        color[node] = BLACK

    for node in adj:
        if color[node] == WHITE:
            dfs(node)


def _make_phase_node(
    phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    """Create a node function for a phase.

    The node function handles:
    - Skipping if a dependency failed
    - Dry-run mode (trace without executing)
    - Calling the executor and collecting results
    - Evaluating quality gates after execution
    - Populating gate_scores and retries in state
    """
    max_retries = phase.effective_max_retries(config.settings)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        # Skip if any dependency failed
        failed = state.get("failed_phases", [])
        for dep in phase.depends_on:
            if dep in failed:
                return {
                    "failed_phases": [phase.id],
                    "errors": [
                        {
                            "phase": phase.id,
                            "error": f"Skipped: dependency '{dep}' failed",
                        }
                    ],
                }

        if state.get("dry_run", False):
            update: dict[str, Any] = {
                "completed_phases": [phase.id],
                "phase_outputs": {phase.id: f"[dry-run] {phase.name}"},
            }
            # Set gate score to 1.0 so gate router routes to "pass"
            if phase.quality_gate:
                update["gate_scores"] = {phase.id: 1.0}
            return update

        if executor is None:
            update = {
                "completed_phases": [phase.id],
                "phase_outputs": {phase.id: f"[no-executor] {phase.name}"},
            }
            if phase.quality_gate:
                update["gate_scores"] = {phase.id: 1.0}
            return update

        from abe_froman.executor.base import PhaseResult

        # Build context from dependency outputs
        context: dict[str, Any] = {}
        for dep in phase.depends_on:
            if dep in state.get("phase_outputs", {}):
                context[dep] = state["phase_outputs"][dep]
            if dep in state.get("phase_structured_outputs", {}):
                context[f"{dep}_structured"] = state["phase_structured_outputs"][dep]

        result: PhaseResult = await executor.execute(phase, context)

        if not result.success:
            return {
                "failed_phases": [phase.id],
                "errors": [{"phase": phase.id, "error": result.error}],
            }

        update: dict[str, Any] = {
            "phase_outputs": {phase.id: result.output},
        }
        if result.structured_output is not None:
            update["phase_structured_outputs"] = {
                phase.id: result.structured_output
            }

        # Evaluate quality gate if present
        if phase.quality_gate:
            retries = state.get("retries", {}).get(phase.id, 0)
            score = await evaluate_gate(
                phase.quality_gate,
                phase.id,
                workdir=state.get("workdir", "."),
                phase_output=result.output,
            )
            update["gate_scores"] = {phase.id: score}

            if score >= phase.quality_gate.threshold:
                update["completed_phases"] = [phase.id]
            elif retries < max_retries:
                update["retries"] = {phase.id: retries + 1}
                # Don't add to completed — gate router will send back to retry
            else:
                if phase.quality_gate.blocking:
                    update["failed_phases"] = [phase.id]
                    update["errors"] = [
                        {
                            "phase": phase.id,
                            "error": (
                                f"Quality gate failed after {max_retries} retries "
                                f"(score={score:.2f}, threshold={phase.quality_gate.threshold})"
                            ),
                        }
                    ]
                else:
                    # Non-blocking gate: pass through with warning
                    update["completed_phases"] = [phase.id]
                    update["errors"] = [
                        {
                            "phase": phase.id,
                            "error": (
                                f"Quality gate below threshold after {max_retries} retries "
                                f"(score={score:.2f}, threshold={phase.quality_gate.threshold}), "
                                f"continuing (non-blocking)"
                            ),
                        }
                    ]
        else:
            update["completed_phases"] = [phase.id]

        return update

    node_fn.__name__ = f"node_{phase.id}"
    return node_fn


def _make_gate_router(phase: Phase, max_retries: int):
    """Create a conditional routing function for quality gates."""

    def router(state: WorkflowState) -> str:
        score = state.get("gate_scores", {}).get(phase.id, 0.0)
        threshold = phase.quality_gate.threshold
        retries = state.get("retries", {}).get(phase.id, 0)

        if score >= threshold:
            return "pass"
        elif retries < max_retries:
            return "retry"
        elif not phase.quality_gate.blocking:
            return "pass"
        else:
            return "fail"

    return router


def _read_manifest(state: WorkflowState, phase: Phase) -> list[dict]:
    """Read manifest items from phase output or from disk.

    Tries parsing the phase's output as JSON first (looking for an "items"
    key or a bare list).  Falls back to reading manifest_path from disk.
    """
    import json
    from pathlib import Path

    output = state.get("phase_outputs", {}).get(phase.id, "")
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    if phase.dynamic_subphases and phase.dynamic_subphases.manifest_path:
        manifest_file = (
            Path(state.get("workdir", ".")) / phase.dynamic_subphases.manifest_path
        )
        try:
            data = json.loads(manifest_file.read_text())
            if isinstance(data, dict) and "items" in data:
                return data["items"]
            if isinstance(data, list):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    return []


def _make_subphase_node(
    parent_phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    """Create a template node function for dynamic subphases.

    Each invocation receives a different ``_subphase_item`` via LangGraph's
    Send — the manifest item dict for this particular subphase instance.
    """
    template = parent_phase.dynamic_subphases.template

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        import json as _json

        item = state.get("_subphase_item", {})
        item_id = item.get("id", "unknown")
        subphase_id = f"{parent_phase.id}::{item_id}"

        if state.get("dry_run", False):
            return {
                "completed_phases": [subphase_id],
                "phase_outputs": {subphase_id: f"[dry-run] subphase {item_id}"},
                "subphase_outputs": {subphase_id: f"[dry-run] subphase {item_id}"},
            }

        if executor is None:
            return {
                "completed_phases": [subphase_id],
                "phase_outputs": {subphase_id: f"[no-executor] subphase {item_id}"},
                "subphase_outputs": {subphase_id: f"[no-executor] subphase {item_id}"},
            }

        synthetic_phase = Phase(
            id=subphase_id,
            name=f"{parent_phase.name} - {item.get('name', item_id)}",
            prompt_file=template.prompt_file,
            model=parent_phase.model,
        )

        # Context: parent output + each manifest item field as a template var
        context: dict[str, Any] = {}
        parent_output = state.get("phase_outputs", {}).get(parent_phase.id, "")
        context[parent_phase.id] = parent_output
        for key, value in item.items():
            context[key] = str(value)

        from abe_froman.executor.base import PhaseResult

        result: PhaseResult = await executor.execute(synthetic_phase, context)

        if not result.success:
            return {
                "failed_phases": [subphase_id],
                "errors": [{"phase": subphase_id, "error": result.error}],
            }

        update: dict[str, Any] = {
            "completed_phases": [subphase_id],
            "phase_outputs": {subphase_id: result.output},
            "subphase_outputs": {subphase_id: result.output},
        }

        if template.quality_gate:
            score = await evaluate_gate(
                template.quality_gate,
                subphase_id,
                workdir=state.get("workdir", "."),
                phase_output=result.output,
            )
            update["gate_scores"] = {subphase_id: score}

        return update

    node_fn.__name__ = f"subphase_{parent_phase.id}"
    return node_fn


def _make_dynamic_router(phase: Phase, config: WorkflowConfig):
    """Create a conditional edge function that handles gate + Send fan-out.

    On pass (or non-blocking exhausted): reads the manifest and returns
    a list of Send objects to dispatch the template node for each item.
    On retry/fail: returns the routing string as usual.
    """
    max_retries = phase.effective_max_retries(config.settings)
    template_node_id = f"_sub_{phase.id}"

    # Determine what to route to when manifest is empty
    dsc = phase.dynamic_subphases
    if dsc.final_phases:
        no_items_target = f"_final_{phase.id}_{dsc.final_phases[0].id}"
    else:
        no_items_target = None  # will be set in build_workflow_graph

    def router(state: WorkflowState):
        # Gate check (same logic as _make_gate_router)
        if phase.quality_gate:
            score = state.get("gate_scores", {}).get(phase.id, 0.0)
            threshold = phase.quality_gate.threshold
            retries = state.get("retries", {}).get(phase.id, 0)

            if score < threshold:
                if retries < max_retries:
                    return "retry"
                elif phase.quality_gate.blocking:
                    return "fail"
                # non-blocking: fall through to fan-out

        # Read manifest and fan out
        items = _read_manifest(state, phase)
        if not items:
            return "no_items"

        return [
            Send(template_node_id, {**state, "_subphase_item": item})
            for item in items
        ]

    return router, no_items_target


def _make_final_phase_node(
    parent_phase: Phase,
    final_phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    """Create a node function for a final phase in a dynamic subphase group.

    Final phases receive all subphase outputs for the parent in their context.
    """
    node_id = f"_final_{parent_phase.id}_{final_phase.id}"

    # Build a synthetic Phase from the FinalPhase schema object
    synthetic = Phase(
        id=node_id,
        name=final_phase.name,
        description=final_phase.description,
        prompt_file=final_phase.prompt_file,
        execution=final_phase.execution,
        quality_gate=final_phase.quality_gate,
        model=parent_phase.model,
    )

    # Reuse _make_phase_node for execution + gate logic, but wrap it to
    # inject subphase outputs into context.
    inner = _make_phase_node(synthetic, config, executor)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        # Collect all subphase outputs for this parent into phase_outputs
        # so the inner node can pick them up via dependency context.
        # We inject a synthetic key `_all_subphases` with aggregated output.
        import json as _json

        prefix = f"{parent_phase.id}::"
        sub_outputs = {
            k: v
            for k, v in state.get("subphase_outputs", {}).items()
            if k.startswith(prefix)
        }

        # Make subphase outputs available as phase_outputs so the inner
        # node's context builder can find them. Also inject a summary key.
        enriched = dict(state)
        enriched_outputs = dict(state.get("phase_outputs", {}))
        enriched_outputs[f"{parent_phase.id}_subphases"] = _json.dumps(sub_outputs)
        enriched["phase_outputs"] = enriched_outputs

        return await inner(enriched)

    node_fn.__name__ = f"final_{parent_phase.id}_{final_phase.id}"
    return node_fn


def build_workflow_graph(
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    Each phase becomes a node. Dependencies become edges. Quality gates
    add conditional routing (pass/retry/fail) after the gated phase.
    Dynamic subphases use LangGraph Send for runtime fan-out.
    """
    _detect_cycles(config)

    builder = StateGraph(WorkflowState)
    terminal_ids = _find_terminal_phases(config)
    phase_map = {p.id: p for p in config.phases}

    # --- Identify special phase types ---
    gated_phase_ids: set[str] = set()
    dynamic_phase_ids: set[str] = set()

    for phase in config.phases:
        if phase.quality_gate:
            gated_phase_ids.add(phase.id)
        if phase.dynamic_subphases and phase.dynamic_subphases.enabled:
            dynamic_phase_ids.add(phase.id)

    # --- Add nodes ---
    for phase in config.phases:
        builder.add_node(phase.id, _make_phase_node(phase, config, executor))

    # Add template + final nodes for dynamic phases
    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases

        # Template subphase node (dispatched N times via Send)
        template_node_id = f"_sub_{phase.id}"
        builder.add_node(
            template_node_id,
            _make_subphase_node(phase, config, executor),
        )

        # Final phase nodes
        for final_phase in dsc.final_phases:
            final_node_id = f"_final_{phase.id}_{final_phase.id}"
            builder.add_node(
                final_node_id,
                _make_final_phase_node(phase, final_phase, config, executor),
            )

    # --- Build exit_node map ---
    # For dynamic phases, downstream dependents wire from the exit node
    # (last final phase or template node) rather than the parent directly.
    exit_node: dict[str, str] = {}
    # Track which nodes need conditional edges (gated or dynamic)
    needs_conditional: set[str] = set()

    for phase in config.phases:
        if phase.id in dynamic_phase_ids:
            dsc = phase.dynamic_subphases
            if dsc.final_phases:
                exit_node[phase.id] = (
                    f"_final_{phase.id}_{dsc.final_phases[-1].id}"
                )
            else:
                exit_node[phase.id] = f"_sub_{phase.id}"
            needs_conditional.add(phase.id)
        elif phase.id in gated_phase_ids:
            exit_node[phase.id] = phase.id  # handled by conditional edges
            needs_conditional.add(phase.id)
        else:
            exit_node[phase.id] = phase.id

    # --- Wire dependency edges ---
    has_incoming: set[str] = set()

    for phase in config.phases:
        if not phase.depends_on:
            continue

        for dep in phase.depends_on:
            if dep in needs_conditional:
                # Conditional edges handle routing from this dep
                pass
            else:
                builder.add_edge(exit_node[dep], phase.id)
            has_incoming.add(phase.id)

    # Root phases get edge from START
    for phase in config.phases:
        if phase.id not in has_incoming:
            builder.add_edge(START, phase.id)

    # --- Wire internal edges for dynamic phases ---
    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases
        template_node_id = f"_sub_{phase.id}"

        # Chain: template -> final[0] -> final[1] -> ... -> exit
        if dsc.final_phases:
            first_final = f"_final_{phase.id}_{dsc.final_phases[0].id}"
            builder.add_edge(template_node_id, first_final)

            for i in range(len(dsc.final_phases) - 1):
                current = f"_final_{phase.id}_{dsc.final_phases[i].id}"
                next_ = f"_final_{phase.id}_{dsc.final_phases[i + 1].id}"
                builder.add_edge(current, next_)

        # Wire exit node to dependents or END
        exit_id = exit_node[phase.id]
        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]

        if not dependents:
            # Terminal dynamic phase
            builder.add_edge(exit_id, END)
        elif len(dependents) == 1:
            builder.add_edge(exit_id, dependents[0])
        else:
            # Multiple dependents from exit node
            for dep_id in dependents:
                builder.add_edge(exit_id, dep_id)

        # Wire the conditional edge from parent -> Send fan-out
        router, no_items_target = _make_dynamic_router(phase, config)
        route_map: dict[str, str] = {"retry": phase.id, "fail": END}

        if no_items_target:
            route_map["no_items"] = no_items_target
        elif dependents:
            route_map["no_items"] = dependents[0]
        else:
            route_map["no_items"] = END

        builder.add_conditional_edges(phase.id, router, route_map)

    # --- Wire conditional edges for non-dynamic gated phases ---
    for phase in config.phases:
        if phase.id not in gated_phase_ids or phase.id in dynamic_phase_ids:
            continue

        max_retries = phase.effective_max_retries(config.settings)

        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]

        if phase.id in terminal_ids:
            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": END, "retry": phase.id, "fail": END},
            )
        elif len(dependents) == 1:
            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": dependents[0], "retry": phase.id, "fail": END},
            )
        else:
            passthrough_id = f"_after_{phase.id}"

            async def passthrough(state: WorkflowState) -> dict[str, Any]:
                return {}

            passthrough.__name__ = f"passthrough_{phase.id}"
            builder.add_node(passthrough_id, passthrough)

            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": passthrough_id, "retry": phase.id, "fail": END},
            )
            for dep_id in dependents:
                builder.add_edge(passthrough_id, dep_id)

    # Terminal non-gated, non-dynamic phases get edge to END
    for phase in config.phases:
        if (
            phase.id in terminal_ids
            and phase.id not in gated_phase_ids
            and phase.id not in dynamic_phase_ids
        ):
            builder.add_edge(phase.id, END)

    return builder.compile()
