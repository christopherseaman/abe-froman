"""Wave-planner: 'answer' one question, mark it done in state.json.

Real workflows would invoke an LLM here; the example uses a
deterministic stub so the topology runs without backend keys and
test assertions remain stable.

Wave-pattern shared state: parallel workers running in the same
wave race on read-modify-write of state.json. fcntl.flock serializes
the critical section so each worker's status mutation is preserved.
For non-POSIX hosts (Windows), authors writing similar workflows
should swap fcntl for portable-locking or per-task marker files.
"""
import fcntl
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("worker: missing question_id arg", file=sys.stderr)
        return 2
    question_id = argv[1]
    state_path = Path("state.json")
    with state_path.open("r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        state = json.load(fh)
        if question_id not in state["questions"]:
            print(f"worker: unknown question {question_id}", file=sys.stderr)
            return 1
        state["questions"][question_id]["status"] = "done"
        fh.seek(0)
        json.dump(state, fh, indent=2)
        fh.truncate()
    print(f"worker_done:{question_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
