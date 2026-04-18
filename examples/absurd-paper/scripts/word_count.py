"""Report word-count statistics for the generated paper.

Runs in the phase's worktree. Reads paper.md via `../../paper/`
which resolves to <state.workdir>/paper/ — the deterministic shared staging
area written by the persist phase.

Stdout: {"words": N, "paper_md_bytes": M}
"""

import json
import re
import sys
from pathlib import Path

paper = Path("../../paper/paper.md")

if not paper.exists():
    print(json.dumps({"error": f"missing {paper}"}), file=sys.stderr)
    sys.exit(1)

paper_text = paper.read_text()

# Naive word counter — splits on whitespace after stripping markdown headers/bullets.
plain = re.sub(r"^[#\-\*\s]+", "", paper_text, flags=re.MULTILINE)
words = len(plain.split())

print(json.dumps({
    "words": words,
    "paper_md_bytes": len(paper_text.encode("utf-8")),
}))
