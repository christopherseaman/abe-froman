"""Full E2E: ACP generates jokes -> deterministic gate validates JSON
schema -> ACP selects best joke. Exercises the entire pipeline with
real Claude execution.

Lives under tests/acp/ because it requires a real ACP backend; the
collection-time pre-flight in tests/conftest.py and the
``--ignore=tests/acp`` exclusion both apply.
"""

import json

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.backends.acp import ACPBackend
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state

from helpers import make_config


@pytest.mark.acp
class TestJokeWorkflowIntegration:
    @pytest.mark.asyncio
    async def test_generate_gate_select(self, tmp_path):
        generate_prompt = tmp_path / "generate.md"
        generate_prompt.write_text(
            'Generate exactly 3 short jokes. Respond with ONLY valid JSON, '
            'no markdown, no code fences:\n'
            '{"jokes": ["joke 1", "joke 2", "joke 3"]}'
        )
        select_prompt = tmp_path / "select.md"
        select_prompt.write_text(
            "Here are some jokes:\n\n{{generate}}\n\n"
            "Pick the funniest one. Respond with ONLY the joke text."
        )

        validator = tmp_path / "validate.py"
        validator.write_text(
            "import json, sys\n"
            "raw = sys.stdin.read().strip()\n"
            "try:\n"
            "    data = json.loads(raw)\n"
            "    jokes = data.get('jokes', [])\n"
            "    if isinstance(jokes, list) and len(jokes) == 3 "
            "and all(isinstance(j, str) and j for j in jokes):\n"
            "        print('1.0')\n"
            "    else:\n"
            "        print('0.0')\n"
            "except Exception:\n"
            "    print('0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "generate",
                    "name": "Generate Jokes",
                    "execute": {"url": "generate.md"},
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 2,
                    },
                },
                {
                    "id": "select",
                    "name": "Select Best",
                    "execute": {"url": "select.md"},
                    "depends_on": ["generate"],
                },
            ],
            presets={
                "default": {
                    "transport": "acp", "provider": "anthropic",
                    "model": "sonnet", "default": True,
                },
            },
        )

        backend = ACPBackend()
        executor = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backends={"default": backend},
            settings=config.settings,
        )
        try:
            graph = build_workflow_graph(config, executor)
            result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

            assert "generate" in result["completed_nodes"]
            assert "select" in result["completed_nodes"]
            assert result["evaluations"]["generate"][-1]["result"]["score"] == 1.0

            gen_output = result["node_outputs"]["generate"]
            data = json.loads(gen_output)
            assert len(data["jokes"]) == 3

            assert len(result["node_outputs"]["select"]) > 0
        finally:
            await executor.close()
