"""Single-phase compilation helper.

build_phase_subgraph bundles the node function and optional gate router
for a single phase, ready for the top-level graph builder to wire in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from abe_froman.compile.nodes import _make_phase_node
from abe_froman.compile.routers import _make_gate_router
from abe_froman.schema.models import Phase, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.runtime.executor.base import PhaseExecutor


@dataclass(frozen=True)
class PhaseSubgraph:
    node_fn: Any
    router: Any | None
    needs_conditional: bool


def build_phase_subgraph(
    phase: Phase, config: WorkflowConfig, executor: PhaseExecutor | None = None
) -> PhaseSubgraph:
    node = _make_phase_node(phase, config, executor)
    if phase.quality_gate:
        router = _make_gate_router(
            phase, phase.effective_max_retries(config.settings)
        )
        return PhaseSubgraph(
            node_fn=node, router=router, needs_conditional=True
        )
    return PhaseSubgraph(node_fn=node, router=None, needs_conditional=False)
