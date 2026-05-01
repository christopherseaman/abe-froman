#!/usr/bin/env python3
"""Gate validator that always returns score 0.3 (below threshold)."""
import json
import sys

sys.stdin.read()
print(json.dumps({"score": 0.3, "feedback": "Needs more work"}))
