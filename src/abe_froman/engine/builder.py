from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

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
        else:
            return "fail"

    return router


def build_workflow_graph(
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    Each phase becomes a node. Dependencies become edges. Quality gates
    add conditional routing (pass/retry/fail) after the gated phase.
    """
    _detect_cycles(config)

    builder = StateGraph(WorkflowState)
    terminal_ids = _find_terminal_phases(config)

    for phase in config.phases:
        builder.add_node(phase.id, _make_phase_node(phase, config, executor))

    # Phases with quality gates need conditional routing.
    # The gate evaluation happens inside the node; the conditional edge
    # reads gate_scores to decide pass/retry/fail.
    gated_phase_ids: set[str] = set()

    for phase in config.phases:
        if phase.quality_gate:
            gated_phase_ids.add(phase.id)

    # Wire dependency edges
    has_incoming: set[str] = set()

    for phase in config.phases:
        if not phase.depends_on:
            continue

        for dep in phase.depends_on:
            if dep in gated_phase_ids:
                # Don't add direct edge from gated phase — conditional edges
                # handle the routing. The "pass" route will go to this phase.
                pass
            else:
                builder.add_edge(dep, phase.id)
            has_incoming.add(phase.id)

    # Root phases get edge from START
    for phase in config.phases:
        if phase.id not in has_incoming:
            builder.add_edge(START, phase.id)

    # Wire conditional edges for gated phases
    for phase in config.phases:
        if phase.id not in gated_phase_ids:
            continue

        max_retries = phase.effective_max_retries(config.settings)

        # Find what comes after this gated phase
        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]

        if phase.id in terminal_ids:
            # Terminal gated phase: pass→END, retry→self, fail→END
            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": END, "retry": phase.id, "fail": END},
            )
        elif len(dependents) == 1:
            # Single dependent: pass→dependent, retry→self, fail→END
            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": dependents[0], "retry": phase.id, "fail": END},
            )
        else:
            # Multiple dependents: can't fan-out from conditional edge directly.
            # Add a passthrough node that fans out to all dependents.
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

    # Terminal non-gated phases get edge to END
    for phase in config.phases:
        if phase.id in terminal_ids and phase.id not in gated_phase_ids:
            builder.add_edge(phase.id, END)

    return builder.compile()
