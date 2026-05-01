#!/usr/bin/env python3
"""Gate score that depends on ATTEMPT_NUMBER env var.

Always emits 0.3 (below threshold) so the route loops back via the
retry-via-goto pattern. ATTEMPT_NUMBER is set by the orchestrator on
each gate evaluation, so the scores recorded across history have
slightly different feedback strings — that's enough to verify the
loop ran multiple times.
"""
import json
import os
import sys

sys.stdin.read()
attempt = int(os.environ.get("ATTEMPT_NUMBER", "1"))
print(json.dumps({"score": 0.3, "feedback": f"attempt {attempt} below threshold"}))
