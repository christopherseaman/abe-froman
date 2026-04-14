from __future__ import annotations

from pathlib import Path

from abe_froman.schema.models import OutputContract


def scaffold_output_directory(contract: OutputContract, workdir: str) -> None:
    """Pre-create the output directory tree for a phase's output contract."""
    base = Path(workdir) / contract.base_directory
    base.mkdir(parents=True, exist_ok=True)


def validate_output_contract(
    contract: OutputContract,
    workdir: str,
) -> list[str]:
    """Check that all required files exist. Returns list of missing files."""
    base = Path(workdir) / contract.base_directory
    missing = []
    for f in contract.required_files:
        if not (base / f).exists():
            missing.append(str(Path(contract.base_directory) / f))
    return missing
