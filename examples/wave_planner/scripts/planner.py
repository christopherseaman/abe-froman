"""Wave-planner: seed initial research questions into state.json.

Runs once at the start of the workflow. Subsequent waves never re-fire
this script (planner has no incoming goto); only `dispatcher` and
`reconcile` mutate state.json after the seeding.
"""
import json
import sys
from pathlib import Path


def main() -> None:
    state_path = Path("state.json")
    state = {
        "questions": {
            "q_market_size": {
                "id": "q_market_size",
                "topic": "global market size for left-handed scissors",
                "status": "pending",
            },
            "q_growth_rate": {
                "id": "q_growth_rate",
                "topic": "year-over-year growth rate",
                "status": "pending",
            },
        }
    }
    state_path.write_text(json.dumps(state, indent=2))
    print(f"planner_seeded:{len(state['questions'])}")


if __name__ == "__main__":
    sys.exit(main() or 0)
