#!/usr/bin/env python3
"""Structural integrity gate for the composed paper.

Reads `<WORKDIR>/output/paper.md` and validates that reconcile produced
a well-formed journal submission:
  - an H1 title
  - an Abstract section
  - all four body sections (Introduction, Methods, Results, Discussion)
  - a Conclusion section
  - a References section with at least 3 bulleted entries

Emits the full structured gate feedback schema so failures flow into
`{{_retry_reason}}` — though this gate is wired as blocking with no
retries since a malformed reconcile is a hard failure.

Invoked as a `gate_only` node's validator: ignores stdin (gate-only
nodes emit a stub output that is not the paper), reads from disk via
the WORKDIR env var injected by the orchestrator.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = [
    "Abstract",
    "Introduction",
    "Methods",
    "Results",
    "Discussion",
    "Conclusion",
    "References",
]


def main() -> int:
    workdir = Path(os.environ.get("WORKDIR", "."))
    paper = workdir / "output" / "paper.md"

    if not paper.exists():
        _emit(0.0, f"output/paper.md not found at {paper}", [f"paper missing: {paper}"])
        return 0

    text = paper.read_text()

    unmet: list[str] = []

    # H1 title
    if not re.search(r"^# .+", text, re.MULTILINE):
        unmet.append("missing H1 title")

    # H2 sections
    h2_headers = {m.group(1).strip() for m in re.finditer(r"^## (.+)$", text, re.MULTILINE)}
    for section in REQUIRED_SECTIONS:
        if section not in h2_headers:
            unmet.append(f"missing section: {section}")

    # References: at least 3 bulleted entries after the References header
    refs_match = re.search(r"^## References\s*\n(.*)", text, re.MULTILINE | re.DOTALL)
    if refs_match:
        refs_block = refs_match.group(1)
        bullets = re.findall(r"^[-*] ", refs_block, re.MULTILINE)
        if len(bullets) < 3:
            unmet.append(f"references has {len(bullets)} entries, need ≥3")
    # (missing-References already captured above)

    if unmet:
        _emit(0.0, "submission failed structural checks", unmet)
    else:
        _emit(1.0, "submission passes structural checks", [])

    return 0


def _emit(score: float, feedback: str, unmet: list[str]) -> None:
    print(json.dumps({
        "score": score,
        "feedback": feedback,
        "pass_criteria_met": [] if unmet else ["all required sections present"],
        "pass_criteria_unmet": unmet,
    }))


if __name__ == "__main__":
    sys.exit(main())
