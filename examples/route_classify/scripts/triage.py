#!/usr/bin/env python3
"""Emit a one-word category on stdout.

Reads ticket text from argv[1] (or stdin if absent) and classifies into
one of: spam | urgent | escalate | standard. The downstream route node
reads this stdout via the namespace binding ``triage`` and dispatches
with string equality.

Mode selection — the runtime treats this as a script execution because
the URL ends in `.py`; the interpreter is dispatched via the script
table in runtime/executor/dispatch.py.
"""
from __future__ import annotations

import sys


def classify(text: str) -> str:
    lower = text.lower()
    if "lottery" in lower or "click here" in lower:
        return "spam"
    if "production down" in lower or "page oncall" in lower:
        return "urgent"
    if "blocker" in lower or "asap" in lower:
        return "escalate"
    return "standard"


def main() -> None:
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    # No trailing newline — keeps the route's string-compare clean.
    sys.stdout.write(classify(text))


if __name__ == "__main__":
    main()
