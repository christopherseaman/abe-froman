
import json
from pathlib import Path
state = json.loads(Path('/home/christopher/projects/abe-froman/.temp/wave-spike-run/state.json').read_text())
ready = [t for t in state["tasks"].values() if t["status"] == "pending"]
print(json.dumps({"items": ready}))
