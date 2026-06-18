"""Unit tests for output contract validation."""

from sqrlly.runtime.gates import validate_output_contract
from sqrlly.schema.models import OutputContract


class TestValidateOutputContract:
    def test_all_files_present(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()
        (base / "report.md").write_text("content")
        (base / "data.json").write_text("{}")

        contract = OutputContract(
            base_directory="output",
            required_files=["report.md", "data.json"],
        )
        assert validate_output_contract(contract, str(tmp_path)) == []

    def test_missing_file(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()
        (base / "report.md").write_text("content")

        contract = OutputContract(
            base_directory="output",
            required_files=["report.md", "missing.txt"],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["output/missing.txt"]

    def test_multiple_missing(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()

        contract = OutputContract(
            base_directory="output",
            required_files=["a.txt", "b.txt", "c.txt"],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["output/a.txt", "output/b.txt", "output/c.txt"]

    def test_empty_required_files(self, tmp_path):
        contract = OutputContract(
            base_directory="output",
            required_files=[],
        )
        assert validate_output_contract(contract, str(tmp_path)) == []

    def test_base_directory_missing(self, tmp_path):
        contract = OutputContract(
            base_directory="nonexistent",
            required_files=["file.txt"],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["nonexistent/file.txt"]


class TestRequiredPaths:
    def test_prepends_base_directory(self):
        contract = OutputContract(
            base_directory="reference",
            required_files=["prd-context-map.json", "sub/x.txt"],
        )
        assert contract.required_paths() == [
            "reference/prd-context-map.json",
            "reference/sub/x.txt",
        ]

    def test_dot_base_collapses_to_bare_name(self):
        contract = OutputContract(base_directory=".", required_files=["x.json"])
        assert contract.required_paths() == ["x.json"]

    def test_empty_required_files(self):
        contract = OutputContract(base_directory="reference", required_files=[])
        assert contract.required_paths() == []
