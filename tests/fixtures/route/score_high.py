#!/usr/bin/env python3
"""Gate validator that always returns score 0.9 (above any reasonable threshold).

Stdin contains the upstream node output; we ignore it and emit JSON.
"""
import json
import sys

sys.stdin.read()
print(json.dumps({"score": 0.9, "feedback": "Looks great"}))
