
import json
from pathlib import Path
state = {"tasks": {
    "task_a": {"id": "task_a", "status": "pending"},
    "task_b": {"id": "task_b", "status": "pending"},
}}
Path('/home/christopher/projects/abe-froman/.temp/wave-spike-run/state.json').write_text(json.dumps(state, indent=2))
print("planner_seeded:2")
