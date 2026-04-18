"""Validate that the outline phase emitted a well-formed 4-section JSON manifest.

Required shape:
    {"items": [
        {"id": "...", "title": "...", "beats": ["...", ...]},
        ... (exactly 4 items)
    ]}

Each `items[*].beats` must be a non-empty list of strings.

Emits structured gate feedback so a retry carries specific complaints.
"""

import json
import re
import sys

raw = sys.stdin.read().strip()

# Tolerate surrounding markdown fences Claude sometimes adds despite "JSON only" instructions.
m = re.search(r"\{[\s\S]*\}", raw)
if not m:
    print(json.dumps({
        "score": 0.0,
        "feedback": "no JSON object found in output",
        "pass_criteria_met": [],
        "pass_criteria_unmet": ["output contains a JSON object"],
    }))
    sys.exit(0)

try:
    data = json.loads(m.group(0))
except json.JSONDecodeError as e:
    print(json.dumps({
        "score": 0.0,
        "feedback": f"JSON parse error: {e}",
        "pass_criteria_met": [],
        "pass_criteria_unmet": ["JSON parses cleanly"],
    }))
    sys.exit(0)

met: list[str] = []
unmet: list[str] = []

abstract_field = data.get("abstract")
if isinstance(abstract_field, str) and len(abstract_field) >= 100:
    met.append(f"top-level `abstract` is a non-trivial string ({len(abstract_field)} chars)")
else:
    unmet.append("top-level `abstract` must be a prose string of ≥100 characters")

buzzwords = data.get("buzzwords")
if isinstance(buzzwords, list) and len(buzzwords) >= 3 and all(isinstance(b, str) for b in buzzwords):
    met.append(f"top-level `buzzwords` has {len(buzzwords)} string entries")
else:
    unmet.append("top-level `buzzwords` must be a list of ≥3 strings")

items = data.get("items")
if isinstance(items, list):
    met.append("top-level `items` is a list")
else:
    unmet.append("top-level `items` must be a list")
    items = []

if len(items) == 4:
    met.append("exactly 4 sections")
else:
    unmet.append(f"expected exactly 4 sections, got {len(items)}")

required_ids = {"intro", "methods", "results", "discussion"}
got_ids = {item.get("id") for item in items if isinstance(item, dict)}
if required_ids.issubset(got_ids):
    met.append("all four required section ids present (intro, methods, results, discussion)")
else:
    missing = required_ids - got_ids
    unmet.append(f"missing section ids: {sorted(missing)}")

for item in items:
    if not isinstance(item, dict):
        unmet.append("every item must be an object")
        continue
    if not item.get("title"):
        unmet.append(f"item {item.get('id', '?')} missing non-empty `title`")
    beats = item.get("beats")
    if not isinstance(beats, list) or not beats:
        unmet.append(f"item {item.get('id', '?')} missing non-empty `beats` list")
    elif not all(isinstance(b, str) and b.strip() for b in beats):
        unmet.append(f"item {item.get('id', '?')} has empty/non-string beats")

score = 1.0 if not unmet else 0.0
print(json.dumps({
    "score": score,
    "feedback": "" if score == 1.0 else "; ".join(unmet),
    "pass_criteria_met": met,
    "pass_criteria_unmet": unmet,
}))
