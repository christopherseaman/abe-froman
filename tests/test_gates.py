import pytest

from abe_froman.engine.gates import evaluate_gate
from abe_froman.schema.models import QualityGate


class TestGateEvaluation:
    @pytest.mark.asyncio
    async def test_md_validator_stub_returns_pass(self):
        gate = QualityGate(validator="gates/v.md", threshold=0.8)
        score = await evaluate_gate(gate, "p1")
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_unsupported_extension_raises(self):
        gate = QualityGate(validator="gates/v.txt", threshold=0.8)
        with pytest.raises(ValueError, match="Unsupported"):
            await evaluate_gate(gate, "p1")

    @pytest.mark.asyncio
    async def test_py_validator_returns_float_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(0.95)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.95

    @pytest.mark.asyncio
    async def test_py_validator_returns_json_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text('import json; print(json.dumps({"score": 0.75}))')
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.75

    @pytest.mark.asyncio
    async def test_py_validator_exception_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("raise Exception('fail')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_garbage_output_returns_zero(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print('not a number')")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_nonexistent_py_validator_returns_zero(self):
        gate = QualityGate(validator="/tmp/does_not_exist_12345.py", threshold=0.8)
        score = await evaluate_gate(gate, "p1")
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_zero_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(0.0)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_py_validator_perfect_score(self, tmp_path):
        script = tmp_path / "validator.py"
        script.write_text("print(1.0)")
        gate = QualityGate(validator=str(script), threshold=0.8)
        score = await evaluate_gate(gate, "p1", workdir=str(tmp_path))
        assert score == 1.0


class TestGateThresholdComparison:
    """Verify threshold comparison semantics match QualityGate model."""

    def test_above_threshold(self):
        gate = QualityGate(validator="v.md", threshold=0.8)
        assert 0.9 >= gate.threshold

    def test_below_threshold(self):
        gate = QualityGate(validator="v.md", threshold=0.8)
        assert not (0.5 >= gate.threshold)

    def test_equals_threshold(self):
        gate = QualityGate(validator="v.md", threshold=0.8)
        assert 0.8 >= gate.threshold
