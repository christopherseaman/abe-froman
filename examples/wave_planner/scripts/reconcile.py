"""Wave-planner: discover follow-up questions after a wave completes.

The deterministic discovery rule for this example: when wave 1
completes (the two seeded questions are both 'done' and no follow-up
exists yet), add a single sub-question. Wave 2 finds nothing new and
emits 'reconcile_clean' — the gate then routes to __end__.

Real workflows would run an LLM here to inspect partial results
and propose follow-ups. The deterministic stub keeps the e2e test
stable and lets contributors trace exactly which wave introduces
each task.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    state_path = Path("state.json")
    state = json.loads(state_path.read_text())
    qs = state["questions"]
    seed_ids = {"q_market_size", "q_growth_rate"}
    seeds_done = all(qs[i]["status"] == "done" for i in seed_ids if i in qs)
    follow_up_id = "q_competitor_share"
    if seeds_done and follow_up_id not in qs:
        qs[follow_up_id] = {
            "id": follow_up_id,
            "topic": "top three competitor market share",
            "status": "pending",
        }
        state_path.write_text(json.dumps(state, indent=2))
        print(f"reconcile_added:{follow_up_id}")
    else:
        print("reconcile_clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
