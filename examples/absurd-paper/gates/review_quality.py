"""Record-only gate for per-reviewer peer-review subphases.

Per CLAUDE.md known limitation, subphase gates record scores but don't
trigger retries. This script still emits structured feedback so the JSONL
`gate_evaluated` events carry meaningful data.

Checks the review has:
  - at least 2 paragraphs (double-newline separated)
  - at least one paragraph mentioning a concern / critique (negative signal)
  - at least one paragraph with a positive note (praise / strength)
"""

import json
import re
import sys

text = sys.stdin.read().strip()
paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

NEG_CUES = re.compile(
    r"\b(concern|weak|flaw|limitation|unconvincing|question|doubt|lacks?|fails?|"
    r"unclear|insufficient|problematic|issue|critique)\b",
    re.IGNORECASE,
)
POS_CUES = re.compile(
    r"\b(strong|clever|innovative|compelling|rigorous|commendable|novel|"
    r"impressive|elegant|interesting|insightful|praiseworthy)\b",
    re.IGNORECASE,
)

met: list[str] = []
unmet: list[str] = []

if len(paragraphs) >= 2:
    met.append(f"{len(paragraphs)} paragraphs (≥2)")
else:
    unmet.append(f"only {len(paragraphs)} paragraphs (<2)")

if any(NEG_CUES.search(p) for p in paragraphs):
    met.append("contains at least one critical / concern-flagging passage")
else:
    unmet.append("no critical / concern passage detected")

if any(POS_CUES.search(p) for p in paragraphs):
    met.append("contains at least one positive / strength passage")
else:
    unmet.append("no positive / strength passage detected")

score = 1.0 if not unmet else 0.0
print(json.dumps({
    "score": score,
    "feedback": "" if score == 1.0 else "; ".join(unmet),
    "pass_criteria_met": met,
    "pass_criteria_unmet": unmet,
}))
