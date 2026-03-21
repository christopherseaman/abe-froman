"""Tests for output directory scaffolding."""

from abe_froman.engine.builder import build_workflow_graph
from abe_froman.engine.contracts import scaffold_output_directory
from abe_froman.schema.models import OutputContract
from helpers import cmd_phase, make_config
from mock_executor import MockExecutor


class TestScaffoldOutputDirectory:
    """Unit tests for scaffold_output_directory()."""

    def test_scaffold_creates_directory(self, tmp_path):
        contract = OutputContract(
            base_directory="output",
            required_files=["report.md"],
        )
        scaffold_output_directory(contract, str(tmp_path))
        assert (tmp_path / "output").is_dir()

    def test_scaffold_creates_nested_directory(self, tmp_path):
        contract = OutputContract(
            base_directory="output/reports/final",
            required_files=["report.md"],
        )
        scaffold_output_directory(contract, str(tmp_path))
        assert (tmp_path / "output" / "reports" / "final").is_dir()

    def test_scaffold_existing_directory_noop(self, tmp_path):
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "existing.txt").write_text("keep")

        contract = OutputContract(
            base_directory="output",
            required_files=["report.md"],
        )
        scaffold_output_directory(contract, str(tmp_path))
        assert (tmp_path / "output").is_dir()
        assert (tmp_path / "output" / "existing.txt").read_text() == "keep"

    def test_scaffold_dot_base_directory(self, tmp_path):
        contract = OutputContract(
            base_directory=".",
            required_files=["report.md"],
        )
        scaffold_output_directory(contract, str(tmp_path))
        assert (tmp_path).is_dir()


class TestScaffoldingIntegration:
    """Integration tests through the full graph."""

    async def test_phase_creates_output_dir_before_execution(self, tmp_path):
        """Command phase writes to base_directory that doesn't pre-exist."""
        outdir = tmp_path / "results"
        phase = cmd_phase(
            "writer",
            output="hello",
            output_contract={
                "base_directory": "results",
                "required_files": [],
            },
        )
        config = make_config([phase])
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor=executor)

        result = await graph.ainvoke({"workdir": str(tmp_path)})

        assert outdir.is_dir()
        assert "writer" in result["completed_phases"]

    async def test_dry_run_does_not_scaffold(self, tmp_path):
        """Dry run skips execution and should not create directories."""
        phase = cmd_phase(
            "writer",
            output_contract={
                "base_directory": "results",
                "required_files": [],
            },
        )
        config = make_config([phase])
        graph = build_workflow_graph(config, executor=None)

        await graph.ainvoke({"workdir": str(tmp_path), "dry_run": True})

        assert not (tmp_path / "results").exists()

    async def test_scaffold_before_contract_validation(self, tmp_path):
        """Full flow: scaffold -> execute (write file) -> validate contract."""
        outdir = tmp_path / "output"
        outfile = outdir / "result.txt"

        phase = {
            "id": "produce",
            "name": "produce",
            "execution": {
                "type": "command",
                "command": "bash",
                "args": ["-c", f"echo -n done > {outfile}"],
            },
            "output_contract": {
                "base_directory": "output",
                "required_files": ["result.txt"],
            },
        }
        config = make_config([phase])

        from abe_froman.executor.dispatch import DispatchExecutor

        graph = build_workflow_graph(
            config,
            executor=DispatchExecutor(workdir=str(tmp_path), settings=config.settings),
        )

        result = await graph.ainvoke({"workdir": str(tmp_path)})

        assert outdir.is_dir()
        assert outfile.exists()
        assert outfile.read_text() == "done"
        assert "produce" in result["completed_phases"]
        assert not result.get("failed_phases")
