"""Wave-planner: emit a manifest of currently-pending questions.

The fan_out router parses this script's stdout as JSON. Each pending
question becomes a Send branch that runs `worker.py` with the
question id. Already-answered questions are excluded — that's how
"the next wave" naturally narrows.

If no questions are pending the manifest is empty and fan-out
dispatches zero children; the gate then routes to __end__ on its
"reconcile_clean" branch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    state = json.loads(Path("state.json").read_text())
    pending = [q for q in state["questions"].values() if q["status"] == "pending"]
    print(json.dumps({"items": pending}))


if __name__ == "__main__":
    sys.exit(main() or 0)
