"""Run the joke workflow and print detailed output."""
import asyncio
from pathlib import Path

import yaml

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.backends.factory import create_prompt_backend
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import WorkflowConfig


async def main():
    config = WorkflowConfig(**yaml.safe_load(Path("examples/jokes/workflow.yaml").read_text()))
    backend = create_prompt_backend("acp")
    executor = DispatchExecutor(workdir=".", prompt_backend=backend, settings=config.settings)
    compiled = build_workflow_graph(config, executor)
    state = make_initial_state(workflow_name=config.name, workdir=".")
    result = await compiled.ainvoke(state)

    print("=== Workflow Result ===")
    print(f"Completed: {result.get('completed_phases', [])}")
    print(f"Failed: {result.get('failed_phases', [])}")

    for err in result.get("errors", []):
        print(f"  Error: {err}")

    print(f"\nGate scores: {result.get('gate_scores', {})}")
    print(f"Retries: {result.get('retries', {})}")

    for phase_id, output in result.get("phase_outputs", {}).items():
        print(f"\n--- {phase_id} ---")
        print(output)

    await executor.close()


asyncio.run(main())
