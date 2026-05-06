
import json, sys
from pathlib import Path
task_id = sys.argv[1]
state_path = Path('/home/christopher/projects/abe-froman/.temp/wave-spike-run/state.json')
state = json.loads(state_path.read_text())
state["tasks"][task_id]["status"] = "done"
state_path.write_text(json.dumps(state, indent=2))
print(f"worker_done:{task_id}")
