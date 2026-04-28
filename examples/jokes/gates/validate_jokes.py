"""Validate that node output is valid JSON with exactly 5 joke strings."""

import json
import sys

raw = sys.stdin.read().strip()

try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print("0.0")
    sys.exit(0)

if not isinstance(data, dict) or "jokes" not in data:
    print("0.0")
    sys.exit(0)

jokes = data["jokes"]
if not isinstance(jokes, list) or len(jokes) != 5:
    print("0.0")
    sys.exit(0)

if not all(isinstance(j, str) and len(j) > 0 for j in jokes):
    print("0.0")
    sys.exit(0)

print("1.0")
