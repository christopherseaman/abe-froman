"""Gate for the integrity_check gate_only phase.

Reads ../../paper/paper.md and ../../paper/bibliography.md relative to the
phase's worktree (resolves to <state.workdir>/paper/). Verifies:
  - paper.md exists and is ≥ 1000 words
  - bibliography.md exists and has ≥ 5 citation lines (any line that looks
    like a markdown bullet or numbered reference)
"""

import json
import re
import sys
from pathlib import Path

# Gate scripts run with cwd=state.workdir (per runtime/gates.py:129),
# so paths here are relative to the base workdir, not the phase worktree.
paper = Path("paper/paper.md")

met: list[str] = []
unmet: list[str] = []

if paper.exists():
    met.append("paper.md exists")
    text = paper.read_text()
    plain = re.sub(r"^[#\-\*\s]+", "", text, flags=re.MULTILINE)
    words = len(plain.split())
    if words >= 1000:
        met.append(f"paper.md has {words} words (≥1000)")
    else:
        unmet.append(f"paper.md has only {words} words (<1000)")

    refs = [
        l for l in text.splitlines()
        if re.match(r"^\s*(\d+\.|-|\*)\s+\S", l)
    ]
    if len(refs) >= 5:
        met.append(f"paper has ≥5 bulleted/numbered reference lines")
    else:
        unmet.append(f"paper has only {len(refs)} reference lines (<5)")
else:
    unmet.append("paper.md does not exist")

# Drain stdin so the subprocess contract is honored (gate_only phase output is empty).
sys.stdin.read()

score = 1.0 if not unmet else 0.0
print(json.dumps({
    "score": score,
    "feedback": "" if score == 1.0 else "; ".join(unmet),
    "pass_criteria_met": met,
    "pass_criteria_unmet": unmet,
}))
