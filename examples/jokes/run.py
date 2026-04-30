"""Run the joke workflow and print detailed output."""
import asyncio
from pathlib import Path

import yaml

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.backends.factory import create_prompt_backend
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import Graph


async def main():
    config = Graph(**yaml.safe_load(Path("examples/jokes/workflow.yaml").read_text()))
    backend = create_prompt_backend("acp")
    executor = DispatchExecutor(workdir=".", prompt_backend=backend, settings=config.settings)
    compiled = build_workflow_graph(config, executor)
    state = make_initial_state(workflow_name=config.name, workdir=".")
    result = await compiled.ainvoke(state)

    print("=== Workflow Result ===")
    print(f"Completed: {result.get('completed_nodes', [])}")
    print(f"Failed: {result.get('failed_nodes', [])}")

    for err in result.get("errors", []):
        print(f"  Error: {err}")

    scores = {
        node: records[-1].get("result", {}).get("score")
        for node, records in result.get("evaluations", {}).items()
        if records
    }
    print(f"\nGate scores: {scores}")
    print(f"Retries: {result.get('retries', {})}")

    for node_id, output in result.get("node_outputs", {}).items():
        print(f"\n--- {node_id} ---")
        print(output)

    await executor.close()


asyncio.run(main())
