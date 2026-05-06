
import json
from pathlib import Path
state_path = Path('/home/christopher/projects/abe-froman/.temp/wave-spike-run/state.json')
state = json.loads(state_path.read_text())
existing = set(state["tasks"].keys())
if existing == {"task_a", "task_b"}:
    state["tasks"]["task_c"] = {"id": "task_c", "status": "pending"}
    state_path.write_text(json.dumps(state, indent=2))
    print("reconcile_added:task_c")
else:
    print("reconcile_clean")
