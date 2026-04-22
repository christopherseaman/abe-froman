#!/usr/bin/env python3
"""Persist a phase's text output to disk.

Invoked as a command phase with Jinja-templated args — upstream phase
output flows through argv rather than any ACP Write tool. Avoids the
Write/Bash path-traversal hang documented in WISHLIST while still
demonstrating the "text-to-file" reconciliation pattern.

Usage: persist_paper.py <content> <dest_path>
"""
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: persist_paper.py <content> <dest_path>", file=sys.stderr)
        return 2
    content = sys.argv[1]
    dest = Path(sys.argv[2])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    print(f"wrote {len(content)} chars to {dest.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
